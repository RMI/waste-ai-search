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

from .costs import RUN_LOG_FILE, TOKEN_COLUMNS, Usage, describe_run_cost, run_cost
from .credibility import dropped_source_site
from .geocoder import enrich_sites_from_cache, look_up_places, save_site_places, with_other_sites, with_places
from .input_loader import resolve_sites, run_seed_path, write_csv_records
from .link_check import BROKEN, LINK_CHECK_FILE, check_urls
from .page_text import PAGE_TEXT_DIR
from .locate import (
    MATCHING_BASES,
    SOURCE_LOCATIONS_FILE,
    STATE_NAMES_FILE,
    GeocoderError,
    canonical_state,
    Located,
    describe_bases,
    load_locations,
    locate_source,
    make_geocoder,
    save_locations,
    states_location,
)
from .payload import found_attributes, merge_payloads, read_payload
from .pilot_selector import select_mixed_pilot
from .prompt_builder import build_site_prompt, requested_attributes
from .run_context import (
    FOLLOW_UP_PASS,
    PASS_SEPARATOR,
    SEARCH_TOOL_RETRY_DELAY_SECONDS,
    PipelineConfig,
    get_client,
    load_json,
    raw_dir,
    raw_response_path,
    save_json,
    search_tool_failed,
    site_id_from_path,
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
from .searchable_names import (
    SKIPPED_SITES_FILE,
    class_breakdown,
    record_skips,
    skip_unsearchable_names,
)


# Where a saved response keeps what the agent's web search tool did: each search with its queries, and
# each page it opened or searched in (foundry_client.web_search_actions).
SEARCH_ACTIONS = "_search_actions"


class NothingToSearch(ValueError):
    """A search whose selection leaves no site to search: an instruction to the user, not a bug."""


def select_sites(sites: list[dict[str, Any]], config: PipelineConfig) -> list[dict[str, Any]]:
    """Apply the explicit filters, then either take the first N or sample a spread.

    `--limit` takes the first N in seed order, which is facility-id order and therefore clusters
    by country and data source. `--pilot-size` instead draws a deterministic spread across
    priority countries, high-volume countries and, with --include-unsearchable-names,
    deliberately hard generic-named sites.

    Countries in DEFAULT_EXCLUDED_ISO3 are dropped unless the caller asked for them explicitly -
    by naming the country in --iso3, by listing site ids, or with
    --include-excluded-countries. Sites whose name gives a search nothing to look for
    (searchable_names.py) are dropped unless their ids are listed or
    --include-unsearchable-names is passed. An explicit request beats a default.
    """
    return choose_sites(sites, config)[0]


def choose_sites(
    sites: list[dict[str, Any]], config: PipelineConfig
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """select_sites, plus a skipped_sites.csv row for each site the name rule dropped."""
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
    # request and a prompt to receive nothing. Country scope empties a site entirely: Brazil,
    # scoped to coordinates alone, has nothing to ask now that a location is not asked for.
    before = len(selected)
    selected = [site for site in selected if requested_attributes(site)]
    nothing_to_ask = before - len(selected)
    if nothing_to_ask:
        print(f"Skipped {nothing_to_ask:,} site(s) with no attributes left to search.")
    # Names that give a search nothing to look for. After the check above, so it counts only sites
    # with something to ask, and before --limit and --pilot-size, so those take searchable sites.
    # The count is therefore of the whole selection, including sites a limit or sample would not
    # have searched. Listing site ids names each site, so it wins; naming a country with --iso3
    # does not name its unnamed sites, so it doesn't.
    skipped: list[dict[str, Any]] = []
    if not config.site_ids and not config.include_unsearchable_names:
        selected, skipped = skip_unsearchable_names(selected)
        if skipped:
            print(
                f"Skipped {len(skipped):,} site(s) whose name gives a search nothing to look for "
                f"({class_breakdown([row['name_class'] for row in skipped])}; "
                "pass --include-unsearchable-names to search them)."
            )
    if config.batch:
        # Counted after the default exclusions, so batch 6 reports what it will search rather
        # than the ~9,600 USA and Brazil sites it is about to drop.
        countries = sorted({normalize_scalar(site.get("country_iso3")).upper() for site in selected})
        print(f"Batch {config.batch}: {len(selected):,} site(s) in {len(countries)} country(ies).")
    if config.min_tonnes > 0:
        # After the rules above, like --limit, so a second pass on a batch's largest sites takes the
        # sites its first pass searched. Listed, so which sites it took can be checked.
        selected = [site for site in selected if (incoming_tonnes(site) or 0) >= config.min_tonnes]
        ids = " ".join(normalize_scalar(site.get("site_id")) for site in selected)
        print(f"Taking {config.min_tonnes:,.0f} t a year or more: {len(selected):,} site(s){': ' + ids if ids else '.'}")

    if config.pilot_size > 0:
        return select_mixed_pilot(selected, size=config.pilot_size), skipped
    if config.limit > 0:
        selected = selected[: config.limit]
    return selected, skipped


def incoming_tonnes(site: dict[str, Any]) -> float | None:
    """The waste a year our seed says a site takes, in tonnes, or None where it says nothing."""
    try:
        return float(normalize_scalar(site.get("annual_incoming_waste_metric_tonnes")).replace(",", ""))
    except ValueError:
        return None


def add_searches(total: int | None, client: Any) -> int | None:
    """Add the Bing queries of the client's last call to a site's running total."""
    count = getattr(client, "web_searches", None)
    if count is None:
        return total
    return (total or 0) + count


def add_tool_calls(total: int | None, client: Any) -> int | None:
    """Add the web search tool calls of the client's last call to a site's running total: its
    searches, and the pages it opened or searched in, each of which counts toward the cap."""
    actions = getattr(client, "search_actions", None)
    if not isinstance(actions, list):
        return total
    return (total or 0) + len(actions)


def add_usage(total: Usage | None, client: Any) -> Usage | None:
    """Add the model tokens of the client's last call to a site's running total."""
    usage = getattr(client, "usage", None)
    if not isinstance(usage, Usage):
        return total
    total = total or Usage()
    for column in TOKEN_COLUMNS:
        setattr(total, column, getattr(total, column) + getattr(usage, column))
    return total


# --- phase 1: search --------------------------------------------------------------------------
def check_run_links(run_dir: Path, keep_text: bool = True, retry_unverified: bool = False) -> dict:
    """Fetch every source URL in a run's cached responses once, and record the verdicts. With
    keep_text, the text of each page that loads is kept in page_text/ too. With retry_unverified, a
    URL left Unverified for a reason that may pass is fetched again (link_check.retryable)."""
    from .arbitrate import cached_source_urls

    text_dir = run_dir / PAGE_TEXT_DIR if keep_text else None
    results = check_urls(
        cached_source_urls(run_dir), cache_path=run_dir / LINK_CHECK_FILE, text_dir=text_dir, retry_unverified=retry_unverified
    )
    broken = sum(1 for r in results.values() if r.verdict == BROKEN)
    repaired = sum(1 for r in results.values() if r.resolved_url != r.url)
    kept = f"; text kept for {sum(1 for r in results.values() if r.text_file)} page(s) -> {PAGE_TEXT_DIR}/" if keep_text else ""
    print(f"Links: {len(results)} checked, {broken} broken, {repaired} repaired{kept} -> {LINK_CHECK_FILE}")
    return results


# The locate step saves its answers after this many lookups, so an interrupted run keeps what it
# paid for without rewriting the file for every source.
LOCATE_SAVE_EVERY = 25


class _CountingGeocoder:
    """A geocoder that counts its lookups, so the locate step knows when it has paid for some."""

    def __init__(self, geocoder: Any) -> None:
        self.geocoder = geocoder
        self.name = geocoder.name
        self.calls = 0

    def geocode(self, address: str, iso2: str) -> Any:
        self.calls += 1
        return self.geocoder.geocode(address, iso2)

    def __getattr__(self, name: str) -> Any:
        # Only a geocoder that can look states up (Nominatim) has `state`.
        if name == "state" and hasattr(self.geocoder, "state"):
            def state(text: str, iso2: str) -> str:
                self.calls += 1
                return self.geocoder.state(text, iso2)
            return state
        raise AttributeError(name)


# locate_run's default: the geocoder the run's configuration names.
_CONFIGURED = object()


def locate_run(
    config: PipelineConfig, sites: list[dict[str, Any]] | None = None, geocoder: Any = _CONFIGURED
) -> list[Located]:
    """Locate where every source in a run's cached responses says its facility is.

    Coordinates and grid references are parsed; an address goes to the configured geocoder, unless
    source_locations.json already has its answer. A dropped map site is never located. The answers
    are kept in that file, so arbitration reads them and stays offline. Returns one result per
    source that states a location.
    """
    if sites is None:
        sites, _headers = resolve_sites(config, allow_database=False)
    sites_by_id = {normalize_scalar(site.get("site_id")): site for site in with_places(sites, config.run_dir)}
    if geocoder is _CONFIGURED:
        geocoder = make_geocoder(config.geocoder)
    geocoder = _CountingGeocoder(geocoder) if geocoder is not None else None
    path = config.run_dir / SOURCE_LOCATIONS_FILE
    cache = load_locations(path)
    states_path = config.run_dir / STATE_NAMES_FILE
    states = load_locations(states_path)
    by_site: dict[str, list[Path]] = {}
    for response in sorted(raw_dir(config.run_dir).glob("site_*.json")):
        by_site.setdefault(site_id_from_path(response), []).append(response)

    located: list[Located] = []
    failures: list[str] = []
    saved_at = 0
    try:
        for site_id, paths in sorted(by_site.items()):
            site = sites_by_id.get(site_id)
            if site is None:
                continue  # arbitration reports a site missing from the seed
            payloads = []
            for response in paths:
                try:
                    payloads.append(load_json(response))
                except (OSError, json.JSONDecodeError):
                    continue
            sources = merge_payloads(payloads)["sources"] if payloads else []
            # Our site's state and each source's, as the geocoder names them, to compare.
            try:
                for written in [site.get("admin1"), *(source.get("stated_state") for source in sources)]:
                    canonical_state(written, site.get("country_iso3"), states, geocoder, live=True)
            except GeocoderError as exc:
                failures.append(str(exc))
            for source in sources:
                if dropped_source_site(source.get("url")) or not states_location(source):
                    continue
                try:
                    located.append(locate_source(source, site, cache, geocoder, live=True, states=states))
                except GeocoderError as exc:
                    failures.append(str(exc))
                    continue
                if geocoder is not None and geocoder.calls - saved_at >= LOCATE_SAVE_EVERY:
                    save_locations(path, cache)
                    saved_at = geocoder.calls
    finally:
        save_locations(path, cache)
        save_locations(states_path, states)

    used = [item for item in located if item.basis in MATCHING_BASES]
    name = geocoder.name if geocoder is not None else "none configured"
    print(
        f"Locations: {len(located)} stated, {len(used)} usable ({describe_bases(located)}); "
        f"geocoder {name} -> {SOURCE_LOCATIONS_FILE}"
    )
    if failures:
        print(f"!! {len(failures)} address lookup(s) failed and are not cached; run locate again. First: {failures[0]}")
    return located


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

    selected, skipped = choose_sites(sites, config)
    # The run's list of sites skipped by name, for review and for Run_Config. Each search adds its
    # skips to the list and takes off the sites it selects or the run has already searched, so a
    # narrower search under the same run id doesn't shrink it and a site searched after all isn't
    # also counted as skipped. A search of listed ids (a retry, or the gas follow-up) only takes
    # its sites off a list that exists. Recorded before the check below, so a search whose every
    # site was skipped by name (one country of a batch, say) still lists them.
    if (selected or skipped) and (not config.site_ids or (config.run_dir / SKIPPED_SITES_FILE).exists()):
        searched = {site_id_from_path(path) for path in raw_dir(config.run_dir).glob("site_*.json")}
        searched |= {normalize_scalar(site.get("site_id")) for site in selected}
        record_skips(config.run_dir, skipped, searched)
    if not selected:
        hint = (
            f" This run is pinned to {run_seed_path(config)}; use a new --run-id to seed afresh."
            if reusing
            else ""
        )
        asked = {value.strip().upper() for value in config.iso3 or ()}
        if asked and asked <= COORDINATES_ONLY_ISO3:
            # Its sites match, but a coordinates-only country is asked only for coordinates, and a
            # source's coordinates never replace ours (the approve-sources plan), so nothing is left.
            raise NothingToSearch(
                f"Nothing to search in {', '.join(sorted(asked))}: it is scoped to coordinates alone, and a "
                "search no longer asks for a site's location, since a source's never replaces ours."
            )
        raise NothingToSearch(
            "No sites to search: none matched the selection filters, or every one that did has nothing "
            f"left to ask (counted above).{hint}"
        )

    raw_dir(config.run_dir).mkdir(parents=True, exist_ok=True)
    # Built before any search is paid for, so a geocoder configured wrongly stops the run here,
    # not after it.
    geocoder = make_geocoder(config.geocoder) if config.locate else None

    # The prompt asks the agent to search using municipality and admin names, so those fields
    # have to be populated or that instruction is asking for context we never supplied. A site
    # whose seed has neither is looked up from our coordinates, once, into the cache; the run keeps
    # what each prompt named, which arbitration compares with where each source puts the facility.
    if config.use_geocode_cache:
        selected = look_up_places(selected, config.geocode_cache, geocoder)
    selected = enrich_sites_from_cache(
        selected, cache_path=config.geocode_cache, enabled=config.use_geocode_cache
    )
    if config.use_geocode_cache:
        hits = sum(1 for site in selected if site.get("geocode_status") == "cache_hit")
        print(f"Geocode cache: {hits}/{len(selected)} sites enriched with admin context")
    # Each prompt names our other sites near it, so the agent can tell them apart. Their places are
    # looked up like our sites', and without the cache only their distance counts.
    selected = with_other_sites(
        selected, sites, config.geocode_cache if config.use_geocode_cache else None, geocoder
    )
    save_site_places(config.run_dir, selected)
    client = get_client(config)
    agent = agent_identity(client)

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
    log_path = config.run_dir / RUN_LOG_FILE
    executor = ThreadPoolExecutor(max_workers=workers)
    futures = []
    run_log: list[dict[str, Any]] = []

    def log(rows: list[dict[str, Any]]) -> None:
        # Each row is merged exactly once: merge_run_log adds Bing counts to the file's.
        if rows:
            write_csv_records(log_path, merge_run_log(log_path, rows), FOUNDRY_RUN_LOG_HEADERS)
            run_log.extend(rows)

    try:
        futures = [executor.submit(search_one, site, attributes) for site, attributes in work]
        for done, future in enumerate(as_completed(futures), start=1):
            row = future.result()
            # Logged as each site finishes, not at the end: a second Ctrl-C or a hard kill would
            # otherwise lose every finished site's row and Bing count.
            log([row])
            print(f"[{done}/{len(work)}] {row['site_id']} {row['site_name']}: {row['status']}", flush=True)
    except KeyboardInterrupt:
        print("\nStopping: sites not yet started are cancelled; waiting for those in flight.", flush=True)
        raise
    finally:
        # Sites in flight finish and are logged too, so an interrupted run keeps their Bing counts.
        executor.shutdown(wait=True, cancel_futures=True)
        logged = {id(row) for row in run_log}
        log([
            f.result()
            for f in futures
            if f.done() and not f.cancelled() and f.exception() is None and id(f.result()) not in logged
        ])

    # A search that raised (a timeout at the site limit, a 429) is a failure too: it saved nothing,
    # and only the first pass is searched again by `run`, so a second pass's would go unnoticed.
    failures = [row for row in run_log if str(row["status"]).startswith(("Search tool failed", "Failed"))]
    print(f"\nSearched {len(work)} sites. Run log: {log_path}")
    # The whole run log's cost, every pass and retry included, as Run_Config gives it: a resumed
    # pass's own rows would leave out what the passes before it spent.
    whole_log: list[dict[str, Any]] = []
    if log_path.exists():
        with log_path.open(newline="", encoding="utf-8-sig") as handle:
            whole_log = list(csv.DictReader(handle))
    if any(row.get("web_searches") for row in whole_log):
        print("The run so far, every pass and retry included:")
        for line in describe_run_cost(run_cost(whole_log)):
            print(line)

    # WP-533: verify cited URLs now, so arbitration can apply the verdicts offline. Every URL in
    # the run's cached responses is passed, but the sidecar is a cache, so a resumed search or the
    # pass-2 follow-up only fetches URLs not already checked.
    if config.check_links:
        check_run_links(config.run_dir, keep_text=config.keep_page_text)
    # Then where each source says its facility is. Like the link check, the cache means a resumed
    # search or the follow-up pass only looks up what is new.
    if config.locate:
        locate_run(config, sites, geocoder)
    if failures:
        ids = " ".join(row["site_id"] for row in failures)
        print(f"\n!! {len(failures)} site(s) still failing after retries. Try again later:")
        if pass_label == FOLLOW_UP_PASS:
            # `search` would ask the first pass's attributes of the pinned seed, and its answer would
            # stop `run` ever following these sites up.
            print(f"   delete their site_<id>{PASS_SEPARATOR}{pass_label}.json if present ({ids}), then run the batch's `run` command again")
        else:
            # Retried as the same pass: without its label, --force would replace the first pass's responses.
            label = f" --pass-label {pass_label}" if pass_label else ""
            print(f"   {agent_setting(client)}uv run waste-ai-search search --run-id {config.run_id}{label} --force --site-ids {ids}")
    return config.run_dir


def agent_setting(client: Any) -> str:
    """The agent a client asks, as the setting to put before a command that searches again, or ""
    where it has none (a fake). The agent may have been named in the shell rather than in .env, and
    a retry asking another would add that model's answer, and its tokens, to this pass's."""
    name = normalize_scalar(getattr(getattr(client, "config", None), "agent_name", ""))
    return f"AZURE_AI_AGENT_NAME={name} " if name else ""


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
    tool_calls: int | None = None
    usage: Usage | None = None

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
                tool_calls = add_tool_calls(tool_calls, client)
                usage = add_usage(usage, client)
                status, error = "Failed", f"{type(exc).__name__}: {exc}"
                print(f"    {site_id}: {error}", flush=True)
                break
            web_searches = add_searches(web_searches, client)
            tool_calls = add_tool_calls(tool_calls, client)
            usage = add_usage(usage, client)
            actions = getattr(client, "search_actions", None)
            if isinstance(actions, list):
                # What the agent searched for and opened, kept with what it answered, to study recall
                # and the search cap with. Nothing reads it back (payload.read_payload ignores it).
                payload[SEARCH_ACTIONS] = list(actions)

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
                status = f"Search tool failed after {max_attempts} attempt(s)"
                earlier = _usable_response(path)
                if earlier is None:
                    save_json(path, payload)
                else:
                    # --force on a site with a good response: a failed retry must not replace it,
                    # or its findings drop out of arbitration.
                    payload = earlier
                    status += "; kept the earlier response"
        if config.site_delay_seconds > 0:
            time.sleep(config.site_delay_seconds)

    # Read like arbitration reads it, so a response cached in the older attribute-first shape counts
    # the same way: attributes found, and sources (one per exact URL).
    read = read_payload(payload) if payload else None
    attribute_count = len({name for _source, name in found_attributes(read)}) if read else 0
    source_count = len(read["sources"]) if read else 0
    return {
        "run_id": config.run_id,
        "site_id": site_id,
        "pass_label": pass_label,
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
        "tool_calls": "" if tool_calls is None else tool_calls,
        "model": normalize_scalar(getattr(client, "model", "")) if usage is not None else "",
        **(usage.as_row() if usage is not None else {column: "" for column in TOKEN_COLUMNS}),
    }


def _usable_response(path: Path) -> dict[str, Any] | None:
    """A cached response worth keeping: readable, and not itself a search-tool failure."""
    if not path.exists():
        return None
    try:
        payload = load_json(path)
    except (OSError, json.JSONDecodeError):
        return None
    return None if search_tool_failed(payload) else payload


def merge_run_log(path: Path, new_rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Keep history across resumed and retried searches: one row per site and pass, the newest
    winning.

    A retried search only covers the sites it touched, so replacing the file would erase every
    other site from the log - which matters a great deal for a multi-day resumable run. Each pass
    has its own row (pass_label), so a second pass with another model is priced at that model, and
    retrying a pass replaces only its row. A log from before rows had a pass reads as the first's.

    `web_searches`, `tool_calls` and the token counts are the exception: they count every attempt,
    each of which is billed, so a row's count adds to its earlier one. A skipped site logs a blank
    count, which keeps the earlier one rather than erasing it, and its model.
    """
    merged: dict[tuple[str, str], dict[str, Any]] = {}
    if path.exists():
        with path.open(newline="", encoding="utf-8-sig") as handle:
            for row in csv.DictReader(handle):
                merged[_run_log_key(row)] = dict(row)
    for row in new_rows:
        key = _run_log_key(row)
        before = merged.get(key) or {}
        row = dict(row)
        for column in ("web_searches", "tool_calls", *TOKEN_COLUMNS):
            earlier = str(before.get(column) or "")
            if earlier.isdigit():
                row[column] = int(earlier) + int(row.get(column) or 0)
        if not row.get("model") and before.get("model"):
            row["model"] = before["model"]
        merged[key] = row
    return sorted(
        merged.values(),
        key=lambda row: (int(row["site_id"]) if str(row.get("site_id", "")).isdigit() else 0, _run_log_key(row)[1]),
    )


def _run_log_key(row: dict[str, Any]) -> tuple[str, str]:
    """A run-log row's site and pass: blank for the first pass, as in a log from before passes."""
    return normalize_scalar(row.get("site_id")), normalize_scalar(row.get("pass_label"))
