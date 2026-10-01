"""Phase 2: rebuild every output from the cached responses.

Pure and offline. No network, no database, no clock beyond the date passed in, so a rubric change
can be re-applied to a finished run for free and an interrupted search is never a partial result.
"""
from __future__ import annotations

import json
import math
from datetime import date, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit

from .arbitration import calculated_row, needs_review, resolve, routed_to_leads
from .credibility import assign_source_tier, is_promotable, tier_label
from .input_loader import describe_seed, resolve_sites, write_csv_records
from .prompt_builder import needs_contradiction_check, requested_attributes
from .run_context import PipelineConfig, load_json, raw_dir, search_tool_failed, site_id_from_path
from .schema import (
    canonical_attribute,
    NOT_A_WASTE_FACILITY,
    ARRAY_TARGET_ATTRIBUTES,
    ATTRIBUTE_RANGES,
    BOOLEAN_TARGET_ATTRIBUTES,
    CALCULATED_ATTRIBUTES,
    ENUM_MAPS,
    EVIDENCE_HEADERS,
    NUMERIC_TARGET_ATTRIBUTES,
    PARSE_WARNING_HEADERS,
    REQUESTABLE_ATTRIBUTES,
    RESOLVED_HEADERS,
    REVIEW_QUEUE_HEADERS,
    SOURCES_HEADERS,
    STANDARDIZED_FACILITY_COLUMNS,
    SUPPLEMENTARY_LEADS_HEADERS,
    bucket_waste_depth,
    is_blank,
    map_enum_value,
    normalize_scalar,
    parse_tristate_bool,
    validate_foundry_payload,
)
from .standardized import build_standard_table, to_csv_row, write_load_statement
from .unit_converter import convert_attribute_value
from .workbook_io import write_review_workbook


MAX_CONFIRMATION_EVIDENCE_ROWS = 2


# Auto-validation (Q30) rests on having confirmed the agent found the right facility. Once
# coordinates resolve, distance from the seed location measures that directly. Beyond this
# threshold the identity is not established, so nothing for that site may auto-validate.
# A landfill footprint spans at most a couple of km and seed coordinates are polygon centroids,
# so 5 km is generous; tune it if legitimate sites are being flagged.
IDENTITY_DISTANCE_KM_THRESHOLD = 5.0


# Coordinate disagreements below this are a more precise fix on the same place, not a different
# facility, and reviewing them is wasted SME time. Measured on the pilot: four of five coordinate
# conflicts were 0.003-0.195 km apart; the fifth was 238 km and is the one worth a human.
COORDINATE_REVIEW_KM_THRESHOLD = 1.0


def haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    radius = 6371.0088
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = phi2 - phi1
    dlambda = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlambda / 2) ** 2
    return 2 * radius * math.asin(math.sqrt(a))


def parse_float(value: Any) -> float | None:
    text = normalize_scalar(value).replace(",", "")
    if not text:
        return None
    try:
        return float(text)
    except ValueError:
        return None


def normalize_url(url: Any) -> str:
    text = normalize_scalar(url)
    if not text:
        return ""
    parts = urlsplit(text.lower())
    netloc = parts.netloc[4:] if parts.netloc.startswith("www.") else parts.netloc
    path = parts.path.rstrip("/")
    return urlunsplit((parts.scheme or "https", netloc, path, "", ""))


# --- phase 2: arbitrate (pure, offline) -------------------------------------------------------
def extract_evidence(
    site: dict[str, Any],
    payload: dict[str, Any],
    run_id: str,
    dataset_version: str,
    access_date: date,
) -> tuple[dict[str, list[dict[str, Any]]], list[dict[str, Any]], list[dict[str, Any]], list[str]]:
    """Turn one cached response into evidence items grouped by attribute."""
    site_id = normalize_scalar(site.get("site_id"))
    by_attribute: dict[str, list[dict[str, Any]]] = {}
    evidence_rows: list[dict[str, Any]] = []
    source_entries: list[dict[str, Any]] = []
    warnings: list[str] = []
    order = 0

    for attr_index, attribute in enumerate(payload.get("attributes", []) or [], start=1):
        if not isinstance(attribute, dict):
            continue
        # A cached response from before a rename still carries the old attribute name.
        name = canonical_attribute(normalize_scalar(attribute.get("attribute_name")))
        if name not in REQUESTABLE_ATTRIBUTES:
            warnings.append(f"Unsupported attribute_name {name!r} dropped.")
            continue

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
            # Bin the CONVERTED metres, not the raw value: the source may have reported feet, and
            # the 5m boundary is only meaningful once the unit conversion above has run. A failed
            # conversion leaves conversion.value in its original unit, so skip binning entirely
            # there and let the conversion warning exclude the row.
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

        sources = [item for item in (attribute.get("sources") or []) if isinstance(item, dict)]
        for source_index, source in enumerate(sources, start=1):
            order += 1
            evidence_id = f"WAIEV-{site_id}-{attr_index:03d}-{source_index:03d}"
            source_id = f"WAISRC-{site_id}-{attr_index:03d}-{source_index:03d}"
            tier, rule = assign_source_tier(source, name)
            url = normalize_scalar(source.get("url"))

            item = {
                "evidence_id": evidence_id,
                "attribute_name": name,
                "value": value,
                "unit": conversion.unit,
                "tier": tier,
                "order": order,
                "url": url,
                "value_date": normalize_scalar(attribute.get("value_date")),
                "confidence": normalize_scalar(attribute.get("confidence_score")),
                "evidence_summary": normalize_scalar(attribute.get("evidence_summary")),
            }
            if url and not is_blank(value) and not numeric_note:
                by_attribute.setdefault(name, []).append(item)

            promotable = bool(url) and not is_blank(value) and is_promotable(tier) and not numeric_note
            exclusion = ""
            if not url:
                exclusion = "No clickable source URL."
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
                    "url": url,
                    "source_tier": tier_label(tier),
                    "agent_proposed_tier": normalize_scalar(source.get("source_tier")),
                    "tier_rule_applied": rule,
                    "evidence_summary": normalize_scalar(attribute.get("evidence_summary")),
                    "quoted_evidence_short": normalize_scalar(source.get("quoted_evidence_short")),
                    "search_terms_used": normalize_scalar(attribute.get("search_terms_used")),
                    "promotion_eligible": "TRUE" if promotable else "FALSE",
                    "exclusion_reason": exclusion,
                    "record_created_date": access_date.isoformat(),
                }
            )

            source_entries.append(
                {
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
            )

    return by_attribute, evidence_rows, source_entries, warnings


def dedupe_sources(entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Collapse the per-citation source entries into one row per normalized URL."""
    grouped: dict[str, dict[str, Any]] = {}
    for entry in entries:
        key = entry["url_normalized"] or entry["url"] or entry["source_id"]
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


def merge_payloads(payloads: list[dict[str, Any]]) -> dict[str, Any]:
    """Combine every pass's response for one site into a single payload.

    Attributes and leads accumulate across passes; search notes are joined so a tool failure in
    any pass stays visible.
    """
    if len(payloads) == 1:
        return payloads[0]
    merged: dict[str, Any] = {"site_id": payloads[0].get("site_id"), "attributes": [], "unverified_leads": []}
    notes = []
    for payload in payloads:
        merged["attributes"].extend(payload.get("attributes") or [])
        merged["unverified_leads"].extend(payload.get("unverified_leads") or [])
        note = normalize_scalar(payload.get("search_notes"))
        if note:
            notes.append(note)
    merged["search_notes"] = " | ".join(notes)
    return merged


def contradiction_rows(
    resolved: list[dict[str, Any]], evidence: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """The "Not a Waste Facility" verdicts, each beside what the same run found about closure.

    The likeliest false positive is a closed landfill misread as "not a landfill" - "it's a park
    now". A site called Not a Waste Facility AND reported closed is exactly that shape, so it is
    flagged for the reviewer rather than left to be noticed.
    """
    by_evidence_id = {row["evidence_id"]: row for row in evidence}
    closure: dict[str, list[str]] = {}
    for row in resolved:
        attribute = row["attribute_name"]
        value = normalize_scalar(row.get("resolved_value"))
        if attribute == "facility_status" and value == "Inactive":
            closure.setdefault(row["site_id"], []).append("facility_status = Inactive")
        elif attribute == "closing_year" and value:
            closure.setdefault(row["site_id"], []).append(f"closing_year = {value}")

    out: list[dict[str, Any]] = []
    for row in resolved:
        if row["attribute_name"] != "facility_type":
            continue
        if normalize_scalar(row.get("resolved_value")) != NOT_A_WASTE_FACILITY:
            continue
        winner = by_evidence_id.get(row.get("winning_evidence_id"), {})
        reported = closure.get(row["site_id"], [])
        out.append(
            {
                "site_id": row["site_id"],
                "site_name": row["site_name"],
                "country_iso3": row["country_iso3"],
                "facility_type": NOT_A_WASTE_FACILITY,
                "winning_source_tier": row["winning_source_tier"],
                "winning_source_url": row["winning_source_url"],
                "evidence_summary": winner.get("evidence_summary", ""),
                "quoted_evidence_short": winner.get("quoted_evidence_short", ""),
                "closure_also_reported": (
                    f"CHECK: {'; '.join(reported)} - is this a closed landfill misread as a "
                    "contradiction?"
                    if reported
                    else ""
                ),
                "validation_status": row["validation_status"],
            }
        )
    return out


def cover_consistency_warnings(resolved: list[dict[str, Any]]) -> list[str]:
    """Flag a has_biocover answer that contradicts the cover types found alongside it.

    They are searched independently and describe the same physical cover, so a disagreement means
    one of the two is wrong. Surfaced rather than auto-corrected: which one is wrong depends on
    the sources, and the reviewer has both rows in front of them.
    """
    values = {
        normalize_scalar(row.get("attribute_name")): normalize_scalar(row.get("resolved_value"))
        for row in resolved
    }
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


def append_distance_row(
    site: dict[str, Any],
    resolved: list[dict[str, Any]],
    reviewed_on: Any = None,
) -> float | None:
    """Compute distance between found and seed coordinates. No source, so no tier (Q12)."""
    found = {row["attribute_name"]: row for row in resolved}
    lat = parse_float(found.get("found_latitude", {}).get("resolved_value"))
    lon = parse_float(found.get("found_longitude", {}).get("resolved_value"))
    base_lat = parse_float(site.get("latitude"))
    base_lon = parse_float(site.get("longitude"))
    if None in (lat, lon, base_lat, base_lon):
        return None
    km = haversine_km(base_lat, base_lon, lat, lon)
    resolved.append(
        calculated_row(
            site,
            CALCULATED_ATTRIBUTES[0],
            f"{km:.3f}".rstrip("0").rstrip("."),
            "km",
            "Haversine distance between seed coordinates and source-backed found coordinates.",
            reviewed_on=reviewed_on,
        )
    )
    return km


def axis_offset_km(site: dict[str, Any], attribute_name: str, value: Any) -> float | None:
    """How far a single found coordinate sits from the seed, holding the other axis fixed.

    Computed per axis so a coordinate can be judged even when its counterpart was never found.
    """
    base_lat = parse_float(site.get("latitude"))
    base_lon = parse_float(site.get("longitude"))
    found = parse_float(value)
    if None in (base_lat, base_lon, found):
        return None
    if attribute_name == "found_latitude":
        return haversine_km(base_lat, base_lon, found, base_lon)
    if attribute_name == "found_longitude":
        return haversine_km(base_lat, base_lon, base_lat, found)
    return None


def settle_near_coordinates(site: dict[str, Any], resolved: list[dict[str, Any]]) -> int:
    """Stop sending sub-threshold coordinate differences to review (pilot feedback 5)."""
    settled = 0
    for row in resolved:
        name = normalize_scalar(row.get("attribute_name"))
        if name not in {"found_latitude", "found_longitude"}:
            continue
        if not needs_review(row) or not normalize_scalar(row.get("resolved_value")):
            continue
        km = axis_offset_km(site, name, row.get("resolved_value"))
        if km is None or km > COORDINATE_REVIEW_KM_THRESHOLD:
            continue
        row["validation_status"] = "Auto-validated"
        row["resolution_rule"] = (
            f"{row.get('resolution_rule', '')} Within {km:.3f} km of the seed location "
            f"(threshold {COORDINATE_REVIEW_KM_THRESHOLD:g} km); treated as the same place."
        ).strip()
        settled += 1
    return settled


def flag_identity_mismatch(resolved: list[dict[str, Any]], km: float | None) -> bool:
    """Revoke auto-validation for a site whose found coordinates are too far from the seed.

    Tier cannot detect a credible source describing the wrong facility. Distance can, so a large
    mismatch withdraws the identity assumption that auto-validation depends on.
    """
    if km is None or km <= IDENTITY_DISTANCE_KM_THRESHOLD:
        return False
    note = (
        f"Identity not confirmed: source-backed coordinates are {km:.1f} km from the seed "
        f"location (threshold {IDENTITY_DISTANCE_KM_THRESHOLD:g} km). Auto-validation withdrawn."
    )
    for row in resolved:
        if row.get("resolution") in {"Not found", "Calculated"}:
            continue
        if row.get("validation_status") == "Auto-validated":
            row["validation_status"] = "Needs review"
        existing = normalize_scalar(row.get("researcher_notes"))
        row["researcher_notes"] = f"{existing} {note}".strip() if existing else note
    return True


def payload_leads(
    cached: list[Path],
    sites_by_id: dict[str, dict[str, Any]],
    run_id: str,
) -> list[dict[str, Any]]:
    """Carry the agent's own unverified_leads through to the leads sheet."""
    out: list[dict[str, Any]] = []
    for path in cached:
        site_id = site_id_from_path(path)
        site = sites_by_id.get(site_id, {})
        try:
            payload = load_json(path)
        except (OSError, json.JSONDecodeError):
            continue
        for lead in payload.get("unverified_leads", []) or []:
            if isinstance(lead, dict):
                summary = normalize_scalar(lead.get("lead_summary") or lead.get("summary") or lead.get("note"))
                url = normalize_scalar(lead.get("url"))
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
                    "attribute_name": attribute,
                    "lead_value": value,
                    "source_tier": "",
                    "lead_summary": summary,
                    "url": url,
                    "exclusion_reason": "Agent reported as unverified.",
                }
            )
    return out


def run_arbitration(config: PipelineConfig) -> dict[str, Path]:
    access_date = date.today()
    sites, _headers = resolve_sites(config)
    sites_by_id = {normalize_scalar(site.get("site_id")): site for site in sites}

    cached = sorted(raw_dir(config.run_dir).glob("site_*.json"))
    if not cached:
        raise ValueError(f"No cached responses in {raw_dir(config.run_dir)}. Run the search phase first.")

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

        if not (payload.get("attributes") or []) and search_tool_failed(payload):
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
            site, payload, config.run_id, config.dataset_version, access_date
        )
        source_entries.extend(site_sources)
        for warning in warnings:
            parse_warnings.append(
                {
                    "run_id": config.run_id,
                    "site_id": site_id,
                    "site_name": normalize_scalar(site.get("site_name")),
                    "warning": warning,
                }
            )

        # Resolve everything we would ask for, plus anything the agent actually returned.
        # A follow-up pass answers attributes the current seed would no longer request, and
        # discarding that evidence because of a stale baseline would silently lose the finding.
        site_resolved: list[dict[str, Any]] = []
        to_resolve = list(requested_attributes(site))
        to_resolve += [name for name in by_attribute if name not in to_resolve]
        for name in to_resolve:
            site_resolved.append(resolve(site, name, by_attribute.get(name, []), reviewed_on=access_date))
        for warning in cover_consistency_warnings(site_resolved):
            parse_warnings.append(
                {
                    "run_id": config.run_id,
                    "site_id": site_id,
                    "site_name": normalize_scalar(site.get("site_name")),
                    "warning": warning,
                }
            )

        km = append_distance_row(site, site_resolved, reviewed_on=access_date)
        settle_near_coordinates(site, site_resolved)
        if flag_identity_mismatch(site_resolved, km):
            parse_warnings.append(
                {
                    "run_id": config.run_id,
                    "site_id": site_id,
                    "site_name": normalize_scalar(site.get("site_name")),
                    "warning": f"Identity not confirmed: found coordinates {km:.1f} km from seed location.",
                }
            )
        resolved_rows.extend(site_resolved)

        # Q8: cap evidence rows for pure confirmations; keep everything else.
        confirmed = {
            row["attribute_name"]
            for row in site_resolved
            if row["resolution"] == "Confirmed baseline"
        }
        kept_per_attribute: dict[str, int] = {}
        for row in site_evidence:
            name = row["attribute_name"]
            if name in confirmed:
                kept_per_attribute[name] = kept_per_attribute.get(name, 0) + 1
                if kept_per_attribute[name] > MAX_CONFIRMATION_EVIDENCE_ROWS:
                    continue
            evidence_rows.append(row)

        for row in site_resolved:
            if not routed_to_leads(row):
                continue
            leads.append(
                {
                    "run_id": config.run_id,
                    "site_id": site_id,
                    "site_name": normalize_scalar(site.get("site_name")),
                    "country_iso3": normalize_scalar(site.get("country_iso3")),
                    "attribute_name": row["attribute_name"],
                    "lead_value": row["resolved_value"],
                    "source_tier": row["winning_source_tier"] or row["best_tier_available"],
                    "lead_summary": row["resolution_rule"],
                    "url": row["winning_source_url"],
                    "exclusion_reason": row["resolution"],
                }
            )

        # Feedback 1 lets a low-tier source fill an empty baseline, so a piece of evidence the
        # tier gate marked ineligible can still end up winning. Listing it as a lead as well would
        # show the SME the same finding twice, in two places, which feedback 3 rules out.
        promoted_attributes = {
            row["attribute_name"]
            for row in site_resolved
            if normalize_scalar(row.get("resolved_value"))
        }

        for row in site_evidence:
            if row["promotion_eligible"] == "TRUE" or not row["exclusion_reason"]:
                continue
            if row["attribute_name"] in promoted_attributes:
                continue
            leads.append(
                {
                    "run_id": config.run_id,
                    "site_id": site_id,
                    "site_name": normalize_scalar(site.get("site_name")),
                    "country_iso3": normalize_scalar(site.get("country_iso3")),
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

    review_queue: list[dict[str, Any]] = []
    for row in resolved_rows:
        if not needs_review(row):
            continue
        queue_row = {header: row.get(header, "") for header in REVIEW_QUEUE_HEADERS}
        queue_row["evidence_summary"] = row.get("resolution_rule", "")
        review_queue.append(queue_row)

    contradictions = contradiction_rows(resolved_rows, evidence_rows)
    # How many arbitrated facilities were offered the verdict at all, so an empty Contradictions
    # tab can be read as "checked N, contradicted none" rather than "never checked".
    contradiction_checks = sum(
        1 for sid in by_site if sid in sites_by_id and needs_contradiction_check(sites_by_id[sid])
    )

    standard_records, standard_notes = build_standard_table(sites_by_id, resolved_rows)
    for note_site_id, note in standard_notes:
        note_site = sites_by_id.get(note_site_id, {})
        parse_warnings.append(
            {
                "run_id": config.run_id,
                "site_id": note_site_id,
                "site_name": normalize_scalar(note_site.get("site_name")),
                "warning": note,
            }
        )

    out = config.run_dir
    paths = {
        "evidence": out / "evidence.csv",
        "resolved": out / "resolved.csv",
        "sources": out / "sources.csv",
        "review_queue": out / "review_queue.csv",
        "leads": out / "supplementary_leads.csv",
        "warnings": out / "parse_warnings.csv",
        "standardized": out / f"std_facility_tbl_ai_search_{config.run_id}.csv",
        "retry": out / "sites_to_retry.csv",
    }
    write_csv_records(paths["evidence"], evidence_rows, EVIDENCE_HEADERS)
    write_csv_records(paths["resolved"], resolved_rows, RESOLVED_HEADERS)
    write_csv_records(paths["sources"], dedupe_sources(source_entries), SOURCES_HEADERS)
    write_csv_records(paths["review_queue"], review_queue, REVIEW_QUEUE_HEADERS)
    write_csv_records(paths["leads"], leads, SUPPLEMENTARY_LEADS_HEADERS)
    write_csv_records(paths["warnings"], parse_warnings, PARSE_WARNING_HEADERS)
    write_csv_records(
        paths["retry"], retry_sites, ["site_id", "site_name", "country_iso3", "reason", "search_notes"]
    )
    write_csv_records(
        paths["standardized"],
        [to_csv_row(record) for record in standard_records],
        STANDARDIZED_FACILITY_COLUMNS,
    )
    # Ship the load statement next to the data so a name-based load is the easy path.
    paths["load_sql"] = write_load_statement(paths["standardized"])

    paths["workbook"] = write_review_workbook(
        out / f"{config.run_id}_review.xlsx",
        run_config=[
            {"setting": "run_id", "value": config.run_id},
            {"setting": "dataset_version", "value": config.dataset_version},
            {
                "setting": "seed_source",
                "value": describe_seed(config),
            },
            {"setting": "sites_arbitrated", "value": len(cached)},
            {"setting": "arbitrated_at", "value": datetime.now().replace(microsecond=0).isoformat()},
            {"setting": "review_queue_rows", "value": len(review_queue)},
            {"setting": "contradiction_checks", "value": contradiction_checks},
            {"setting": "not_a_waste_facility", "value": len(contradictions)},
            {"setting": "standardized_records", "value": len(standard_records)},
        ],
        review_queue=review_queue,
        contradictions=contradictions,
        leads=leads,
        parse_warnings=parse_warnings,
    )

    print_summary(
        by_site, resolved_rows, evidence_rows, review_queue, standard_records, leads, parse_warnings, retry_sites
    )
    return paths


def print_summary(
    cached, resolved_rows, evidence_rows, review_queue, standard_records, leads, warnings, retry_sites
) -> None:
    from collections import Counter

    resolutions = Counter(row["resolution"] for row in resolved_rows)
    tiers = Counter(row["source_tier"] for row in evidence_rows)
    print(f"\nSites arbitrated:        {len(cached)}")
    print(f"Evidence rows:           {len(evidence_rows)}")
    print(f"Resolved rows:           {len(resolved_rows)}")
    print(f"Standardized records:    {len(standard_records)}")
    print(f"Leads:                   {len(leads)}")
    print(f"Parse warnings:          {len(warnings)}")
    if retry_sites:
        ids = " ".join(row["site_id"] for row in retry_sites)
        print(
            f"\n!! {len(retry_sites)} site(s) could not be searched - the agent's search tool failed."
        )
        print("   These are NOT 'no data found'. Retry them:")
        print(f"   uv run waste-ai-search search --run-id <run> --force --site-ids {ids}")
    print(f"\nSME review queue:        {len(review_queue)} rows")
    print("\nResolutions:")
    for name, count in resolutions.most_common():
        print(f"  {count:>5}  {name}")
    print("\nEvidence by assigned tier:")
    for name, count in sorted(tiers.items()):
        print(f"  {count:>5}  {name}")
