"""Phase 1: query the Foundry agent and cache one raw JSON response per site.

The only phase that costs money, and the only one that touches the network. Resumable: a site
with a cached response is skipped unless --force.
"""
from __future__ import annotations

import csv
import time
from datetime import datetime
from pathlib import Path
from typing import Any

from .geocoder import enrich_sites_from_cache
from .input_loader import resolve_sites, snapshot_seed, write_csv_records
from .pilot_selector import select_mixed_pilot
from .prompt_builder import build_site_prompt, coordinates_only, requested_attributes
from .run_context import (
    SEARCH_TOOL_RETRY_DELAY_SECONDS,
    PipelineConfig,
    get_client,
    hard_timeout,
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
    FOUNDRY_RUN_LOG_HEADERS,
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

    if not explicit and not config.include_excluded_countries:
        before = len(selected)
        selected = [
            site
            for site in selected
            if normalize_scalar(site.get("country_iso3")).upper() not in DEFAULT_EXCLUDED_ISO3
        ]
        dropped = before - len(selected)
        if dropped:
            print(
                f"Excluded {dropped:,} site(s) in {', '.join(sorted(DEFAULT_EXCLUDED_ISO3))} "
                "(already Tier 1 covered; pass --include-excluded-countries to search them)."
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


# --- phase 1: search --------------------------------------------------------------------------
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
    sites, headers = resolve_sites(config)
    # A database-seeded run has no input file to point back at, so it records the corpus it
    # actually read. Skipped when a CSV was supplied, which is already that record.
    snapshot = snapshot_seed(config, sites, headers)
    if snapshot is not None:
        print(f"Seeded {len(sites)} facilities from the database; snapshot: {snapshot}")

    selected = select_sites(sites, config)
    if not selected:
        raise ValueError("No sites matched the selection filters.")

    # The prompt asks the agent to search using municipality and admin names, so those fields
    # have to be populated or that instruction is asking for context we never supplied.
    selected = enrich_sites_from_cache(
        selected, cache_path=config.geocode_cache, enabled=config.use_geocode_cache
    )
    if config.use_geocode_cache:
        hits = sum(1 for site in selected if site.get("geocode_status") == "cache_hit")
        print(f"Geocode cache: {hits}/{len(selected)} sites enriched with admin context")

    raw_dir(config.run_dir).mkdir(parents=True, exist_ok=True)
    client = get_client(config)
    agent = agent_identity(client)
    run_log: list[dict[str, Any]] = []

    coordinate_only_sites = sum(1 for site in selected if coordinates_only(site))
    if coordinate_only_sites:
        print(
            f"Coordinates-only scope for {coordinate_only_sites:,} site(s) in "
            f"{', '.join(sorted(COORDINATES_ONLY_ISO3))}."
        )

    for index, site in enumerate(selected, start=1):
        site_id = normalize_scalar(site.get("site_id"))
        site_name = normalize_scalar(site.get("site_name"))
        attributes = requested_attributes(site)
        if only_attributes is not None:
            wanted = set(only_attributes)
            attributes = [a for a in attributes if a in wanted]
            if not attributes:
                continue
        path = raw_response_path(config.run_dir, site_id, pass_label)
        started = datetime.now().replace(microsecond=0).isoformat()
        status, error = "Started", ""
        tool_retries = 0

        print(f"[{index}/{len(selected)}] {site_id} {site_name} ({len(attributes)} attrs)", flush=True)

        payload: dict[str, Any] | None = None
        if path.exists() and not config.force:
            status = "Skipped existing response"
            try:
                payload = load_json(path)
            except (OSError, json.JSONDecodeError) as exc:
                status, error = "Failed", f"Cached response unreadable: {exc}"
        else:
            prompt = build_site_prompt(site, attributes)
            max_attempts = max(1, config.search_tool_retries)
            for attempt in range(1, max_attempts + 1):
                try:
                    with hard_timeout(config.hard_site_timeout_seconds):
                        payload = client.search_site(site, prompt)
                except Exception as exc:  # noqa: BLE001 - one bad site must not end a multi-day run
                    status, error = "Failed", f"{type(exc).__name__}: {exc}"
                    print(f"    {error}", flush=True)
                    break

                if not search_tool_failed(payload):
                    save_json(path, payload)
                    status = "Succeeded" if attempt == 1 else f"Succeeded after {attempt - 1} tool retry(s)"
                    error = ""
                    break

                tool_retries = attempt
                error = "Agent reported its web-search tool failed."
                if attempt < max_attempts:
                    print(f"    search tool failed; retrying ({attempt}/{max_attempts - 1})", flush=True)
                    time.sleep(SEARCH_TOOL_RETRY_DELAY_SECONDS)
                else:
                    save_json(path, payload)
                    status = f"Search tool failed after {max_attempts} attempt(s)"
                    print(f"    {status}", flush=True)
            if index < len(selected) and config.site_delay_seconds > 0:
                time.sleep(config.site_delay_seconds)

        attribute_count = len(payload.get("attributes", []) or []) if payload else 0
        source_count = (
            sum(len(item.get("sources", []) or []) for item in (payload.get("attributes", []) or []))
            if payload
            else 0
        )
        run_log.append(
            {
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
            }
        )

    log_path = config.run_dir / "foundry_run_log.csv"
    write_csv_records(log_path, merge_run_log(log_path, run_log), FOUNDRY_RUN_LOG_HEADERS)

    failures = [row for row in run_log if str(row["status"]).startswith("Search tool failed")]
    print(f"\nSearched {len(selected)} sites. Run log: {log_path}")
    if failures:
        ids = " ".join(row["site_id"] for row in failures)
        print(f"\n!! {len(failures)} site(s) still failing after retries. Try again later:")
        print(f"   uv run waste-ai-search search --run-id {config.run_id} --force --site-ids {ids}")
    return config.run_dir


def merge_run_log(path: Path, new_rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Keep history across resumed and retried searches; the newest row for a site wins.

    A retried search only covers the sites it touched, so replacing the file would erase every
    other site from the log - which matters a great deal for a multi-day resumable run.
    """
    merged: dict[str, dict[str, Any]] = {}
    if path.exists():
        with path.open(newline="", encoding="utf-8-sig") as handle:
            for row in csv.DictReader(handle):
                merged[normalize_scalar(row.get("site_id"))] = dict(row)
    for row in new_rows:
        merged[normalize_scalar(row.get("site_id"))] = row
    return sorted(
        merged.values(),
        key=lambda row: int(row["site_id"]) if str(row.get("site_id", "")).isdigit() else 0,
    )
