"""Resolve many pieces of evidence about one facility attribute into a single decision.

Pure: takes a seed row and a list of evidence dicts, returns one Resolved row. No network, no
database, no clock beyond what the caller passes in, so a rubric change can be re-arbitrated
offline against cached responses (Q17).
"""
from __future__ import annotations

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
    may_auto_validate,
    may_override_baseline,
    tier_label,
)
from .schema import (
    ALWAYS_REVIEW_ATTRIBUTES,
    AI_SEARCH_DATA_SOURCE,
    AUTO_REVIEWER,
    GAP_FILL_ATTRIBUTES,
    LEAD_ROUTED_RESOLUTIONS,
    is_blank,
    normalize_for_compare,
    normalize_scalar,
)


# Identity attributes compare against the seed columns they would replace.
IDENTITY_BASELINE_COLUMNS = {
    "found_site_name": "site_name",
    "found_latitude": "latitude",
    "found_longitude": "longitude",
}


def baseline_for(site: dict[str, Any], attribute_name: str) -> str:
    if attribute_name in IDENTITY_BASELINE_COLUMNS:
        return normalize_scalar(site.get(IDENTITY_BASELINE_COLUMNS[attribute_name], ""))
    if attribute_name in GAP_FILL_ATTRIBUTES:
        return normalize_scalar(site.get(attribute_name, ""))
    return ""


def comparable(value: Any) -> str:
    """Value form used for agreement and conflict tests."""
    text = normalize_for_compare(value)
    try:
        return f"{float(text):.6g}"
    except (TypeError, ValueError):
        return text


def resolve(
    site: dict[str, Any],
    attribute_name: str,
    evidence: list[dict[str, Any]],
    reviewed_on: Any = None,
) -> dict[str, Any]:
    """Arbitrate one (facility, attribute) pair.

    `reviewed_on` stamps rows the pipeline signs off itself, so an auto-validated row carries the
    same reviewer/date/notes a human would have left rather than three blank columns.
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
        row["validation_status"] = "Auto-validated"
        stamp_auto_review(row, reviewed_on)
        return row

    tiers = [int(item.get("tier", TIER_5)) for item in evidence]
    row["best_tier_available"] = tier_label(min(tiers))

    # An empty baseline is filled from whatever was found, regardless of tier: a sourced value
    # beats no value at all. Tier still decides whether a human has to look at it.
    baseline_empty = is_blank(baseline)
    candidates = evidence if baseline_empty else [item for item in evidence if is_promotable(int(item.get("tier", TIER_5)))]

    if not candidates:
        # Non-empty baseline and nothing above the floor: a weaker contradiction, so it goes to
        # the leads sheet rather than consuming review time.
        row["resolution"] = "Conflict - lower credibility"
        row["resolution_rule"] = (
            f"{len(evidence)} source(s) found, best {tier_label(min(tiers))}, against a "
            f"{tier_label(base_tier)} baseline; routed to leads."
        )
        row["validation_status"] = "Routed to leads"
        return row

    # Winner: best tier, then most corroborated, then first seen.
    counts = Counter(comparable(item.get("value")) for item in candidates)
    candidates.sort(
        key=lambda item: (
            int(item.get("tier", TIER_5)),
            -counts[comparable(item.get("value"))],
            int(item.get("order", 0)),
        )
    )
    winner = candidates[0]
    win_key = comparable(winner.get("value"))
    win_tier = int(winner.get("tier", TIER_5))

    row["resolved_value"] = normalize_scalar(winner.get("value"))
    row["resolved_unit"] = normalize_scalar(winner.get("unit"))
    row["winning_evidence_id"] = normalize_scalar(winner.get("evidence_id"))
    row["winning_source_tier"] = tier_label(win_tier)
    row["winning_source_url"] = normalize_scalar(winner.get("url"))
    row["agreeing_source_count"] = counts[win_key]
    row["dissenting_source_count"] = sum(n for key, n in counts.items() if key != win_key)
    row["value_date"] = normalize_scalar(winner.get("value_date"))
    row["confidence_score"] = normalize_scalar(winner.get("confidence"))
    row["credibility_margin"] = base_tier - win_tier

    matches_baseline = not baseline_empty and comparable(baseline) == win_key

    if matches_baseline:
        row["resolution"] = "Confirmed baseline"
        row["resolution_rule"] = f"{tier_label(win_tier)} source agrees with {tier_label(base_tier)} baseline."
        row["validation_status"] = "Auto-validated"
    elif baseline_empty:
        row["resolution"] = "Filled empty baseline"
        row["resolution_rule"] = f"Baseline empty; filled from {tier_label(win_tier)} source."
        row["validation_status"] = "Auto-validated" if may_auto_validate(win_tier) else "Needs review"
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
    else:
        row["resolution"] = "Conflict - lower credibility"
        row["resolution_rule"] = (
            f"{tier_label(win_tier)} candidate is weaker than the {tier_label(base_tier)} "
            "baseline; baseline retained and the candidate routed to leads."
        )
        row["validation_status"] = "Routed to leads"

    # Some verdicts are never the pipeline's to sign off. Without this a Tier 1-2 source calling a
    # site a quarry would take the ordinary empty-baseline path above and auto-validate.
    if attribute_name in ALWAYS_REVIEW_ATTRIBUTES and row["resolved_value"]:
        row["validation_status"] = "Needs review"
        row["resolution_rule"] = (
            f"{row['resolution_rule']} Always reviewed: a contradiction verdict is never "
            "signed off automatically, whatever the tier."
        )

    stamp_auto_review(row, reviewed_on, agent_note=normalize_scalar(winner.get("evidence_summary")))
    return row


def stamp_auto_review(row: dict[str, Any], reviewed_on: Any, agent_note: str = "") -> None:
    """Record the agent as the reviewer on any row no human needs to see."""
    if normalize_scalar(row.get("validation_status")) != "Auto-validated":
        return
    row["reviewer"] = AUTO_REVIEWER
    if reviewed_on is not None:
        row["reviewed_date"] = normalize_scalar(reviewed_on)
    note = agent_note or normalize_scalar(row.get("resolution_rule"))
    existing = normalize_scalar(row.get("researcher_notes"))
    if note and note not in existing:
        row["researcher_notes"] = f"{existing} {note}".strip() if existing else note


def calculated_row(
    site: dict[str, Any],
    attribute_name: str,
    value: Any,
    unit: str,
    note: str,
    reviewed_on: Any = None,
) -> dict[str, Any]:
    """A locally computed value with no source and therefore no tier (Q12)."""
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
        "validation_status": "Auto-validated",
        "reviewer": AUTO_REVIEWER,
        "reviewed_date": normalize_scalar(reviewed_on) if reviewed_on is not None else "",
        "researcher_notes": note,
    }


def needs_review(row: dict[str, Any]) -> bool:
    return normalize_scalar(row.get("validation_status")) == "Needs review"


def routed_to_leads(row: dict[str, Any]) -> bool:
    return normalize_scalar(row.get("resolution")) in LEAD_ROUTED_RESOLUTIONS
