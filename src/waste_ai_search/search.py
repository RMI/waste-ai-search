"""Phase 1: query the Foundry agent and cache one raw JSON response per site.

The only phase that costs money, and the only one that touches the network. Resumable: a site
with a cached response is skipped unless --force.
"""
from __future__ import annotations

import csv
import json
import threading
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path
from typing import Any

from .geocoder import enrich_sites_from_cache
from .input_loader import resolve_sites, run_seed_path, write_csv_records
from .link_check import BROKEN, LINK_CHECK_FILE, check_urls
from .pilot_selector import select_mixed_pilot
from .prompt_builder import build_site_prompt, coordinates_only, requested_attributes
from .run_context import (
    SEARCH_TOOL_RETRY_DELAY_SECONDS,
    PipelineConfig,
    get_client,
    load_json,
    raw_dir,
    raw_response_path,
    save_json,
    search_tool_failed,
    agent_identity,
)
from .schema import (
    COORDINATES_ONLY_ISO3,
    DEFAULT_EXCLUDED_ISO3,
    DEFAULT_EXCLUSION_REASONS,
    FOUNDRY_RUN_LOG_HEADERS,
    in_batch,
    normalize_scalar,
)


def select_sites(sites: list[dict[str, Any]], config: PipelineConfig) -> list[dict[str, Any]]:
    """Apply the explicit filters, then either take the first N or sample a spread.

    `--limit` takes the first N in seed order, which is facility-id order and therefore clusters
    by country and data source. `--pilot-size` instead draws a deterministic spread across
    priority countries, high-volume countries and deliberately hard generic-named sites.

    Countries in DEFAULT_EXCLUDED_ISO3 are dropped unless the caller asked for them explicitly -
    by naming the country in --iso3, by listing site ids, or with
    --include-excluded-countries. An explicit request beats a default.
    """
    selected = sites
    explicit = bool(config.site_ids)
    if config.site_ids:
        wanted = {normalize_scalar(value) for value in config.site_ids}
        selected = [site for site in selected if normalize_scalar(site.get("site_id")) in wanted]
    if config.iso3:
        wanted_iso = {value.strip().upper() for value in config.iso3}
        selected = [site for site in selected if normalize_scalar(site.get("country_iso3")).upper() in wanted_iso]
        explicit = explicit or bool(wanted_iso & DEFAULT_EXCLUDED_ISO3)
    if config.batch:
        selected = [site for site in selected if in_batch(normalize_scalar(site.get("country_iso3")), config.batch)]
        countries = sorted({normalize_scalar(site.get("country_iso3")).upper() for site in selected})
        print(f"Batch {config.batch}: {len(selected):,} site(s) in {len(countries)} country(ies).")

    if not explicit and not config.include_excluded_countries:
        dropped = Counter(
            iso3
            for site in selected
            if (iso3 := normalize_scalar(site.get("country_iso3")).upper()) in DEFAULT_EXCLUDED_ISO3
        )
        selected = [
            site
            for site in selected
            if normalize_scalar(site.get("country_iso3")).upper() not in DEFAULT_EXCLUDED_ISO3
        ]
        for iso3, count in sorted(dropped.items()):
            print(
                f"Excluded {count:,} site(s) in {iso3} ({DEFAULT_EXCLUSION_REASONS[iso3]}; "
                "pass --include-excluded-countries to search them)."
            )
    # A site with no requestable attributes has nothing to ask; querying it would spend a
    # request and a prompt to receive nothing. Country scope can empty a site entirely.
    before = len(selected)
    selected = [site for site in selected if requested_attributes(site)]
    nothing_to_ask = before - len(selected)
    if nothing_to_ask:
        print(f"Skipped {nothing_to_ask:,} site(s) with no attributes left to search.")

    if config.pilot_size > 0:
        return select_mixed_pilot(selected, size=config.pilot_size)
    if config.limit > 0:
        selected = selected[: config.limit]
    return selected


def add_searches(total: int | None, client: Any) -> int | None:
    """Add the Bing queries of the client's last call to a site's running total."""
    count = getattr(client, "web_searches", None)
    if count is None:
        return total
    return (total or 0) + count


# --- phase 1: search --------------------------------------------------------------------------
def check_run_links(run_dir: Path) -> dict:
    """Fetch every source URL in a run's cached responses once, and record the verdicts."""
    from .arbitrate import cached_source_urls

    results = check_urls(cached_source_urls(run_dir), cache_path=run_dir / LINK_CHECK_FILE)
    broken = sum(1 for r in results.values() if r.verdict == BROKEN)
    repaired = sum(1 for r in results.values() if r.resolved_url != r.url)
    print(f"Links: {len(results)} checked, {broken} broken, {repaired} repaired -> {LINK_CHECK_FILE}")
    return results


def run_search(
    config: PipelineConfig,
    pass_label: str = "",
    only_attributes: list[str] | None = None,
) -> Path:
    """Query Foundry for the selected sites and cache one response each.

    `pass_label` keeps a follow-up pass's responses beside the first rather than overwriting them.
    `only_attributes` narrows what is asked - a follow-up asks for the attributes the first pass
    unlocked, not the whole set again.
    """
    # Resuming a search, or re-running one with the same run id, reads this run's pinned seed
    # rather than a newer database state, so cached responses stay matched to the corpus they
    # were searched against.
    reusing = config.input_csv is None and run_seed_path(config).exists()
    sites, _headers = resolve_sites(config)
    if config.input_csv is None:
        verb = "Reusing this run's seed snapshot" if reusing else "Seeded from the database, snapshot"
        print(f"{verb}: {run_seed_path(config)} ({len(sites)} facilities)")

    selected = select_sites(sites, config)
    if not selected:
        hint = (
            f" This run is pinned to {run_seed_path(config)}; use a new --run-id to seed afresh."
            if reusing
            else ""
        )
        raise ValueError(f"No sites matched the selection filters.{hint}")

    # The prompt asks the agent to search using municipality and admin names, so those fields
    # have to be populated or that instruction is asking for context we never supplied.
    selected = enrich_sites_from_cache(
        selected, cache_path=config.geocode_cache, enabled=config.use_geocode_cache
    )
    if config.use_geocode_cache:
        hits = sum(1 for site in selected if site.get("geocode_status") == "cache_hit")
        print(f"Geocode cache: {hits}/{len(selected)} sites enriched with admin context")

    raw_dir(config.run_dir).mkdir(parents=True, exist_ok=True)
    agent = agent_identity(get_client(config))

    coordinate_only_sites = sum(1 for site in selected if coordinates_only(site))
    if coordinate_only_sites:
        print(
            f"Coordinates-only scope for {coordinate_only_sites:,} site(s) in "
            f"{', '.join(sorted(COORDINATES_ONLY_ISO3))}."
        )

    work: list[tuple[dict[str, Any], list[str]]] = []
    for site in selected:
        attributes = requested_attributes(site)
        if only_attributes is not None:
            wanted = set(only_attributes)
            attributes = [a for a in attributes if a in wanted]
            if not attributes:
                continue
        work.append((site, attributes))

    # One client per thread: a client holds the Bing count of the site it is searching.
    local = threading.local()

    def search_one(site: dict[str, Any], attributes: list[str]) -> dict[str, Any]:
        if not hasattr(local, "client"):
            local.client = get_client(config)
        return search_one_site(config, local.client, site, attributes, pass_label, agent)

    workers = max(1, config.workers)
    print(f"Searching {len(work)} site(s), {workers} at a time.", flush=True)
    log_path = config.run_dir / "foundry_run_log.csv"
    executor = ThreadPoolExecutor(max_workers=workers)
    futures = []
    try:
        futures = [executor.submit(search_one, site, attributes) for site, attributes in work]
        for done, future in enumerate(as_completed(futures), start=1):
            row = future.result()
            print(f"[{done}/{len(work)}] {row['site_id']} {row['site_name']}: {row['status']}", flush=True)
    except KeyboardInterrupt:
        print("\nStopping: sites not yet started are cancelled; waiting for those in flight.", flush=True)
        raise
    finally:
        # Sites in flight finish and are logged, so an interrupted run keeps their Bing counts.
        executor.shutdown(wait=True, cancel_futures=True)
        run_log = [f.result() for f in futures if f.done() and not f.cancelled() and f.exception() is None]
        write_csv_records(log_path, merge_run_log(log_path, run_log), FOUNDRY_RUN_LOG_HEADERS)

    failures = [row for row in run_log if str(row["status"]).startswith("Search tool failed")]
    print(f"\nSearched {len(work)} sites. Run log: {log_path}")
    counted = [row["web_searches"] for row in run_log if row["web_searches"] != ""]
    if counted:
        print(f"Bing queries: {sum(counted):,} across {len(counted)} searched site(s).")

    # WP-533: verify cited URLs now, so arbitration can apply the verdicts offline. Every URL in
    # the run's cached responses is passed, but the sidecar is a cache, so a resumed search or the
    # pass-2 follow-up only fetches URLs not already checked.
    if config.check_links:
        check_run_links(config.run_dir)
    if failures:
        ids = " ".join(row["site_id"] for row in failures)
        print(f"\n!! {len(failures)} site(s) still failing after retries. Try again later:")
        print(f"   uv run waste-ai-search search --run-id {config.run_id} --force --site-ids {ids}")
    return config.run_dir


def search_one_site(
    config: PipelineConfig,
    client: Any,
    site: dict[str, Any],
    attributes: list[str],
    pass_label: str,
    agent: str,
) -> dict[str, Any]:
    """Search one site, or reuse its cached response, and return its run-log row.

    Runs on a worker thread, so it prints with the site id: lines from parallel sites interleave.
    """
    site_id = normalize_scalar(site.get("site_id"))
    site_name = normalize_scalar(site.get("site_name"))
    path = raw_response_path(config.run_dir, site_id, pass_label)
    started = datetime.now().replace(microsecond=0).isoformat()
    status, error = "Started", ""
    tool_retries = 0
    # None when the client cannot count (a fake in tests); blank in the log, never a false 0.
    web_searches: int | None = None

    payload: dict[str, Any] | None = None
    if path.exists() and not config.force:
        status = "Skipped existing response"
        try:
            payload = load_json(path)
        except (OSError, json.JSONDecodeError) as exc:
            status, error = "Failed", f"Cached response unreadable: {exc}"
    else:
        prompt = build_site_prompt(site, attributes, max_web_searches=config.max_web_searches)
        max_attempts = max(1, config.search_tool_retries)
        for attempt in range(1, max_attempts + 1):
            try:
                payload = client.search_site(site, prompt)
            except Exception as exc:  # noqa: BLE001 - one bad site must not end a multi-day run
                web_searches = add_searches(web_searches, client)
                status, error = "Failed", f"{type(exc).__name__}: {exc}"
                print(f"    {site_id}: {error}", flush=True)
                break
            web_searches = add_searches(web_searches, client)

            if not search_tool_failed(payload):
                save_json(path, payload)
                status = "Succeeded" if attempt == 1 else f"Succeeded after {attempt - 1} tool retry(s)"
                error = ""
                break

            tool_retries = attempt
            error = "Agent reported its web-search tool failed."
            if attempt < max_attempts:
                print(f"    {site_id}: search tool failed; retrying ({attempt}/{max_attempts - 1})", flush=True)
                time.sleep(SEARCH_TOOL_RETRY_DELAY_SECONDS)
            else:
                save_json(path, payload)
                status = f"Search tool failed after {max_attempts} attempt(s)"
        if config.site_delay_seconds > 0:
            time.sleep(config.site_delay_seconds)

    attribute_count = len(payload.get("attributes", []) or []) if payload else 0
    source_count = (
        sum(len(item.get("sources", []) or []) for item in (payload.get("attributes", []) or []))
        if payload
        else 0
    )
    return {
        "run_id": config.run_id,
        "site_id": site_id,
        "site_name": site_name,
        "country_iso3": normalize_scalar(site.get("country_iso3")),
        "requested_attributes": "; ".join(attributes),
        "agent_id": agent,
        "request_started_at": started,
        "request_finished_at": datetime.now().replace(microsecond=0).isoformat(),
        "status": status,
        "raw_response_path": str(path),
        "parsed_attribute_count": attribute_count,
        "parsed_source_count": source_count,
        "error_message": error,
        "retry_count": tool_retries,
        "web_searches": "" if web_searches is None else web_searches,
    }


def merge_run_log(path: Path, new_rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Keep history across resumed and retried searches; the newest row for a site wins.

    A retried search only covers the sites it touched, so replacing the file would erase every
    other site from the log - which matters a great deal for a multi-day resumable run.

    `web_searches` is the exception: every pass is billed, so a site's count adds to its earlier
    one. A skipped site logs a blank count, which keeps the earlier one rather than erasing it.
    """
    merged: dict[str, dict[str, Any]] = {}
    if path.exists():
        with path.open(newline="", encoding="utf-8-sig") as handle:
            for row in csv.DictReader(handle):
                merged[normalize_scalar(row.get("site_id"))] = dict(row)
    for row in new_rows:
        site_id = normalize_scalar(row.get("site_id"))
        earlier = str((merged.get(site_id) or {}).get("web_searches") or "")
        if earlier.isdigit():
            row = {**row, "web_searches": int(earlier) + int(row["web_searches"] or 0)}
        merged[site_id] = row
    return sorted(
        merged.values(),
        key=lambda row: int(row["site_id"]) if str(row.get("site_id", "")).isdigit() else 0,
    )
