"""Resolve many pieces of evidence about one facility attribute into a single decision.

Pure: takes a seed row and a list of evidence dicts, returns one Resolved row. No network, no
database, no clock beyond what the caller passes in, so a rubric change can be re-arbitrated
offline against cached responses (Q17).
"""
from __future__ import annotations

import re

from collections import Counter
from typing import Any

from .credibility import (
    TIER_5,
    ai_filled_tiers,
    apply_staleness_penalty,
    attribute_baseline_tier,
    attribute_sources,
    backfilled_years,
    baseline_tier,
    is_promotable,
    may_override_baseline,
    parse_year,
    tier_label,
    NEWER_SOURCE_YEARS,
    RECENCY_OVERRIDE_ATTRIBUTES,
)
from .schema import (
    AI_SEARCH_DATA_SOURCE,
    ARRAY_TARGET_ATTRIBUTES,
    GAP_FILL_ATTRIBUTES,
    NUMERIC_ARRAY_TARGET_ATTRIBUTES,
    LEAD_ROUTED_RESOLUTIONS,
    NOTHING_TO_REVIEW,
    is_blank,
    normalize_for_compare,
    normalize_scalar,
)


# Identity attributes compare against the seed columns they would replace.
IDENTITY_BASELINE_COLUMNS = {
    "found_facility_name": "site_name",
    "found_latitude": "latitude",
    "found_longitude": "longitude",
}


def baseline_for(site: dict[str, Any], attribute_name: str) -> str:
    if attribute_name in IDENTITY_BASELINE_COLUMNS:
        return normalize_scalar(site.get(IDENTITY_BASELINE_COLUMNS[attribute_name], ""))
    if attribute_name in GAP_FILL_ATTRIBUTES:
        return normalize_scalar(site.get(attribute_name, ""))
    return ""


def comparable(value: Any, attribute_name: str = "") -> str:
    """Value form used for agreement and conflict tests.

    A list attribute compares as a set: "sand cover; other soil mixture" and the same two items in
    the other order, or with one repeated (the seed holds "Electricity Generation; Electricity
    Generation"), are the same value, not a conflict for a human to settle.
    """
    if attribute_name in ARRAY_TARGET_ATTRIBUTES | NUMERIC_ARRAY_TARGET_ATTRIBUTES:
        items = value if isinstance(value, (list, tuple)) else re.split(r"[;,]", normalize_scalar(value))
        keys = {comparable(item) for item in items if normalize_scalar(item)}
        return "; ".join(sorted(keys))
    text = normalize_for_compare(value)
    try:
        return f"{float(text):.6g}"
    except (TypeError, ValueError):
        return text


def date_key(value: Any) -> tuple[int, int, int]:
    """A value_date as (year, month, day) for ordering; missing parts sort as 0, undated as oldest."""
    match = re.search(r"\b(1[89]\d{2}|20\d{2})(?:-(\d{1,2}))?(?:-(\d{1,2}))?", normalize_scalar(value))
    if not match:
        return (0, 0, 0)
    return tuple(int(part) if part else 0 for part in match.groups())


def source_key(item: dict[str, Any], index: int) -> str:
    """Which source a piece of evidence came from: its exact URL's `source_id`, else itself."""
    return normalize_scalar(item.get("source_id")) or f"#{index}"


def source_counts(evidence: list[dict[str, Any]], attribute_name: str) -> Counter:
    """How many distinct sources give each value.

    A source is its exact URL (`source_id`), so one page cited twice for the same value - listed
    twice, or under two entries for the attribute - counts once. Evidence without a source_id
    counts once per item.
    """
    cited = {
        (comparable(item.get("value"), attribute_name), source_key(item, index))
        for index, item in enumerate(evidence)
    }
    return Counter(value for value, _source in cited)


def resolve(
    site: dict[str, Any],
    attribute_name: str,
    evidence: list[dict[str, Any]],
) -> dict[str, Any]:
    """Arbitrate one (facility, attribute) pair.

    Nothing is accepted automatically. A found value for an empty field needs review whatever its
    tier, and so does one that disagrees with ours from a source at the floor or above and at least
    as credible as ours; a weaker disagreement goes to leads. A confirmation of ours, or nothing
    found, leaves our database as it is, so there is nothing to review.
    """
    site_id = normalize_scalar(site.get("site_id"))
    baseline = baseline_for(site, attribute_name)

    # Baseline credibility, most specific provenance first:
    #   1. a value a previous AI pass supplied, tiered by the source that supplied it
    #   2. the value resolution ledger's per-attribute dataset
    #   3. the facility's composite contributing_data_sources, which takes the BEST of everything
    #      the facility draws on and therefore over-credits weaker individual values
    ai_tiers = ai_filled_tiers(site.get("ai_filled_fields"))
    per_attribute = attribute_sources(site.get("attribute_sources"))
    baseline_source = ""
    if attribute_name in ai_tiers:
        base_tier = ai_tiers[attribute_name]
        baseline_source = AI_SEARCH_DATA_SOURCE
        base_note = f"baseline supplied by a previous ai_search pass at {tier_label(base_tier)}"
    elif attribute_name in per_attribute:
        baseline_source = per_attribute[attribute_name]
        base_tier, base_note = attribute_baseline_tier(baseline_source)
    else:
        base_tier, base_note = baseline_tier(site.get("contributing_data_sources"))
        if not is_blank(baseline):
            base_note = f"{base_note}; no per-attribute provenance, fell back to the facility composite"

    backfills = backfilled_years(site.get("backfilled_fields"))
    is_backfilled = attribute_name in backfills
    # The year the baseline value describes: the year it was carried from, else the seed's year.
    baseline_year = backfills[attribute_name] if is_backfilled else parse_year(site.get("reference_year"))
    stale_note = ""
    if is_backfilled:
        base_tier, stale_note = apply_staleness_penalty(
            base_tier, attribute_name, site.get("reference_year"), backfills[attribute_name]
        )
    if stale_note:
        base_note = f"{base_note}; {stale_note}" if base_note else stale_note

    # These columns describe the baseline VALUE. With no baseline there is nothing to credit, and
    # showing the facility composite's tier against an empty field reads as a claim about a value
    # that does not exist. The fill path does not consult the tier anyway (F1).
    if is_blank(baseline):
        baseline_source = ""
        base_tier_label = ""
    else:
        base_tier_label = tier_label(base_tier)

    row: dict[str, Any] = {
        "site_id": site_id,
        "internal_facility_id": normalize_scalar(site.get("internal_facility_id") or site_id),
        "site_name": normalize_scalar(site.get("site_name")),
        "country_iso3": normalize_scalar(site.get("country_iso3")),
        "attribute_name": attribute_name,
        "baseline_value": baseline,
        "baseline_source": baseline_source,
        "baseline_tier": base_tier_label,
        # Whether the SEED carried this attribute forward from an older year during the fold.
        # It does NOT mean the search filled the value - that is resolution="Filled empty baseline".
        "baseline_carried_from_earlier_year": "TRUE" if is_backfilled else "FALSE",
        "baseline_staleness_note": base_note,
        "resolved_value": "",
        "resolved_unit": "",
        "resolution": "Not found",
        "resolution_rule": "",
        "winning_evidence_id": "",
        "winning_source_tier": "",
        "winning_source_url": "",
        "winning_link_status": "",
        "best_tier_available": "",
        "agreeing_source_count": 0,
        "dissenting_source_count": 0,
        "credibility_margin": "",
        "value_date": "",
        "confidence_score": "",
        "validation_status": "Needs review",
        "reviewer": "",
        "reviewed_date": "",
        "researcher_notes": "",
    }

    if not evidence:
        row["resolution_rule"] = "No source-backed evidence returned."
        row["validation_status"] = NOTHING_TO_REVIEW
        return row

    tiers = [int(item.get("tier", TIER_5)) for item in evidence]
    row["best_tier_available"] = tier_label(min(tiers))

    # An empty baseline is filled from whatever was found, regardless of tier: a sourced value
    # beats no value at all. A human still looks at it, whatever the tier.
    baseline_empty = is_blank(baseline)
    candidates = evidence if baseline_empty else [item for item in evidence if is_promotable(int(item.get("tier", TIER_5)))]

    if not candidates:
        # Non-empty baseline and nothing above the floor: a weaker contradiction, so it goes to
        # the leads sheet rather than consuming review time. Sources are counted, not citations.
        found = len({source_key(item, index) for index, item in enumerate(evidence)})
        row["resolution"] = "Conflict - lower credibility"
        row["resolution_rule"] = (
            f"{found} source(s) found, best {tier_label(min(tiers))}, against a "
            f"{tier_label(base_tier)} baseline; routed to leads."
        )
        row["validation_status"] = "Routed to leads"
        return row

    # Winner: best tier, then newest (undated counts as oldest), then most corroborated, then
    # first seen (WP-558: within a tier, a newer source beats an older one).
    counts = source_counts(candidates, attribute_name)
    candidates.sort(
        key=lambda item: (
            int(item.get("tier", TIER_5)),
            tuple(-part for part in date_key(item.get("value_date"))),
            -counts[comparable(item.get("value"), attribute_name)],
            int(item.get("order", 0)),
        )
    )
    winner = candidates[0]
    win_key = comparable(winner.get("value"), attribute_name)
    win_tier = int(winner.get("tier", TIER_5))
    win_year = parse_year(winner.get("value_date"))
    recency_notes: list[str] = []
    rivals = [
        item for item in candidates
        if int(item.get("tier", TIER_5)) == win_tier and comparable(item.get("value"), attribute_name) != win_key
    ]
    if rivals and win_year:
        recency_notes.append(
            f"Newest of the disagreeing {tier_label(win_tier)} sources "
            f"(dated {normalize_scalar(winner.get('value_date'))}) chosen."
        )

    # For status, a lower-tier source down to the promotion floor may win if it is at least
    # NEWER_SOURCE_YEARS newer - a dated permit (Tier 1) against recent news (Tier 3).
    if attribute_name in RECENCY_OVERRIDE_ATTRIBUTES and win_year:
        eligible = [
            item for item in candidates
            if int(item.get("tier", TIER_5)) > win_tier
            and is_promotable(int(item.get("tier", TIER_5)))
            and (parse_year(item.get("value_date")) or 0) >= win_year + NEWER_SOURCE_YEARS
        ]
        # The newest eligible source, agreeing or not; tier, then corroboration, only break ties.
        # It overrides only if it disagrees - a newer source agreeing with the winner confirms it.
        newest = min(
            eligible,
            key=lambda e: (
                tuple(-part for part in date_key(e.get("value_date"))),
                int(e.get("tier", TIER_5)),
                -counts[comparable(e.get("value"), attribute_name)],
                int(e.get("order", 0)),
            ),
            default=None,
        )
        if newest is not None and comparable(newest.get("value"), attribute_name) != win_key:
            item = newest
            year = parse_year(item.get("value_date"))
            new_tier = int(item.get("tier", TIER_5))
            recency_notes.append(
                f"{tier_label(new_tier)} source dated {year} replaces the {tier_label(win_tier)} "
                f"source dated {win_year}: status from a source {year - win_year} years newer wins."
            )
            winner, win_year, win_tier = item, year, new_tier
            win_key = comparable(winner.get("value"), attribute_name)

    row["resolved_value"] = normalize_scalar(winner.get("value"))
    row["resolved_unit"] = normalize_scalar(winner.get("unit"))
    row["winning_evidence_id"] = normalize_scalar(winner.get("evidence_id"))
    row["winning_source_tier"] = tier_label(win_tier)
    row["winning_source_url"] = normalize_scalar(winner.get("url"))
    # OK, Unverified, or blank when the run has no link check. Unverified usually means a server
    # refusing bots, so the SME is told the link may not open for them either.
    row["winning_link_status"] = normalize_scalar(winner.get("link_status"))
    row["agreeing_source_count"] = counts[win_key]
    row["dissenting_source_count"] = sum(n for key, n in counts.items() if key != win_key)
    row["value_date"] = normalize_scalar(winner.get("value_date"))
    row["confidence_score"] = normalize_scalar(winner.get("confidence"))
    row["credibility_margin"] = base_tier - win_tier

    matches_baseline = not baseline_empty and comparable(baseline, attribute_name) == win_key

    if matches_baseline:
        # Kept in evidence.csv, but neither queued nor loaded, so the search doesn't become the
        # credited source for a value nobody reviewed.
        row["resolution"] = "Confirmed baseline"
        row["resolution_rule"] = f"{tier_label(win_tier)} source agrees with {tier_label(base_tier)} baseline."
        row["validation_status"] = NOTHING_TO_REVIEW
    elif baseline_empty:
        row["resolution"] = "Filled empty baseline"
        row["resolution_rule"] = f"Baseline empty; filled from {tier_label(win_tier)} source."
        row["validation_status"] = "Needs review"
    elif win_tier < base_tier:
        # Candidate is more credible than the baseline: a decision worth a human's time.
        if may_override_baseline(win_tier):
            row["resolution"] = "Overrides baseline"
            row["resolution_rule"] = (
                f"{tier_label(win_tier)} source beats {tier_label(base_tier)} baseline "
                f"by {base_tier - win_tier} tier(s)."
            )
        else:
            row["resolution"] = "Conflict - needs review"
            row["resolution_rule"] = (
                f"{tier_label(win_tier)} source beats the {tier_label(base_tier)} baseline but "
                "may not override a non-empty value."
            )
        row["validation_status"] = "Needs review"
    elif win_tier == base_tier:
        # Equal credibility has no automatic winner, and discarding it would lose real signal.
        row["resolution"] = "Conflict - needs review"
        row["resolution_rule"] = (
            f"Candidate disagrees with baseline at equal credibility ({tier_label(win_tier)}); "
            "no automatic winner."
        )
        row["validation_status"] = "Needs review"
    elif (
        attribute_name in RECENCY_OVERRIDE_ATTRIBUTES
        and win_tier > base_tier
        and is_promotable(win_tier)
        and win_year
        and baseline_year
        and win_year >= baseline_year + NEWER_SOURCE_YEARS
    ):
        # A status this much newer than the baseline may replace it despite a lower tier.
        row["resolution"] = "Overrides baseline"
        row["resolution_rule"] = (
            f"{tier_label(win_tier)} source dated {win_year} is {win_year - baseline_year} years newer "
            f"than the {tier_label(base_tier)} baseline ({baseline_year}); a status this much newer "
            "may replace it, always for review."
        )
        row["validation_status"] = "Needs review"
    else:
        row["resolution"] = "Conflict - lower credibility"
        row["resolution_rule"] = (
            f"{tier_label(win_tier)} candidate is weaker than the {tier_label(base_tier)} "
            "baseline; baseline retained and the candidate routed to leads."
        )
        row["validation_status"] = "Routed to leads"

    if recency_notes:
        row["resolution_rule"] = " ".join([row["resolution_rule"], *recency_notes]).strip()
    return row


def calculated_row(
    site: dict[str, Any],
    attribute_name: str,
    value: Any,
    unit: str,
    note: str,
) -> dict[str, Any]:
    """A locally computed value with no source and therefore no tier (Q12). Never queued or loaded."""
    site_id = normalize_scalar(site.get("site_id"))
    return {
        "site_id": site_id,
        "internal_facility_id": normalize_scalar(site.get("internal_facility_id") or site_id),
        "site_name": normalize_scalar(site.get("site_name")),
        "country_iso3": normalize_scalar(site.get("country_iso3")),
        "attribute_name": attribute_name,
        "baseline_value": "",
        "baseline_source": "",
        "baseline_tier": "",
        "baseline_carried_from_earlier_year": "FALSE",
        "baseline_staleness_note": "",
        "resolved_value": normalize_scalar(value),
        "resolved_unit": unit,
        "resolution": "Calculated",
        "resolution_rule": note,
        "winning_evidence_id": "",
        "winning_source_tier": "",
        "winning_source_url": "",
        "best_tier_available": "",
        "agreeing_source_count": 0,
        "dissenting_source_count": 0,
        "credibility_margin": "",
        "value_date": "",
        "confidence_score": "",
        "validation_status": NOTHING_TO_REVIEW,
        "reviewer": "",
        "reviewed_date": "",
        "researcher_notes": note,
    }


def needs_review(row: dict[str, Any]) -> bool:
    return normalize_scalar(row.get("validation_status")) == "Needs review"


def routed_to_leads(row: dict[str, Any]) -> bool:
    return normalize_scalar(row.get("resolution")) in LEAD_ROUTED_RESOLUTIONS
