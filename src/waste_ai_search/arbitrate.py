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

from .arbitration import needs_review, resolve, routed_to_leads, site_for_year, value_year
from .costs import RUN_LOG_FILE, describe_run_cost, run_cost
from .credibility import (
    assign_source_tier,
    dropped_source_site,
    is_promotable,
    tier_label,
    trusted_baseline_name,
)
from .input_loader import describe_seed, resolve_sites, write_csv_records
from .link_check import BROKEN, LINK_CHECK_FILE, LinkResult, load_results
from .locate import (
    HINT,
    MATCHING_BASES,
    SOURCE_LOCATIONS_FILE,
    STREET,
    VALUE_BASES,
    Located,
    count_bases,
    describe_geocoders,
    format_coordinate,
    load_locations,
    locate_source,
    states_location,
)
from .match import (
    FOUND_BY_SME,
    LOCATED,
    MOVED_BY_NAME,
    NAME_NEARBY,
    NAMED_AMONG_SEVERAL,
    NEW_FACILITY,
    DATASETS_FOUND_FILE,
    DATASETS_FOUND_HEADERS,
    NEW_FACILITY_FILE,
    NEW_FACILITY_HEADERS,
    NO_LOCATION,
    NOT_A_FACILITY,
    OUTSIDE_RUN,
    OUTSIDE_RUN_FILE,
    OUTSIDE_RUN_HEADERS,
    POSSIBLE_DUPLICATES_FILE,
    POSSIBLE_DUPLICATES_HEADERS,
    TIE,
    Citation,
    Filing,
    Matching,
    datasets_found,
    match_sources,
    run_selection,
)
from .on_page import NOT_FOUND, PARTLY, PageChecker, describe_results
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
from .prompt_builder import asks_location, needs_contradiction_check, requested_attributes
from .run_context import PipelineConfig, load_json, raw_dir, search_tool_failed, site_id_from_path
from .searchable_names import SKIPPED_NAME_CLASSES, describe_counts, recorded_skips, site_name_class
from .snapshot import recorded_run
from .schema import (
    canonical_attribute,
    NOT_A_WASTE_FACILITY,
    ARRAY_TARGET_ATTRIBUTES,
    ATTRIBUTE_RANGES,
    BOOLEAN_TARGET_ATTRIBUTES,
    ENUM_MAPS,
    EVIDENCE_HEADERS,
    LOCATION_ATTRIBUTE,
    NOTHING_TO_REVIEW,
    NUMERIC_TARGET_ATTRIBUTES,
    PARSE_WARNING_HEADERS,
    RESOLVED_HEADERS,
    REVIEW_QUEUE_HEADERS,
    SOURCES_HEADERS,
    SUPPLEMENTARY_LEADS_HEADERS,
    TIME_VARYING_ATTRIBUTES,
    VALUE_ATTRIBUTES,
    bucket_waste_depth,
    is_blank,
    map_enum_value,
    normalize_scalar,
    parse_tristate_bool,
    validate_foundry_payload,
)
from .unit_converter import convert_attribute_value
from .workbook_io import write_review_workbook


MAX_CONFIRMATION_EVIDENCE_ROWS = 2


def parse_float(value: Any) -> float | None:
    text = normalize_scalar(value).replace(",", "")
    if not text:
        return None
    try:
        return float(text)
    except ValueError:
        return None


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
    """Turn one cached response, of either shape, into evidence items grouped by attribute.

    The response is read as sources, each with its values (payload.read_payload). `link_results`
    are the run's link-check verdicts (WP-533), keyed by the URL as the agent wrote it. A repaired
    URL replaces the one the agent wrote; the original stays in the raw response. A Broken link is
    treated like a missing one - the source cannot be promoted and goes to leads - unless
    keep_broken_links is set. Unverified links are never touched.

    A citation of a dropped map site (DROPPED_SOURCE_SITES) is skipped before anything else, and
    counted in the warnings, one line per map site, with any of the agent's unverified_leads that
    point at one. An attribute whose every source was one is left with none, so, like a value
    given with no source, it is not promoted.

    Each source's stated location is read into a point (locate.py), with geocoded addresses taken
    from `locations`, the run's source_locations.json; nothing here reaches the network. Every row
    of a source carries the facility it describes and that point, and its number in the response
    (source_index), by which matching files it. A source that states a location also gets a
    found_location row, and where the point is coordinates, a grid reference or a rooftop address,
    it is evidence for the found location of the site it is filed under.

    Everything here is about the site searched; run_arbitration then files each source under the
    site it is about (match.py), and decides there what is reviewed.
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

    for source_index, page, value_index, attribute in citations(read):
        if source_index not in located:
            continue  # a dropped map site
        # A response cached attribute-first keeps how each citation described its page.
        source = cited_source(page, attribute)
        name = canonical_attribute(normalize_scalar(attribute.get("attribute_name")))
        if name not in VALUE_ATTRIBUTES:
            warnings.append(f"Unsupported attribute_name {name!r} dropped.")
            continue
        url, link_status, dead_link, link = link_for(source)
        # The evidence id stays per citation; the source id is the exact URL's, shared by every
        # citation of it, so evidence.csv joins sources.csv.
        evidence_id, fallback_source_id = citation_ids(site_id, source_index, value_index, attribute)
        source_id = source_id_for(url, fallback=fallback_source_id)

        conversion = convert_attribute_value(name, attribute.get("value"), attribute.get("unit"))
        # A failed conversion returns the value untouched in its ORIGINAL unit. Promoting that
        # would file an unconverted number in a column whose name asserts the canonical unit -
        # e.g. an ambiguous "30000 tons" landing in waste_in_place_metric_tonnes, a 10% error if
        # the source meant short tons. If we could not normalize it, it cannot be promoted.
        conversion_failed = bool(conversion.warning)
        if conversion.warning:
            warnings.append(f"{name}: {conversion.warning}")

        value: Any = conversion.value
        mapping_note = ""
        if name in BOOLEAN_TARGET_ATTRIBUTES:
            parsed, note = parse_tristate_bool(value)
            mapping_note = note
            value = "" if parsed is None else ("TRUE" if parsed else "FALSE")
        elif name == "waste_depth":
            # Bin the CONVERTED metres, not the raw value: the source may have reported feet,
            # and the 5m boundary is only meaningful once the unit conversion above has run. A
            # failed conversion leaves conversion.value in its original unit, so skip binning
            # entirely there and let the conversion warning exclude the row.
            if conversion_failed:
                value = ""
            else:
                mapped, mapping_note = bucket_waste_depth(conversion.value)
                value = normalize_scalar(mapped or "")
        elif name in ENUM_MAPS:
            mapped, mapping_note = map_enum_value(name, attribute.get("value"))
            value = normalize_scalar(mapped) if name in ARRAY_TARGET_ATTRIBUTES else normalize_scalar(mapped or "")
        if mapping_note:
            warnings.append(f"{name}: {mapping_note}")

        # A numeric attribute whose value is not a number must never be promoted. The agent can
        # return unit text or prose here, and it would otherwise land in a numeric spec column.
        numeric_note = ""
        if conversion_failed:
            numeric_note = f"{name}: {conversion.warning}"
        elif name in NUMERIC_TARGET_ATTRIBUTES and not is_blank(value):
            parsed = parse_float(value)
            if parsed is None:
                numeric_note = f"{name} value {value!r} is not numeric; not promotable."
            else:
                low, high = ATTRIBUTE_RANGES.get(name, (None, None))
                if (low is not None and parsed < low) or (high is not None and parsed > high):
                    bounds = f"{low if low is not None else '-inf'} to {high if high is not None else 'inf'}"
                    numeric_note = (
                        f"{name} value {parsed:g} is outside the spec range {bounds}; not promotable."
                    )
            if numeric_note:
                warnings.append(numeric_note)

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

        promotable = (
            bool(url) and not dead_link and not is_blank(value) and is_promotable(tier) and not numeric_note
        )
        exclusion = ""
        if not url:
            exclusion = "No clickable source URL."
        elif dead_link:
            exclusion = f"Source link broken ({link.status}); not promoted."
        elif is_blank(value):
            exclusion = "Value empty after normalization."
        elif numeric_note:
            exclusion = numeric_note
        elif not is_promotable(tier):
            exclusion = f"{tier_label(tier)} is below the promotion floor."

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
                "search_terms_used": normalize_scalar(attribute.get("search_terms_used")),
                "promotion_eligible": "TRUE" if promotable else "FALSE",
                "exclusion_reason": exclusion,
                "record_created_date": access_date.isoformat(),
                **location_columns(source, located[source_index]),
                "source_index": source_index,
            }
        )
        source_entries.append(source_entry(source, source_id, url, tier, rule, run_id, site_id))

    # Where each source puts the facility. Whether it is reviewed as the found location depends on
    # the site the source is filed under, so run_arbitration decides that after matching.
    for source_index, place in located.items():
        source = cited_source(read["sources"][source_index - 1])
        if not states_location(source):
            continue
        url, link_status, dead_link, link = link_for(source)
        evidence_id = f"WAIEV-{site_id}-S{source_index:03d}-LOC"
        source_id = source_id_for(url, fallback=f"WAISRC-{site_id}-S{source_index:03d}")
        tier, rule = assign_source_tier(source, LOCATION_ATTRIBUTE)
        value = place.value() if place.basis in VALUE_BASES else ""
        published = normalize_scalar(source.get("publication_date"))
        summary = f"Read as {place.basis} from the location the source states: {normalize_scalar(source.get('stated_location'))}"
        order += 1
        if url and not dead_link and value:
            by_attribute.setdefault(LOCATION_ATTRIBUTE, []).append(
                {
                    "evidence_id": evidence_id,
                    "source_id": source_id,
                    "source_index": source_index,
                    "attribute_name": LOCATION_ATTRIBUTE,
                    "value": value,
                    "unit": "decimal degrees",
                    "tier": tier,
                    "order": order,
                    "url": url,
                    "link_status": link_status,
                    # A location has no date of its own; the newest source wins a tie of tiers.
                    "value_date": published,
                    "confidence": "",
                    "evidence_summary": summary,
                }
            )
        exclusion = ""
        if not url:
            exclusion = "No clickable source URL."
        elif dead_link:
            exclusion = f"Source link broken ({link.status}); not promoted."
        elif not value:
            exclusion = location_exclusion(place)
        elif not is_promotable(tier):
            exclusion = f"{tier_label(tier)} is below the promotion floor."
        evidence_rows.append(
            {
                "evidence_id": evidence_id,
                "run_id": run_id,
                "dataset_version": dataset_version,
                "site_id": site_id,
                "internal_facility_id": normalize_scalar(site.get("internal_facility_id") or site_id),
                "attribute_name": LOCATION_ATTRIBUTE,
                "claimed_value": normalize_scalar(source.get("stated_location")),
                "claimed_unit": normalize_scalar(source.get("stated_location_kind")),
                "normalized_value": place.value(),
                "normalized_unit": "decimal degrees" if place.point else "",
                "unit_conversion_note": "",
                "mapped_value": value,
                "mapping_note": "",
                "value_basis": "",
                "value_date": published,
                "agent_confidence": "",
                "source_id": source_id,
                "link_status": link_status,
                "url": url,
                "source_tier": tier_label(tier),
                "agent_proposed_tier": normalize_scalar(source.get("source_tier")),
                "tier_rule_applied": rule,
                "evidence_summary": summary,
                "quoted_evidence_short": "",
                "search_terms_used": "",
                "promotion_eligible": "TRUE" if not exclusion else "FALSE",
                "exclusion_reason": exclusion,
                "record_created_date": access_date.isoformat(),
                **location_columns(source, place),
                "source_index": source_index,
            }
        )
        source_entries.append(source_entry(source, source_id, url, tier, rule, run_id, site_id))

    for name in sorted(only_dropped - kept_attributes):
        if name in VALUE_ATTRIBUTES:
            warnings.append(f"{name}: every source was a dropped map site; not promoted.")

    # The agent's own leads go to the Leads sheet (payload_leads), which skips the same map sites,
    # so they are counted with the rest.
    for lead in read["unverified_leads"]:
        mirror = dropped_source_site(lead.get("url")) if isinstance(lead, dict) else ""
        if mirror:
            dropped[mirror] = dropped.get(mirror, 0) + 1

    for mirror, count in sorted(dropped.items()):
        warnings.append(
            f"Dropped {count} citation(s) of {mirror}: map sites that copy OpenStreetMap or scrape "
            "listings are not used as sources."
        )
    # A value cited by several sources is checked once per source; one warning says it.
    return by_attribute, evidence_rows, source_entries, list(dict.fromkeys(warnings))


def location_columns(source: dict[str, Any], place: Located) -> dict[str, str]:
    """The facility a source describes, as it writes it, and the point read from its location."""
    stated = states_location(source)
    return {
        "facility_name_in_source": normalize_scalar(source.get("facility_name_in_source")),
        "facility_name_english": normalize_scalar(source.get("facility_name_english")),
        "stated_location": normalize_scalar(source.get("stated_location")) if stated else "",
        "stated_location_kind": normalize_scalar(source.get("stated_location_kind")) if stated else "",
        "location_basis": place.basis if stated else "",
        "located_latitude": format_coordinate(place.latitude) if stated else "",
        "located_longitude": format_coordinate(place.longitude) if stated else "",
        "location_reason": place.reason if stated else "",
    }


def describe_located(bases: list[str]) -> str:
    """A count with its bases, for Run_Config: "12 (coordinates 9, street 3)", or "0"."""
    return f"{len(bases):,} ({count_bases(bases)})" if bases else "0"


def location_exclusion(place: Located) -> str:
    """Why a stated location is not a location value."""
    if place.basis == STREET:
        return "An address placed only on its street is used for matching, not as a value."
    if place.basis == HINT:
        return f"A hint, not a location value: {place.reason}."
    return f"No location: {place.reason}."


def source_entry(
    source: dict[str, Any], source_id: str, url: str, tier: int, rule: str, run_id: str, site_id: str
) -> dict[str, Any]:
    """One citation's row for sources.csv, before dedupe_sources makes it one row per URL."""
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


def baseline_coordinates(site: dict[str, Any]) -> str:
    """The seed location as "lat, lon", which Google Maps finds when pasted into its search box."""
    lat, lon = parse_float(site.get("latitude")), parse_float(site.get("longitude"))
    if lat is None or lon is None:
        return ""
    return f"{lat:.6f}, {lon:.6f}"


def closure_reported(resolved: list[dict[str, Any]]) -> dict[str, list[str]]:
    """What the run found about each site's closure, keyed by site.

    The likeliest false "Not a Waste Facility" is a closed landfill misread as "not a landfill" -
    "it's a park now". A site given that verdict AND reported closed is exactly that shape, so its
    facility_type review row says so.
    """
    closure: dict[str, list[str]] = {}
    for row in resolved:
        attribute = row["attribute_name"]
        value = normalize_scalar(row.get("resolved_value"))
        if attribute == "facility_status" and value == "Inactive":
            closure.setdefault(row["site_id"], []).append("facility_status = Inactive")
        elif attribute == "closing_year" and value:
            closure.setdefault(row["site_id"], []).append(f"closing_year = {value}")
    return closure


# What the queue says of a winning source whose quote or stated location isn't on its page.
QUOTE_CHECKS = {
    NOT_FOUND: "its quote is not on the source's page",
    PARTLY: "its quote is only partly on the source's page",
}
LOCATION_CHECKS = {
    NOT_FOUND: "the location the source states is not on its page",
    PARTLY: "the location the source states is only partly on its page",
}


def page_checks(row: dict[str, Any]) -> list[str]:
    """What the queue flags of a row's winning source, from the text the link check kept of its page.

    The location is flagged where it counted: on a found_location row, and wherever the source was
    filed by it."""
    checks = [QUOTE_CHECKS[row["_quote_on_page"]]] if row.get("_quote_on_page") in QUOTE_CHECKS else []
    location_counted = row["attribute_name"] == LOCATION_ATTRIBUTE or row.get("_located_by") in MATCHING_BASES
    if location_counted and row.get("_location_on_page") in LOCATION_CHECKS:
        checks.append(LOCATION_CHECKS[row["_location_on_page"]])
    return checks


def review_summary(row: dict[str, Any], closure: dict[str, list[str]]) -> str:
    """The evidence_summary a review row shows: the rule applied, plus the closure check and what
    the source's own page doesn't bear out."""
    summary = row.get("resolution_rule", "")
    reported = closure.get(row["site_id"], [])
    if row["attribute_name"] == "facility_type" and row.get("resolved_value") == NOT_A_WASTE_FACILITY and reported:
        summary += (
            f" CHECK: this run also found {'; '.join(reported)} - is this a closed landfill "
            "misread as never a waste site?"
        )
    for check in page_checks(row):
        summary += f" CHECK: {check}."
    return summary


def cover_consistency_warnings(resolved: list[dict[str, Any]]) -> list[str]:
    """Flag a has_biocover answer that contradicts the cover types found alongside it.

    They are searched independently and describe the same physical cover, so a disagreement means
    one of the two is wrong. Surfaced rather than auto-corrected: which one is wrong depends on
    the sources, and the reviewer has both rows in front of them. cover_types is yearly, with its
    newest year first, and that is the one compared.
    """
    values: dict[str, str] = {}
    for row in resolved:
        values.setdefault(normalize_scalar(row.get("attribute_name")), normalize_scalar(row.get("resolved_value")))
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


# --- filing each source under the site it is about (build step 5) -----------------------------
def citations_for(found: dict[str, tuple[dict[str, list[dict[str, Any]]], list[dict[str, Any]]]]) -> list[Citation]:
    """Every source each search cited, as matching sees it, read from the search's evidence rows.

    Every row of a source carries the facility it describes and where it was located, so any of
    its rows will do. A source that gives no value and states no location has no row, and nothing
    to file.
    """
    out: list[Citation] = []
    for site_id, (_by_attribute, rows) in found.items():
        cited: dict[int, Citation] = {}
        for row in rows:
            index = row.get("source_index")
            if index is None:
                continue
            citation = cited.get(index)
            if citation is None:
                latitude, longitude = parse_float(row.get("located_latitude")), parse_float(row.get("located_longitude"))
                citation = Citation(
                    searched_site_id=site_id,
                    source_index=index,
                    url=normalize_scalar(row.get("url")),
                    name_in_source=normalize_scalar(row.get("facility_name_in_source")),
                    name_english=normalize_scalar(row.get("facility_name_english")),
                    stated_location=normalize_scalar(row.get("stated_location")),
                    basis=normalize_scalar(row.get("location_basis")),
                    point=(latitude, longitude) if latitude is not None and longitude is not None else None,
                    location_reason=normalize_scalar(row.get("location_reason")),
                )
                cited[index] = citation
            if row.get("attribute_name") == "facility_type" and row.get("mapped_value") == NOT_A_WASTE_FACILITY:
                citation.not_a_waste_facility = True
        out.extend(cited.values())
    return out


# Where a value goes once its source is filed: into its site's pool for review, to Leads, to one of
# the lists for ingestion (new_facility_candidates.csv or outside_run_matches.csv), or nowhere but
# evidence.csv, where a Tier 1-2 name or location of ours is not reviewed.
POOL, LEAD, LISTED, RECORDED = "pool", "lead", "listed", "recorded"


def route(
    attribute: str, value: Any, filing: Filing, searched: dict[str, Any], filed: dict[str, Any] | None
) -> tuple[str, str]:
    """Where one value goes, given where its source is filed, and why it isn't reviewed if not.

    "Not a Waste Facility" stays only with the site searched, and only where that site was offered
    the check (WP-525); anywhere else it is a lead, and never a new-landfill candidate. A site whose
    name or coordinates come from a Tier 1-2 source has its sources' names and locations recorded,
    not reviewed: the match is judged on the site a source is filed under, not the one searched.
    """
    searched_id = normalize_scalar(searched.get("site_id"))
    if attribute == "facility_type" and normalize_scalar(value) == NOT_A_WASTE_FACILITY:
        if filing.site_id != searched_id:
            where = f"is filed under site {filing.site_id}" if filing.site_id else "matches no site of ours"
            return LEAD, (
                f"{NOT_A_WASTE_FACILITY} is kept only for the site searched, where it was offered the check; "
                f"this source {where}."
            )
        if not needs_contradiction_check(searched):
            return LEAD, f"{NOT_A_WASTE_FACILITY} is kept only where it was offered, and this site was not offered it."
    if filing.outcome == NOT_A_FACILITY:
        return RECORDED, (
            "Recorded only: no site of ours is near where the source places it, and it says the facility "
            f"is not a waste facility, so it is not listed in {NEW_FACILITY_FILE}."
        )
    if not filing.site_id:
        return LISTED, f"No site of ours is near where the source places it: listed in {NEW_FACILITY_FILE}."
    if not filing.in_run:
        return LISTED, (
            f"Filed under site {filing.site_id}, outside this run's selection: listed in {OUTSIDE_RUN_FILE}."
        )
    if attribute == LOCATION_ATTRIBUTE and filed is not None and not asks_location(filed):
        return RECORDED, "Recorded only: our location comes from a Tier 1-2 source, so it is not reviewed."
    if attribute == "found_facility_name" and filed is not None and trusted_baseline_name(filed):
        return RECORDED, "Recorded only: our name comes from a Tier 1-2 source, so it is not reviewed."
    return POOL, ""


def filing_for(matching: Matching, site_id: str, source_index: Any) -> Filing:
    """A source's filing; one matching never saw stays with the site searched."""
    return matching.filings.get(
        (site_id, source_index),
        Filing(site_id, NO_LOCATION, f"{NO_LOCATION}: the source gives no location, so it stays with the site searched.", [site_id]),
    )


def file_evidence(
    found: dict[str, tuple[dict[str, list[dict[str, Any]]], list[dict[str, Any]]]],
    matching: Matching,
    sites_by_id: dict[str, dict[str, Any]],
) -> dict[str, dict[str, list[dict[str, Any]]]]:
    """Pool every search's evidence under the site each source is filed under, by attribute.

    Each evidence row now names that site as its site_id, the search that found it as its
    searched_site_id, and the rule as its match. A row not pooled says why in exclusion_reason, and
    carries its route, so Leads takes only what belongs there. Evidence keeps the agent's order
    within a search, and searches follow one another by site id, so equal sources still break ties
    by order.
    """
    pools: dict[str, dict[str, list[dict[str, Any]]]] = {}
    order = 0
    for site_id, (by_attribute, rows) in sorted(found.items()):
        searched = sites_by_id[site_id]
        for row in rows:
            filing = filing_for(matching, site_id, row.get("source_index"))
            filed = sites_by_id.get(filing.site_id)
            row["searched_site_id"] = site_id
            row["site_id"] = filing.site_id
            row["internal_facility_id"] = normalize_scalar((filed or {}).get("internal_facility_id") or filing.site_id)
            row["match"] = filing.match
            value = row.get("mapped_value") or row.get("normalized_value")
            where, why = route(row["attribute_name"], value, filing, searched, filed)
            row["_route"] = where
            if where in (LEAD, LISTED) or (where == RECORDED and not row["exclusion_reason"]):
                row["promotion_eligible"] = "FALSE"
                row["exclusion_reason"] = why
        for name, items in by_attribute.items():
            for item in items:
                filing = filing_for(matching, site_id, item.get("source_index"))
                where, _why = route(name, item.get("value"), filing, searched, sites_by_id.get(filing.site_id))
                if where != POOL:
                    continue
                order += 1
                pools.setdefault(filing.site_id, {}).setdefault(name, []).append(
                    {**item, "order": order, "searched_site_id": site_id}
                )
    return pools


def resolve_site(
    site: dict[str, Any], pool: dict[str, list[dict[str, Any]]], searched: bool, run_year: int
) -> list[dict[str, Any]]:
    """Every source filed under one site, from any search, pooled into one decision per attribute,
    and for a yearly attribute one per year.

    A site that was searched is resolved on everything it was asked, so what nothing answered says
    "Not found"; a site that only had sources filed under it, on what they give. A yearly value's
    year is its value_date's, or the run's where it has none, as ingestion reads it; its baseline
    is ours for that year (site_for_year), and the newest year comes first.
    """
    names = list(requested_attributes(site)) if searched else []
    names += [name for name in pool if name not in names]
    rows: list[dict[str, Any]] = []
    for name in names:
        items = pool.get(name, [])
        if name not in TIME_VARYING_ATTRIBUTES or not items:
            rows.append(searched_by(resolve(site, name, items), items))
            continue
        by_year: dict[int, list[dict[str, Any]]] = {}
        for item in items:
            by_year.setdefault(value_year(item.get("value_date"), run_year), []).append(item)
        for year in sorted(by_year, reverse=True):
            site_then, note = site_for_year(site, name, year)
            row = searched_by(resolve(site_then, name, by_year[year]), by_year[year])
            if note:
                row["resolution_rule"] = f"{row['resolution_rule']} {note}".strip()
            row["year"] = year
            rows.append(row)
    return rows


def searched_by(row: dict[str, Any], items: list[dict[str, Any]]) -> dict[str, Any]:
    """Name on a decided row the searches whose sources it weighed. describe_winner narrows it to
    the one that found the winning value; a decision with no winner, such as a weaker disagreement
    routed to leads, keeps them all."""
    row["searched_site_id"] = "; ".join(dict.fromkeys(normalize_scalar(item.get("searched_site_id")) for item in items))
    return row


def decision_key(site_id: str, attribute: str, year: Any) -> tuple[str, str, Any]:
    """(site, attribute, year): what one decision is made for, the year only for a yearly attribute."""
    return site_id, attribute, year if attribute in TIME_VARYING_ATTRIBUTES else ""


def describe_winner(row: dict[str, Any], evidence_by_id: dict[str, dict[str, Any]], matching: Matching) -> None:
    """Add to a decided row the search that found its winning value, why that value is filed here,
    and the facility its source describes, as the queue shows them."""
    winner = evidence_by_id.get(normalize_scalar(row.get("winning_evidence_id")))
    if winner is None:
        return
    filing = filing_for(matching, winner["searched_site_id"], winner.get("source_index"))
    row["searched_site_id"] = winner["searched_site_id"]
    row["match"] = winner["match"]
    row["facility_name_in_source"] = winner.get("facility_name_in_source", "")
    row["stated_location"] = winner.get("stated_location", "")
    # Only a point matching could use: never a town's, nor one the guards set aside.
    placed = winner.get("location_basis", "") in MATCHING_BASES
    row["source_coordinates"] = (
        f"{winner['located_latitude']}, {winner['located_longitude']}"
        if placed and winner.get("located_latitude") and winner.get("located_longitude") else ""
    )
    row["location_basis"] = winner.get("location_basis", "") if row["attribute_name"] == LOCATION_ATTRIBUTE else ""
    # Whether its quote, and the location it states, are on its page, for the queue's CHECK notes.
    row["_quote_on_page"] = winner.get("quote_on_page", "")
    row["_location_on_page"] = winner.get("location_on_page", "")
    row["_located_by"] = winner.get("location_basis", "")
    # The sites an SME might re-file it under, offered where there is more than one.
    row["_site_choices"] = filing.choices if len(filing.choices) > 1 else []


def is_evidence_lead(row: dict[str, Any], promoted: bool) -> bool:
    """Whether an evidence row is listed under Leads: a "Not a Waste Facility" verdict filed where
    it was not offered, or a finding that could have been reviewed and lost. `promoted` says whether
    its attribute was decided with a value at the site it is filed under."""
    if row["_route"] == LEAD:
        return True
    if row["_route"] != POOL or row["promotion_eligible"] == "TRUE" or not row["exclusion_reason"]:
        return False
    # A stated location is a lead only where it could have been a value and lost: a hint and a
    # street-level address stay in evidence.csv.
    if row["attribute_name"] == LOCATION_ATTRIBUTE and row["location_basis"] not in VALUE_BASES:
        return False
    # A finding already promoted is not listed again as a lead. A dead-link source is the exception:
    # it never won anything - it was excluded before resolution - so the lead is the only place it
    # is recorded, and without this it vanished whenever another source filled the same attribute.
    return not promoted or row.get("link_status") == BROKEN


def read_listed_candidates(path: Path) -> list[dict[str, str]]:
    """The new-facility candidates SMEs added to a run's list, which arbitrating again keeps."""
    if not path.exists():
        return []
    with path.open(newline="", encoding="utf-8-sig") as handle:
        return [dict(row) for row in csv.DictReader(handle) if row.get("found_by") == FOUND_BY_SME]


def payload_leads(
    cached: list[Path],
    sites_by_id: dict[str, dict[str, Any]],
    run_id: str,
) -> list[dict[str, Any]]:
    """Carry the agent's own unverified_leads through to the leads sheet.

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
                summary = normalize_scalar(lead.get("lead_summary") or lead.get("summary") or lead.get("note"))
                url = normalize_scalar(lead.get("url"))
                if dropped_source_site(url):
                    continue
                attribute = normalize_scalar(lead.get("attribute_name"))
                value = normalize_scalar(lead.get("value"))
            else:
                summary, url, attribute, value = normalize_scalar(lead), "", "", ""
            out.append(
                {
                    "run_id": run_id,
                    "site_id": site_id,
                    "site_name": normalize_scalar(site.get("site_name")),
                    "country_iso3": normalize_scalar(site.get("country_iso3")),
                    "searched_site_id": site_id,
                    "attribute_name": attribute,
                    "lead_value": value,
                    "source_tier": "",
                    "lead_summary": summary,
                    "url": url,
                    "exclusion_reason": "Agent reported as unverified.",
                }
            )
    return out


# What arbitration wrote until build step 3 of the source-matching plan: a standardized table, and
# the .load.sql beside it, both built from values accepted before review. Ingestion #261 now builds
# the AI search's table from reviewed values instead.
RETIRED_OUTPUTS = ("std_facility_tbl_ai_search_*.csv", "std_facility_tbl_ai_search_*.load.sql")


def remove_retired_outputs(run_dir: Path) -> list[Path]:
    """Delete the standardized table and .load.sql an earlier arbitration left in the run folder.

    Arbitrating again replaces that arbitration's outputs, resolved.csv among them, and these two
    would otherwise sit beside a header-only resolved.csv looking ready to load. Only the local
    copies go: a copy pushed to blob stays there as the record of that arbitration, and comes back
    down on the next pull, so this runs on every arbitration.
    """
    removed = []
    for pattern in RETIRED_OUTPUTS:
        for path in sorted(run_dir.glob(pattern)):
            path.unlink()
            removed.append(path)
    if removed:
        names = ", ".join(path.name for path in removed)
        print(
            f"Removed {names}: an earlier arbitration built them before review, and nothing is loaded "
            "from this repository now. Any copy in blob is kept."
        )
    return removed


def run_arbitration(config: PipelineConfig) -> dict[str, Path]:
    access_date = date.today()
    # Checked before the seed, so a mistyped --run-id says so rather than reaching for a database.
    cached = sorted(raw_dir(config.run_dir).glob("site_*.json"))
    if not cached:
        raise ValueError(f"No cached responses in {raw_dir(config.run_dir)}. Run the search phase first.")
    sites, _headers = resolve_sites(config, allow_database=False)
    sites_by_id = {normalize_scalar(site.get("site_id")): site for site in sites}

    # Read only: the search phase did the fetching and the geocoding, so arbitration stays offline.
    link_results = load_results(config.run_dir / LINK_CHECK_FILE)
    locations = load_locations(config.run_dir / SOURCE_LOCATIONS_FILE)
    # The text the link check kept of each page that loaded: each quote and stated location is
    # checked against it, and one that isn't there is flagged for the SME (on_page.py).
    pages = PageChecker(
        config.run_dir, {result.text_file: result.text_note for result in link_results.values() if result.text_file}
    )

    # A site may have several cached responses - one per pass - and all of them count as evidence.
    by_site: dict[str, list[Path]] = {}
    for path in cached:
        by_site.setdefault(site_id_from_path(path), []).append(path)

    evidence_rows: list[dict[str, Any]] = []
    resolved_rows: list[dict[str, Any]] = []
    source_entries: list[dict[str, Any]] = []
    leads: list[dict[str, Any]] = []
    parse_warnings: list[dict[str, Any]] = []
    retry_sites: list[dict[str, Any]] = []
    # What each source that states a location was read as, for Run_Config.
    stated_bases: list[str] = []
    # What each search found, by attribute and as evidence rows, before its sources are filed.
    found: dict[str, tuple[dict[str, list[dict[str, Any]]], list[dict[str, Any]]]] = {}

    for site_id, paths in sorted(by_site.items()):
        site = sites_by_id.get(site_id)
        if site is None:
            parse_warnings.append(
                {"run_id": config.run_id, "site_id": site_id, "site_name": "", "warning": "Site not in the seed."}
            )
            continue

        payloads = []
        for path in paths:
            try:
                payloads.append(load_json(path))
            except (OSError, json.JSONDecodeError) as exc:
                parse_warnings.append(
                    {
                        "run_id": config.run_id,
                        "site_id": site_id,
                        "site_name": normalize_scalar(site.get("site_name")),
                        "warning": f"Unreadable cached response {path.name}: {exc}",
                    }
                )
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
            parse_warnings.append(
                {
                    "run_id": config.run_id,
                    "site_id": site_id,
                    "site_name": normalize_scalar(site.get("site_name")),
                    "warning": "Search tool failure reported by the agent; retry this site.",
                }
            )

        for warning in validate_foundry_payload(payload):
            parse_warnings.append(
                {
                    "run_id": config.run_id,
                    "site_id": site_id,
                    "site_name": normalize_scalar(site.get("site_name")),
                    "warning": warning,
                }
            )

        by_attribute, site_evidence, site_sources, warnings = extract_evidence(
            site, payload, config.run_id, config.dataset_version, access_date,
            link_results=link_results, keep_broken_links=config.keep_broken_links, locations=locations,
        )
        source_entries.extend(site_sources)
        stated_bases += [row["location_basis"] for row in site_evidence if row["attribute_name"] == LOCATION_ATTRIBUTE]
        for warning in warnings:
            parse_warnings.append(
                {
                    "run_id": config.run_id,
                    "site_id": site_id,
                    "site_name": normalize_scalar(site.get("site_name")),
                    "warning": warning,
                }
            )

        for row in site_evidence:
            pages.check(row)
        found[site_id] = (by_attribute, site_evidence)

    # Each source is filed under the site of ours it is about (match.py), and only the sites this
    # run selected get values. Everything filed under one site, from any search, is decided
    # together: one review row per site and attribute, and per year for a yearly attribute.
    selection = run_selection(sites, set(found))
    found_citations = citations_for(found)
    matching = match_sources(found_citations, sites, selection)
    pools = file_evidence(found, matching, sites_by_id)
    evidence_by_id = {
        row["evidence_id"]: row for _by_attribute, rows in found.values() for row in rows
    }
    run_year = access_date.year
    resolved_by_site: dict[str, list[dict[str, Any]]] = {}
    for site_id in sorted(set(found) | set(pools)):
        site = sites_by_id[site_id]
        site_resolved = resolve_site(site, pools.get(site_id, {}), site_id in found, run_year)
        for row in site_resolved:
            describe_winner(row, evidence_by_id, matching)
        for warning in cover_consistency_warnings(site_resolved):
            parse_warnings.append(
                {
                    "run_id": config.run_id,
                    "site_id": site_id,
                    "site_name": normalize_scalar(site.get("site_name")),
                    "warning": warning,
                }
            )
        resolved_by_site[site_id] = site_resolved
        resolved_rows.extend(site_resolved)

        for row in site_resolved:
            if not routed_to_leads(row):
                continue
            leads.append(
                {
                    "run_id": config.run_id,
                    "site_id": site_id,
                    "site_name": normalize_scalar(site.get("site_name")),
                    "country_iso3": normalize_scalar(site.get("country_iso3")),
                    "searched_site_id": row.get("searched_site_id", ""),
                    "attribute_name": row["attribute_name"],
                    "lead_value": row["resolved_value"],
                    "source_tier": row["winning_source_tier"] or row["best_tier_available"],
                    "lead_summary": row["resolution_rule"],
                    "url": row["winning_source_url"],
                    "exclusion_reason": row["resolution"],
                }
            )

    # Feedback 1 lets a low-tier source fill an empty baseline, so a piece of evidence the tier gate
    # marked ineligible can still end up winning. Listing it as a lead as well would show the SME
    # the same finding twice, in two places, which feedback 3 rules out. Both this and the cap below
    # go by the key each decision was made for: one year of a yearly attribute can confirm ours
    # while another is queued, and the queued year's evidence is kept in full.
    promoted = {
        decision_key(row["site_id"], row["attribute_name"], row.get("year", ""))
        for row in resolved_rows
        if normalize_scalar(row.get("resolved_value"))
    }
    # Q8: cap evidence rows for pure confirmations, per site they are filed under; keep everything else.
    confirmed = {
        decision_key(row["site_id"], row["attribute_name"], row.get("year", ""))
        for row in resolved_rows
        if row["resolution"] == "Confirmed baseline"
    }
    kept_per_attribute: dict[tuple[str, str, Any], int] = {}
    for site_id in sorted(found):
        for row in found[site_id][1]:
            key = decision_key(row["site_id"], row["attribute_name"], value_year(row.get("value_date"), run_year))
            if key in confirmed:
                kept_per_attribute[key] = kept_per_attribute.get(key, 0) + 1
                if kept_per_attribute[key] > MAX_CONFIRMATION_EVIDENCE_ROWS:
                    continue
            evidence_rows.append(row)
            if not is_evidence_lead(row, key in promoted):
                continue
            leads.append(
                {
                    "run_id": config.run_id,
                    "site_id": row["site_id"] or row["searched_site_id"],
                    "site_name": "",
                    "country_iso3": normalize_scalar(sites_by_id.get(row["site_id"] or row["searched_site_id"], {}).get("country_iso3")),
                    "searched_site_id": row["searched_site_id"],
                    "attribute_name": row["attribute_name"],
                    "lead_value": row["mapped_value"] or row["normalized_value"],
                    "source_tier": row["source_tier"],
                    "lead_summary": row["evidence_summary"],
                    "url": row.get("url", "") or "",
                    "exclusion_reason": row["exclusion_reason"],
                }
            )

    for lead in payload_leads(cached, sites_by_id, config.run_id):
        leads.append(lead)

    # One pass fills the seed's name fields into every row list, keyed by site. The review queue is
    # derived from resolved_rows below, so it inherits the names.
    for rows in (evidence_rows, resolved_rows, leads):
        attach_site_names(rows, sites_by_id)

    # A "Not a Waste Facility" verdict is reviewed on its own facility_type row, like any other
    # facility_type; the row also says when the same run found the site closed.
    closure = closure_reported(resolved_rows)
    review_queue: list[dict[str, Any]] = []
    for row in resolved_rows:
        if not needs_review(row):
            continue
        queue_row = {header: row.get(header, "") for header in REVIEW_QUEUE_HEADERS}
        queue_row["evidence_summary"] = review_summary(row, closure)
        queue_row["baseline_coordinates"] = baseline_coordinates(sites_by_id.get(row["site_id"], {}))
        # The workbook offers these in corrected_site_id's dropdown; they are not a column.
        queue_row["_site_choices"] = row.get("_site_choices", [])
        review_queue.append(queue_row)

    not_a_waste_facility = sum(
        1
        for row in resolved_rows
        if row["attribute_name"] == "facility_type" and row.get("resolved_value") == NOT_A_WASTE_FACILITY
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

    out = config.run_dir
    remove_retired_outputs(out)
    paths = {
        "evidence": out / "evidence.csv",
        "resolved": out / "resolved.csv",
        "sources": out / "sources.csv",
        "review_queue": out / "review_queue.csv",
        "leads": out / "supplementary_leads.csv",
        "warnings": out / "parse_warnings.csv",
        "retry": out / "sites_to_retry.csv",
        "new_facilities": out / NEW_FACILITY_FILE,
        "possible_duplicates": out / POSSIBLE_DUPLICATES_FILE,
        "outside_run": out / OUTSIDE_RUN_FILE,
        "datasets_found": out / DATASETS_FOUND_FILE,
    }
    # The new-facility candidates SMEs marked, which collect-new-facilities added, stay listed.
    new_facilities = matching.new_facilities + read_listed_candidates(paths["new_facilities"])
    write_csv_records(paths["evidence"], evidence_rows, EVIDENCE_HEADERS)
    # Header only: nothing is accepted automatically. Ingestion reads the SMEs' decisions from the
    # workbook, and accepts an empty resolved.csv beside it.
    write_csv_records(paths["resolved"], [], RESOLVED_HEADERS)
    sources = dedupe_sources(source_entries)
    write_csv_records(paths["sources"], sources, SOURCES_HEADERS)
    write_csv_records(paths["review_queue"], review_queue, REVIEW_QUEUE_HEADERS)
    write_csv_records(paths["leads"], leads, SUPPLEMENTARY_LEADS_HEADERS)
    write_csv_records(paths["warnings"], parse_warnings, PARSE_WARNING_HEADERS)
    write_csv_records(
        paths["retry"], retry_sites, ["site_id", "site_name", "country_iso3", "reason", "search_notes"]
    )
    # For ingestion, which has no path for either: landfills none of our sites is, and pairs of our
    # sites one source fits equally well. Then sources matched outside this run's selection.
    write_csv_records(paths["new_facilities"], new_facilities, NEW_FACILITY_HEADERS)
    write_csv_records(paths["possible_duplicates"], matching.duplicates, POSSIBLE_DUPLICATES_HEADERS)
    write_csv_records(paths["outside_run"], matching.outside, OUTSIDE_RUN_HEADERS)
    # Pages that look like datasets, better loaded once through ingestion than found again by every
    # search that needs one of their rows.
    datasets = datasets_found(found_citations, matching, sources)
    write_csv_records(paths["datasets_found"], datasets, DATASETS_FOUND_HEADERS)

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
            {"setting": "sites_arbitrated", "value": len(cached)},
            {"setting": "unsearchable_names_skipped", "value": recorded_skips(config.run_dir, set(by_site))},
            {"setting": "unsearchable_names_searched", "value": unsearchable_searched},
            {"setting": "arbitrated_at", "value": datetime.now().replace(microsecond=0).isoformat()},
            {"setting": "review_queue_rows", "value": len(review_queue)},
            {"setting": "links_checked", "value": len(link_results)},
            {"setting": "links_broken", "value": sum(1 for r in link_results.values() if r.verdict == BROKEN)},
            {"setting": "links_repaired", "value": sum(1 for r in link_results.values() if r.resolved_url != r.url)},
            {
                "setting": "broken_link_route",
                "value": "kept promotable" if config.keep_broken_links else "routed to leads",
            },
            {"setting": "locations_stated", "value": describe_located(stated_bases)},
            {"setting": "locations_used", "value": describe_located([b for b in stated_bases if b in MATCHING_BASES])},
            {
                "setting": "geocoder",
                "value": describe_geocoders(locations, (config.run_dir / SOURCE_LOCATIONS_FILE).exists()),
            },
            {"setting": "contradiction_checks", "value": contradiction_checks},
            {"setting": "not_a_waste_facility", "value": not_a_waste_facility},
            *match_settings(matching),
            {"setting": "datasets_found", "value": len(datasets)},
            *cost_settings(config.run_dir),
            # Counted by file: two spellings of one URL share one.
            {"setting": "page_text_kept", "value": len({result.text_file for result in link_results.values() if result.text_file})},
            {"setting": "quotes_on_page", "value": describe_results([row.get("quote_on_page", "") for row in evidence_rows])},
            {"setting": "locations_on_page", "value": describe_results(stated_on_page(evidence_rows))},
        ],
        review_queue=review_queue,
        leads=leads,
        parse_warnings=parse_warnings,
    )

    print_summary(by_site, resolved_rows, evidence_rows, review_queue, leads, parse_warnings, retry_sites, matching)
    return paths


# Run_Config's count of each match outcome (match.py), in the order of the rules.
MATCH_SETTINGS = [
    ("match_located", LOCATED),
    ("match_named_among_several", NAMED_AMONG_SEVERAL),
    ("match_tie", TIE),
    ("match_name_nearby", NAME_NEARBY),
    ("match_new_facility", NEW_FACILITY),
    ("match_not_a_waste_facility", NOT_A_FACILITY),
    ("match_moved_by_name", MOVED_BY_NAME),
    ("match_no_location", NO_LOCATION),
    ("match_outside_run", OUTSIDE_RUN),
]


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
            seen.setdefault((row.get("searched_site_id") or row.get("site_id"), row.get("source_index")), row["location_on_page"])
    return list(seen.values())


def match_settings(matching: Matching) -> list[dict[str, Any]]:
    """How each source was matched, for Run_Config. A source with a location is counted once, however
    many searches cited it; one without, once for each search that cited it."""
    return [
        *({"setting": setting, "value": matching.counts[outcome]} for setting, outcome in MATCH_SETTINGS),
        {"setting": "new_facility_candidates", "value": len(matching.new_facilities)},
        {"setting": "possible_duplicates", "value": len(matching.duplicates)},
    ]


def print_summary(cached, resolved_rows, evidence_rows, review_queue, leads, warnings, retry_sites, matching=None) -> None:
    from collections import Counter

    resolutions = Counter(row["resolution"] for row in resolved_rows)
    tiers = Counter(row["source_tier"] for row in evidence_rows)
    print(f"\nSites arbitrated:        {len(cached)}")
    print(f"Evidence rows:           {len(evidence_rows)}")
    print(f"Attributes arbitrated:   {len(resolved_rows)}")
    print(f"Leads:                   {len(leads)}")
    print(f"Parse warnings:          {len(warnings)}")
    if matching is not None:
        print("\nSources matched:")
        for setting, outcome in MATCH_SETTINGS:
            print(f"  {matching.counts[outcome]:>5}  {outcome}")
        print(f"  New-facility candidates: {len(matching.new_facilities)}; possible duplicates: {len(matching.duplicates)}")
    if retry_sites:
        ids = " ".join(row["site_id"] for row in retry_sites)
        print(
            f"\n!! {len(retry_sites)} site(s) could not be searched - the agent's search tool failed."
        )
        print("   These are NOT 'no data found'. Retry them:")
        print(f"   uv run waste-ai-search search --run-id <run> --force --site-ids {ids}")
    print(f"\nSME review queue:        {len(review_queue)} rows  (nothing is accepted without review)")
    print("\nResolutions:")
    for name, count in resolutions.most_common():
        print(f"  {count:>5}  {name}")
    print("\nEvidence by assigned tier:")
    for name, count in sorted(tiers.items()):
        print(f"  {count:>5}  {name}")
