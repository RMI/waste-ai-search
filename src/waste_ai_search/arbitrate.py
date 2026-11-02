"""Phase 2: rebuild every output from the cached responses.

Pure and offline. No network, no database, no clock beyond the date passed in, so a rubric change
can be re-applied to a finished run for free and an interrupted search is never a partial result.
"""
from __future__ import annotations

import csv
import json
from datetime import date, datetime
from pathlib import Path
from typing import Any

from .accept_reviews import ACCEPTED_VALUES_FILE
from .costs import RUN_LOG_FILE, describe_run_cost, run_cost
from .credibility import assign_source_tier, dropped_source_site, tier_label
from .found_values import (
    DEAD_LINK,
    FOUND_VALUES_FILE,
    NO_LINK,
    NOT_USED,
    STATE_NAME,
    UNUSABLE,
    describe_sorting,
    pick_values,
    sort_values,
    unverified_rows,
)
from .geocoder import with_places
from .input_loader import describe_seed, resolve_sites, write_csv_records
from .link_check import BROKEN, LINK_CHECK_FILE, LinkResult, load_results
from .lists import (
    DATASETS_FOUND_FILE,
    DATASETS_FOUND_HEADERS,
    FOUND_BY_SME,
    NEW_FACILITY_FILE,
    NEW_FACILITY_HEADERS,
    POSSIBLE_DUPLICATES_FILE,
    POSSIBLE_DUPLICATES_HEADERS,
    datasets_found,
    possible_duplicates,
)
from .locate import (
    MATCHING_BASES,
    SOURCE_LOCATIONS_FILE,
    STATE_NAMES_FILE,
    Located,
    canonical_state,
    count_bases,
    describe_geocoders,
    format_coordinate,
    load_locations,
    locate_source,
    stated_address,
    stated_coordinates,
    states_location,
)
from .normalize_values import normalize_value
from .on_page import PageChecker, describe_results
from .payload import (
    citation_ids,
    citations,
    cited_source,
    is_empty,
    merge_payloads,
    normalize_url,
    payload_urls,
    read_payload,
    source_id_for,
)
from .prompt_builder import needs_contradiction_check
from .run_context import PipelineConfig, load_json, raw_dir, search_tool_failed, site_id_from_path
from .searchable_names import SKIPPED_NAME_CLASSES, describe_counts, recorded_skips, site_name_class
from .snapshot import recorded_run
from .schema import (
    canonical_attribute,
    NOT_A_WASTE_FACILITY,
    ACCEPTED_VALUES_HEADERS,
    FOUND_VALUES_HEADERS,
    IDENTITY_VALUES,
    PARSE_WARNING_HEADERS,
    SOURCES_HEADERS,
    VALUE_ATTRIBUTES,
    is_blank,
    normalize_scalar,
    parse_tristate_bool,
    validate_foundry_payload,
)
from .site_review import say_link_status, site_review_rows
from .workbook_io import write_review_workbook


# --- phase 2: arbitrate (pure, offline) -------------------------------------------------------
def extract_evidence(
    site: dict[str, Any],
    payload: dict[str, Any],
    run_id: str,
    dataset_version: str,
    access_date: date,
    link_results: dict[str, LinkResult] | None = None,
    keep_broken_links: bool = False,
    locations: dict[str, dict[str, Any]] | None = None,
) -> tuple[dict[str, list[dict[str, Any]]], list[dict[str, Any]], list[dict[str, Any]], list[str]]:
    """Turn one cached response, of either shape, into evidence: every value its sources give.

    Returns the values that can be weighed, by attribute; one evidence row per value, usable or not;
    one source entry per value, and one for a source that gives none, which also says the facility
    it names and where it places it; and the warnings.

    The response is read as sources, each with its values (payload.read_payload). `link_results`
    are the run's link-check verdicts (WP-533), keyed by the URL as the agent wrote it. A repaired
    URL replaces the one the agent wrote; the original stays in the raw response. A value whose
    source has no link, or a Broken one, can't be used, unless keep_broken_links is set. Unverified
    links are never touched. Nor can a value that is empty, or that could not be converted or mapped.

    A citation of a dropped map site (DROPPED_SOURCE_SITES) is skipped before anything else, and
    counted in the warnings, one line per map site, with any of the agent's unverified_leads that
    point at one. An older response's name and coordinates given as values are skipped too: a
    source's name and location only check that it is about the site searched.

    Each source's stated location is read into a point (locate.py), with geocoded addresses taken
    from `locations`, the run's source_locations.json; nothing here reaches the network. Every row
    of a source carries the facility it describes and that point, and its number in the response
    (source_index), by which found_values.py pairs it with the site searched.
    """
    link_results = link_results or {}
    site_id = normalize_scalar(site.get("site_id"))
    by_attribute: dict[str, list[dict[str, Any]]] = {}
    evidence_rows: list[dict[str, Any]] = []
    source_entries: list[dict[str, Any]] = []
    warnings: list[str] = []
    dropped: dict[str, int] = {}
    only_dropped: set[str] = set()
    kept_attributes: set[str] = set()
    order = 0
    read = read_payload(payload)
    located: dict[int, Located] = {}

    for source_index, source in enumerate(read["sources"], start=1):
        # A cached response from before a rename still carries the old attribute name.
        names = {canonical_attribute(normalize_scalar(value.get("attribute_name"))) for value in source["values"]}
        mirror = dropped_source_site(source.get("url"))
        if mirror:
            dropped[mirror] = dropped.get(mirror, 0) + max(1, len(source["values"]))
            only_dropped.update(names)
        else:
            kept_attributes.update(names)
            located[source_index] = locate_source(source, site, locations)

    def link_for(source: dict[str, Any]) -> tuple[str, str, bool, LinkResult | None]:
        """The URL to file a source under, its link verdict, and whether the link is dead."""
        raw_url = normalize_scalar(source.get("url"))
        link = link_results.get(raw_url)
        link_status = link.verdict if link else ""
        return (link.resolved_url if link else raw_url), link_status, link_status == BROKEN and not keep_broken_links, link

    cited_indexes: set[int] = set()
    for source_index, page, value_index, attribute in citations(read):
        if source_index not in located:
            continue  # a dropped map site
        # A response cached attribute-first keeps how each citation described its page.
        source = cited_source(page, attribute)
        name = canonical_attribute(normalize_scalar(attribute.get("attribute_name")))
        if name in IDENTITY_VALUES:
            continue
        if name not in VALUE_ATTRIBUTES:
            warnings.append(f"Unsupported attribute_name {name!r} dropped.")
            continue
        url, link_status, dead_link, link = link_for(source)
        # The evidence id stays per citation; the source id is the exact URL's, shared by every
        # citation of it, so found_values.csv joins sources.csv.
        evidence_id, fallback_source_id = citation_ids(site_id, source_index, value_index, attribute)
        source_id = source_id_for(url, fallback=fallback_source_id)

        normal = normalize_value(name, attribute.get("value"), attribute.get("unit"))
        conversion, value, mapping_note, numeric_note = normal.conversion, normal.value, normal.mapping_note, normal.numeric_note
        warnings.extend(normal.warnings)

        order += 1
        tier, rule = assign_source_tier(source, name)

        item = {
            "evidence_id": evidence_id,
            "source_id": source_id,
            "source_index": source_index,
            "attribute_name": name,
            "value": value,
            "unit": conversion.unit,
            "tier": tier,
            "order": order,
            "url": url,
            "link_status": link_status,
            "value_date": normalize_scalar(attribute.get("value_date")),
            "confidence": normalize_scalar(attribute.get("confidence_score")),
            "evidence_summary": normalize_scalar(attribute.get("evidence_summary")),
        }
        if url and not dead_link and not is_blank(value) and not numeric_note:
            by_attribute.setdefault(name, []).append(item)

        exclusion, excluded = "", ""
        if not url:
            exclusion, excluded = "No clickable source URL.", NO_LINK
        elif dead_link:
            exclusion, excluded = f"Source link broken ({link.status}).", DEAD_LINK
        elif is_blank(value):
            exclusion, excluded = "Value empty after normalization.", UNUSABLE
        elif numeric_note:
            exclusion, excluded = numeric_note, UNUSABLE

        evidence_rows.append(
            {
                "evidence_id": evidence_id,
                "run_id": run_id,
                "dataset_version": dataset_version,
                "site_id": site_id,
                "internal_facility_id": normalize_scalar(site.get("internal_facility_id") or site_id),
                "attribute_name": name,
                "claimed_value": conversion.original_value,
                "claimed_unit": conversion.original_unit,
                "normalized_value": conversion.value,
                "normalized_unit": conversion.unit,
                "unit_conversion_note": conversion.note,
                "mapped_value": normalize_scalar(value),
                "mapping_note": mapping_note,
                "value_basis": normalize_scalar(attribute.get("value_basis")),
                "value_date": normalize_scalar(attribute.get("value_date")),
                "agent_confidence": normalize_scalar(attribute.get("confidence_score")),
                "source_id": source_id,
                "link_status": link_status,
                "url": url,
                "source_tier": tier_label(tier),
                "agent_proposed_tier": normalize_scalar(source.get("source_tier")),
                "tier_rule_applied": rule,
                "evidence_summary": normalize_scalar(attribute.get("evidence_summary")),
                "quoted_evidence_short": normalize_scalar(attribute.get("quoted_evidence_short")),
                # A closing_year the source gives as planned or expected, not actual.
                "planned": "TRUE" if name == "closing_year" and parse_tristate_bool(attribute.get("planned"))[0] else "",
                # An incoming waste amount the source says is mainly other waste than municipal.
                "not_municipal": (
                    "TRUE" if name == "annual_incoming_waste_metric_tonnes"
                    and parse_tristate_bool(attribute.get("not_municipal"))[0] else ""
                ),
                "search_terms_used": normalize_scalar(attribute.get("search_terms_used")),
                # Whether the value can be used at all: its status is decided in found_values.py.
                "promotion_eligible": "FALSE" if exclusion else "TRUE",
                "exclusion_reason": exclusion,
                "_excluded": excluded,
                "record_created_date": access_date.isoformat(),
                **location_columns(source, located[source_index]),
                "source_index": source_index,
            }
        )
        source_entries.append(
            source_entry(source, source_id, url, tier, rule, run_id, site_id, source_index, located[source_index])
        )
        cited_indexes.add(source_index)

    # A source that gives no value still names a facility and may place it: it is listed in
    # sources.csv, and counts for datasets_found.csv and possible_duplicates.csv.
    for source_index, place in located.items():
        if source_index in cited_indexes:
            continue
        source = cited_source(read["sources"][source_index - 1])
        url, _link_status, _dead_link, _link = link_for(source)
        source_id = source_id_for(url, fallback=f"WAISRC-{site_id}-S{source_index:03d}")
        tier, rule = assign_source_tier(source)
        source_entries.append(source_entry(source, source_id, url, tier, rule, run_id, site_id, source_index, place))

    for name in sorted(only_dropped - kept_attributes):
        if name in VALUE_ATTRIBUTES:
            warnings.append(f"{name}: every source was a dropped map site; not used.")

    # The agent's own leads are listed in found_values.csv (payload_leads), which skips the same map
    # sites, so they are counted with the rest.
    for lead in read["unverified_leads"]:
        mirror = dropped_source_site(lead_part(lead, "url")) if isinstance(lead, dict) else ""
        if mirror:
            dropped[mirror] = dropped.get(mirror, 0) + 1

    for mirror, count in sorted(dropped.items()):
        warnings.append(
            f"Dropped {count} citation(s) of {mirror}: map sites that copy OpenStreetMap or scrape "
            "listings are not used as sources."
        )
    # A value cited by several sources is checked once per source; one warning says it.
    return by_attribute, evidence_rows, source_entries, list(dict.fromkeys(warnings))


def location_columns(source: dict[str, Any], place: Located) -> dict[str, Any]:
    """The facility a source describes, as it writes it - its name, address and coordinates - and the
    point read from them."""
    stated = states_location(source)
    return {
        "facility_name_in_source": normalize_scalar(source.get("facility_name_in_source")),
        "facility_name_english": normalize_scalar(source.get("facility_name_english")),
        "stated_address": stated_address(source),
        "stated_coordinates": stated_coordinates(source),
        # The municipality and state the source puts the facility in, as the agent gives them.
        "stated_municipality": normalize_scalar(source.get("stated_municipality")),
        "stated_state": normalize_scalar(source.get("stated_state")),
        "location_basis": place.basis if stated else "",
        "located_latitude": format_coordinate(place.latitude) if stated else "",
        "located_longitude": format_coordinate(place.longitude) if stated else "",
        "location_reason": place.reason if stated else "",
        # Not a column: what the 5 km check measures where there is no point to use.
        "rough_points": place.rough_points() if stated else (),
    }


def describe_located(bases: list[str]) -> str:
    """A count with its bases, for Run_Config: "12 (coordinates 9, street 3)", or "0"."""
    return f"{len(bases):,} ({count_bases(bases)})" if bases else "0"


def source_entry(
    source: dict[str, Any], source_id: str, url: str, tier: int, rule: str, run_id: str, site_id: str,
    source_index: int, place: Located,
) -> dict[str, Any]:
    """One citation's row for sources.csv, before dedupe_sources makes it one row per URL. It also
    says the facility the source names and where it places it, which sources.csv leaves out."""
    return {
        "source_id": source_id,
        "url_normalized": normalize_url(url),
        "url": url,
        "source_title": normalize_scalar(source.get("source_title")),
        "publisher": normalize_scalar(source.get("publisher")),
        "source_type": normalize_scalar(source.get("source_type")),
        "source_tier": tier_label(tier),
        "tier_rule_applied": rule,
        "agent_proposed_tier": normalize_scalar(source.get("source_tier")),
        "publication_date": normalize_scalar(source.get("publication_date")),
        "has_publication_date": "TRUE" if normalize_scalar(source.get("publication_date")) else "FALSE",
        "language": normalize_scalar(source.get("language")),
        "paywall_flag": "TRUE" if source.get("paywall_flag") else "FALSE",
        "subscription_followup_needed": "TRUE" if source.get("subscription_followup_needed") else "FALSE",
        "first_seen_run_id": run_id,
        "site_id": site_id,
        "source_index": source_index,
        **location_columns(source, place),
    }


def dedupe_sources(entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Collapse the per-citation source entries into one row per exact URL.

    The source_id is derived from the exact URL, so it is the key. The first citation's title,
    publisher and tier stand for the row.
    """
    grouped: dict[str, dict[str, Any]] = {}
    for entry in entries:
        key = entry["source_id"]
        row = grouped.get(key)
        if row is None:
            row = {header: entry.get(header, "") for header in SOURCES_HEADERS}
            row["times_cited"] = 0
            row["_sites"] = set()
            grouped[key] = row
        row["times_cited"] += 1
        row["_sites"].add(entry["site_id"])

    out: list[dict[str, Any]] = []
    for row in grouped.values():
        row["cited_site_count"] = len(row.pop("_sites"))
        out.append(row)
    out.sort(key=lambda item: (-int(item["times_cited"]), str(item["url_normalized"])))
    return out


def cached_source_urls(run_dir: Path) -> list[str]:
    """Every source URL in a run's cached responses, read the way arbitration reads them.

    URLs are returned as the agent wrote them, since that is what gets fetched and what the
    verdicts are keyed by. A dropped map site is never used, so it is not fetched either.
    """
    urls: list[str] = []
    for path in sorted(raw_dir(run_dir).glob("site_*.json")):
        try:
            payload = load_json(path)
        except (OSError, json.JSONDecodeError):
            continue
        urls.extend(url for url in payload_urls(payload) if not dropped_source_site(url))
    return list(dict.fromkeys(urls))


def attach_site_names(rows: list[dict[str, Any]], sites_by_id: dict[str, dict[str, Any]]) -> None:
    """Stamp each row with the site's translated name, its own spelling and the source language.

    `site_name` is only filled where a row lacks one: a row built from the site already carries
    it, and must not be overwritten.
    """
    for row in rows:
        site = sites_by_id.get(normalize_scalar(row.get("site_id")), {})
        if not normalize_scalar(row.get("site_name")):
            row["site_name"] = normalize_scalar(site.get("site_name"))
        row["original_site_name"] = normalize_scalar(site.get("original_site_name"))
        row["source_language"] = normalize_scalar(site.get("source_language"))


def cover_consistency_warnings(found: list[dict[str, Any]]) -> list[str]:
    """Flag a has_biocover answer that contradicts the cover types found alongside it.

    They are searched independently and describe the same physical cover, so a disagreement means
    one of the two is wrong. Surfaced rather than auto-corrected: which one is wrong depends on
    the sources, and the reviewer has both values in front of them. `found` is one site's values,
    one per attribute and year, with the newest year of a yearly attribute first, and that is the
    one compared.
    """
    values: dict[str, str] = {}
    for row in found:
        values.setdefault(normalize_scalar(row.get("attribute_name")), normalize_scalar(row.get("value")))
    biocover = values.get("has_biocover", "")
    cover_types = values.get("cover_types", "")
    if not biocover or not cover_types:
        return []

    has_organic = "organic cover" in cover_types.lower()
    if biocover == "TRUE" and not has_organic:
        return [
            f"has_biocover is TRUE but cover_types is {cover_types!r} with no organic cover; "
            "one of the two is wrong."
        ]
    if biocover == "FALSE" and has_organic:
        return [
            f"has_biocover is FALSE but cover_types includes organic cover ({cover_types!r}); "
            "one of the two is wrong."
        ]
    return []


def read_listed_candidates(path: Path) -> list[dict[str, str]]:
    """The new-facility candidates SMEs added to a run's list, which arbitrating again keeps.
    Arbitration lists none of its own since the matcher went."""
    if not path.exists():
        return []
    with path.open(newline="", encoding="utf-8-sig") as handle:
        return [dict(row) for row in csv.DictReader(handle) if row.get("found_by") == FOUND_BY_SME]


# The parts of an unverified lead, and the keys each may come under. The prompt's template names the
# first of each; responses from before it named them as the agent chose, in 23 shapes in round 2 of
# the Mexico tests, where reading only the first lost the value and the note of 157 of 174 leads. The
# reason keys say why the agent didn't stand behind it; the rest of its text is the note. gpt-5.4 also
# wrote leads as whole sources, its reason as their "issue" and its values in a list (lead_values),
# each with its own note.
LEAD_KEYS = {
    "url": ("url", "source_url", "supporting_url"),
    "attribute_name": ("attribute_name",),
    "value": ("value", "candidate_value"),
    "unit": ("unit",),
    "value_date": ("value_date",),
    "quote": ("quoted_evidence_short",),
    "why_unverified": ("why_unverified", "reason_unverified", "reason", "issue"),
    "lead_summary": ("lead_summary", "summary", "note", "lead", "finding", "description", "evidence_summary"),
}


def lead_part(lead: dict[str, Any], part: str) -> str:
    """One part of an unverified lead (LEAD_KEYS), under whichever of its keys the agent used."""
    return next((text for key in LEAD_KEYS[part] if (text := normalize_scalar(lead.get(key)))), "")


def lead_values(lead: dict[str, Any]) -> list[dict[str, Any]]:
    """A lead's values, one lead each: a lead written as a whole source gives each value in its
    values list with the source's link (4005's DigitalMex closure gave three); any other lead is one."""
    values = lead.get("values")
    values = [value for value in values if isinstance(value, dict)] if isinstance(values, list) else []
    return [{**lead, **value} for value in values] or [lead]


def payload_leads(
    cached: list[Path],
    sites_by_id: dict[str, dict[str, Any]],
    run_id: str,
) -> list[dict[str, Any]]:
    """The agent's own unverified_leads, which found_values.csv lists as not used: each with its parts
    (LEAD_KEYS), however the response named them, and one per value for a lead written as a whole
    source (lead_values). A lead given as text only is all note.

    A lead pointing at a dropped map site is skipped, like a citation of one; extract_evidence
    counts it in the site's parse warnings.
    """
    out: list[dict[str, Any]] = []
    for path in cached:
        site_id = site_id_from_path(path)
        site = sites_by_id.get(site_id, {})
        try:
            payload = read_payload(load_json(path))
        except (OSError, json.JSONDecodeError):
            continue
        for lead in payload["unverified_leads"]:
            if isinstance(lead, dict):
                if dropped_source_site(lead_part(lead, "url")):
                    continue
                entries = [{part: lead_part(value, part) for part in LEAD_KEYS} for value in lead_values(lead)]
            else:
                entries = [{**{part: "" for part in LEAD_KEYS}, "lead_summary": normalize_scalar(lead)}]
            out.extend(
                {"run_id": run_id, "site_id": site_id, "site_name": normalize_scalar(site.get("site_name")), **parts}
                for parts in entries
            )
    return out


# What arbitration once wrote and no longer does, which an earlier arbitration may have left in the
# run folder: until build step 3 of the source-matching plan, a standardized table and the .load.sql
# beside it, both built from values accepted before review; until step 2 of the approve-sources
# plan, the evidence, the decisions, the review queue and the leads, which found_values.csv replaces,
# and the sources matched outside the run, since no source is matched now.
RETIRED_OUTPUTS = (
    "std_facility_tbl_ai_search_*.csv", "std_facility_tbl_ai_search_*.load.sql",
    "evidence.csv", "resolved.csv", "review_queue.csv", "supplementary_leads.csv", "outside_run_matches.csv",
)


def remove_retired_outputs(run_dir: Path) -> list[Path]:
    """Delete the files an earlier arbitration left in the run folder that this one no longer writes.

    Left beside this arbitration's outputs they would look current: a standardized table would look
    ready to load, and an old review queue ready to review. Only the local copies go: a copy pushed
    to blob stays there as the record of that arbitration, and comes back down on the next pull, so
    this runs on every arbitration, and after accept-reviews' pull.
    """
    removed = []
    for pattern in RETIRED_OUTPUTS:
        for path in sorted(run_dir.glob(pattern)):
            path.unlink()
            removed.append(path)
    if removed:
        names = ", ".join(path.name for path in removed)
        print(
            f"Removed {names}: an earlier arbitration wrote them, and this one doesn't "
            f"({FOUND_VALUES_FILE} holds every value found). Any copy in blob is kept."
        )
    return removed


def remove_stale_acceptance(run_dir: Path) -> bool:
    """Empty the accepted_values.csv accept-reviews wrote from the found values this arbitration sorts
    again, leaving its header row.

    Arbitrating rewrites found_values.csv with its sme_decision and loaded blank, and may sort a value
    differently - a link found dead since, say - so an accepted_values.csv left beside it would load
    what the run no longer says. accept-reviews writes it again from the same reviewed workbook, as
    review_id is the same each time.

    The file is emptied rather than deleted because push never deletes a blob: a deleted file's copy
    in blob would stay, and the next pull would bring the old values back to this machine or any
    other. The emptied file is newer, so the push after arbitrating replaces that copy.
    """
    path = run_dir / ACCEPTED_VALUES_FILE
    if not path.exists():
        return False
    with path.open(newline="", encoding="utf-8-sig") as handle:
        if len(list(csv.reader(handle))) <= 1:
            return False  # emptied already, or never written to
    write_csv_records(path, [], ACCEPTED_VALUES_HEADERS)
    print(
        f"Emptied {ACCEPTED_VALUES_FILE}: it was written from the found values this arbitration sorts again. "
        "Run accept-reviews again to load the approved values."
    )
    return True


def run_arbitration(config: PipelineConfig) -> dict[str, Path]:
    # One clock for the whole run: the access date, Run_Config's arbitrated_at, and the year an
    # undated value is given, so a value's year is the run's wherever it is read.
    arbitrated_at = datetime.now().replace(microsecond=0)
    access_date = arbitrated_at.date()
    run_year = arbitrated_at.year
    # Checked before the seed, so a mistyped --run-id says so rather than reaching for a database.
    cached = sorted(raw_dir(config.run_dir).glob("site_*.json"))
    if not cached:
        raise ValueError(f"No cached responses in {raw_dir(config.run_dir)}. Run the search phase first.")
    sites, _headers = resolve_sites(config, allow_database=False)
    # With the municipality and state each prompt named, and every state as the geocoder names it
    # (state_names.json, from the locate step), so where a source puts the facility can be compared
    # with where our site is.
    sites = with_places(sites, config.run_dir)
    states = load_locations(config.run_dir / STATE_NAMES_FILE)
    for site in sites:
        site[STATE_NAME] = canonical_state(site.get("admin1"), site.get("country_iso3"), states, None, live=False)
    sites_by_id = {normalize_scalar(site.get("site_id")): site for site in sites}

    # Read only: the search phase did the fetching and the geocoding, so arbitration stays offline.
    link_results = load_results(config.run_dir / LINK_CHECK_FILE)
    locations = load_locations(config.run_dir / SOURCE_LOCATIONS_FILE)
    # The text the link check kept of each page that loaded: each quote and stated location is
    # checked against it, and one that isn't there is flagged for the SME (on_page.py).
    pages = PageChecker(
        config.run_dir,
        {result.text_file: result.text_note for result in link_results.values() if result.text_file},
        {result.text_file: result.text_kind for result in link_results.values() if result.text_file},
    )

    # A site may have several cached responses - one per pass - and all of them count as evidence.
    by_site: dict[str, list[Path]] = {}
    for path in cached:
        by_site.setdefault(site_id_from_path(path), []).append(path)

    evidence_rows: list[dict[str, Any]] = []
    source_entries: list[dict[str, Any]] = []
    parse_warnings: list[dict[str, Any]] = []
    retry_sites: list[dict[str, Any]] = []

    def warn(site_id: str, site: dict[str, Any], warning: str) -> None:
        parse_warnings.append(
            {"run_id": config.run_id, "site_id": site_id, "site_name": normalize_scalar(site.get("site_name")), "warning": warning}
        )

    for site_id, paths in sorted(by_site.items()):
        site = sites_by_id.get(site_id)
        if site is None:
            warn(site_id, {}, "Site not in the seed.")
            continue

        payloads = []
        for path in paths:
            try:
                payloads.append(load_json(path))
            except (OSError, json.JSONDecodeError) as exc:
                warn(site_id, site, f"Unreadable cached response {path.name}: {exc}")
        if not payloads:
            continue
        payload = merge_payloads(payloads)

        if is_empty(payload) and search_tool_failed(payload):
            retry_sites.append(
                {
                    "site_id": site_id,
                    "site_name": normalize_scalar(site.get("site_name")),
                    "country_iso3": normalize_scalar(site.get("country_iso3")),
                    "reason": "Agent reported its web-search tool failed; no search was performed.",
                    "search_notes": normalize_scalar(payload.get("search_notes"))[:400],
                }
            )
            warn(site_id, site, "Search tool failure reported by the agent; retry this site.")

        for warning in validate_foundry_payload(payload, normalize_scalar(site.get("country_iso3"))):
            warn(site_id, site, warning)

        _by_attribute, site_evidence, site_sources, warnings = extract_evidence(
            site, payload, config.run_id, config.dataset_version, access_date,
            link_results=link_results, keep_broken_links=config.keep_broken_links, locations=locations,
        )
        for row in site_sources:
            row[STATE_NAME] = canonical_state(row.get("stated_state"), site.get("country_iso3"), states, None, live=False)
        source_entries.extend(site_sources)
        for warning in warnings:
            warn(site_id, site, warning)
        for row in site_evidence:
            pages.check(row)
        evidence_rows.extend(site_evidence)

    # Every source each search cited, with or without values: the first entry of each. Each is filed
    # under the site searched, and each value sorted by its site-source pair (found_values.py).
    cited: dict[tuple[str, int], dict[str, Any]] = {}
    for entry in source_entries:
        cited.setdefault((entry["site_id"], entry["source_index"]), entry)
    citations = list(cited.values())
    sorting = sort_values(evidence_rows, citations, sites_by_id, run_year)
    found = sorting.rows + unverified_rows(payload_leads(cached, sites_by_id, config.run_id), sites_by_id, run_year)
    attach_site_names(found, sites_by_id)

    # Checked on the values that would load, one per site, attribute and year, newest year first.
    picked = sorted(pick_values(found).values(), key=lambda row: (row["site_id"], -int(row["year"])))
    by_site_picked: dict[str, list[dict[str, Any]]] = {}
    for row in picked:
        by_site_picked.setdefault(row["site_id"], []).append(row)
    for site_id, rows in sorted(by_site_picked.items()):
        for warning in cover_consistency_warnings(rows):
            warn(site_id, sites_by_id.get(site_id, {}), warning)

    not_a_waste_facility = sum(
        1 for row in found
        if row["attribute_name"] == "facility_type" and row["value"] == NOT_A_WASTE_FACILITY and row["status"] != NOT_USED
    )
    # How many arbitrated facilities were offered the verdict at all, so a count of zero can be read
    # as "checked N, contradicted none" rather than "never checked".
    contradiction_checks = sum(
        1 for sid in by_site if sid in sites_by_id and needs_contradiction_check(sites_by_id[sid])
    )
    # Sites searched although their name gives a search nothing to look for: listed by id, or
    # searched with --include-unsearchable-names. Beside the count skipped, it shows which way the
    # name rule ran.
    unsearchable_searched = describe_counts([
        cls
        for sid in by_site
        if sid in sites_by_id and (cls := site_name_class(sites_by_id[sid])) in SKIPPED_NAME_CLASSES
    ])
    # What each source that states a location was read as, once per search's citation of it.
    stated_bases = [citation["location_basis"] for citation in citations if citation["location_basis"]]

    out = config.run_dir
    remove_retired_outputs(out)
    remove_stale_acceptance(out)
    paths = {
        "found_values": out / FOUND_VALUES_FILE,
        "sources": out / "sources.csv",
        "warnings": out / "parse_warnings.csv",
        "retry": out / "sites_to_retry.csv",
        "new_facilities": out / NEW_FACILITY_FILE,
        "possible_duplicates": out / POSSIBLE_DUPLICATES_FILE,
        "datasets_found": out / DATASETS_FOUND_FILE,
    }
    write_csv_records(paths["found_values"], found, FOUND_VALUES_HEADERS)
    sources = dedupe_sources(source_entries)
    write_csv_records(paths["sources"], sources, SOURCES_HEADERS)
    write_csv_records(paths["warnings"], parse_warnings, PARSE_WARNING_HEADERS)
    write_csv_records(
        paths["retry"], retry_sites, ["site_id", "site_name", "country_iso3", "reason", "search_notes"]
    )
    # For ingestion, which has no path for either: landfills none of our sites is, which only an
    # SME's decision lists now, so arbitrating again keeps theirs and drops any an earlier
    # arbitration listed; and pairs of our sites cited under the same page and name.
    new_facilities = read_listed_candidates(paths["new_facilities"])
    write_csv_records(paths["new_facilities"], new_facilities, NEW_FACILITY_HEADERS)
    duplicates = possible_duplicates(sorting.shared, sites_by_id)
    write_csv_records(paths["possible_duplicates"], duplicates, POSSIBLE_DUPLICATES_HEADERS)
    # Pages that look like datasets, better loaded once through ingestion than found again by every
    # search that needs one of their rows.
    datasets = datasets_found(citations, sources)
    write_csv_records(paths["datasets_found"], datasets, DATASETS_FOUND_HEADERS)

    sorted_settings = describe_sorting(found, sorting.pairs)
    paths["workbook"] = write_review_workbook(
        out / f"{config.run_id}_review.xlsx",
        run_config=[
            {"setting": "run_id", "value": config.run_id},
            {"setting": "dataset_version", "value": config.dataset_version},
            {
                "setting": "seed_source",
                "value": describe_seed(config),
            },
            {"setting": "consolidation_run", "value": recorded_run(config.run_dir)},
            # Sites, not responses: a site searched in two passes has two.
            {"setting": "sites_arbitrated", "value": len(by_site)},
            {"setting": "unsearchable_names_skipped", "value": recorded_skips(config.run_dir, set(by_site))},
            {"setting": "unsearchable_names_searched", "value": unsearchable_searched},
            {"setting": "arbitrated_at", "value": arbitrated_at.isoformat()},
            *sorted_settings,
            {"setting": "links_checked", "value": len(link_results)},
            {"setting": "links_broken", "value": sum(1 for r in link_results.values() if r.verdict == BROKEN)},
            {"setting": "links_repaired", "value": sum(1 for r in link_results.values() if r.resolved_url != r.url)},
            {
                "setting": "broken_link_route",
                "value": "kept usable" if config.keep_broken_links else "not used",
            },
            {"setting": "locations_stated", "value": describe_located(stated_bases)},
            {"setting": "locations_used", "value": describe_located([b for b in stated_bases if b in MATCHING_BASES])},
            {
                "setting": "geocoder",
                "value": describe_geocoders(locations, (config.run_dir / SOURCE_LOCATIONS_FILE).exists()),
            },
            {"setting": "contradiction_checks", "value": contradiction_checks},
            {"setting": "not_a_waste_facility", "value": not_a_waste_facility},
            {"setting": "possible_duplicates", "value": len(duplicates)},
            {"setting": "new_facility_candidates", "value": len(new_facilities)},
            {"setting": "datasets_found", "value": len(datasets)},
            *cost_settings(config.run_dir),
            # Counted by file: two spellings of one URL share one.
            {"setting": "page_text_kept", "value": len({result.text_file for result in link_results.values() if result.text_file})},
            {"setting": "quotes_on_page", "value": describe_results([row.get("quote_on_page", "") for row in found])},
            {"setting": "locations_on_page", "value": describe_results(stated_on_page(evidence_rows))},
        ],
        # Where a page couldn't be checked against, its link_status says why.
        site_review=say_link_status(site_review_rows(found, sorting.pairs, sites_by_id), pages.link_words(link_results)),
        parse_warnings=parse_warnings,
    )

    print_summary(by_site, found, sorted_settings, parse_warnings, retry_sites)
    return paths


def cost_settings(run_dir: Path) -> list[dict[str, Any]]:
    """What the run's searches used and cost, every pass and retry included, from its run log."""
    path = run_dir / RUN_LOG_FILE
    if not path.exists():
        return []
    with path.open(newline="", encoding="utf-8-sig") as handle:
        cost = run_cost(list(csv.DictReader(handle)))
    return [{"setting": "search_cost", "value": " ".join(describe_run_cost(cost))}]


def stated_on_page(evidence_rows: list[dict[str, Any]]) -> list[str]:
    """Whether each source's stated location is on its page, once per source as each search cited
    it: every row of a source carries the same location."""
    seen: dict[tuple[str, Any], str] = {}
    for row in evidence_rows:
        if row.get("location_on_page"):
            seen.setdefault((row.get("site_id"), row.get("source_index")), row["location_on_page"])
    return list(seen.values())


def print_summary(cached, found, sorted_settings, warnings, retry_sites) -> None:
    from collections import Counter

    tiers = Counter(row["source_tier"] for row in found if row.get("source_tier"))
    print(f"\nSites arbitrated:        {len(cached)}")
    print(f"Values found:            {len(found)}")
    print(f"Parse warnings:          {len(warnings)}")
    if retry_sites:
        ids = " ".join(row["site_id"] for row in retry_sites)
        print(
            f"\n!! {len(retry_sites)} site(s) could not be searched - the agent's search tool failed."
        )
        print("   These are NOT 'no data found'. Retry them:")
        print(f"   uv run waste-ai-search search --run-id <run> --force --site-ids {ids}")
    print("\nSorted, by site-source pair:")
    for setting in sorted_settings:
        print(f"  {setting['setting']:<20} {setting['value']}")
    print("\nValues by tier:")
    for name, count in sorted(tiers.items()):
        print(f"  {count:>5}  {name}")
