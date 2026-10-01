"""Source credibility: tier assignment, baseline tiering, and promotion gating.

Tier is decided *locally* from the source's publisher class, never taken from the agent. Measured
against the pilot responses, the agent's self-assigned tier put the same source class in three
different tiers (21 news reports at Tier 2, 4 at Tier 3, 1 at Tier 4), so its number is retained
only as an audit field.

Pure and dependency-free so the rubric stays testable and arbitration runs offline.
"""
from __future__ import annotations

import re
from typing import Any

from .schema import canonical_attribute, normalize_scalar


# Tier 5 closes the scale: a source we could not classify is weaker than one we classified as
# weak, and it belongs on the same ordered scale rather than as an out-of-band sentinel. Keeping
# it in-scale means credibility_margin stays meaningful and comparisons need no special cases.
TIER_1, TIER_2, TIER_3, TIER_4, TIER_5 = 1, 2, 3, 4, 5

TIER_LABELS = {
    TIER_1: "Tier 1",
    TIER_2: "Tier 2",
    TIER_3: "Tier 3",
    TIER_4: "Tier 4",
    TIER_5: "Tier 5",
}

TIER_DEFINITIONS = {
    "Tier 1": "Regulator, environmental agency, official permit or registry, court decision, or the facility operator's own disclosure.",
    "Tier 2": "Peer-reviewed literature, IFI / UN / development-bank dataset, or government statistical office.",
    "Tier 3": "Established news outlet, industry trade press, or established NGO field report.",
    "Tier 4": "Aggregator, wiki, blog, map directory, or any undated and unattributed page.",
    "Tier 5": "Source type could not be established. Every source should fit one of the listed source_type values; if none fits, the finding belongs in unverified_leads instead.",
}

# Closed vocabulary the agent must choose from. Free text here is what produced composites like
# "Regulator / Court decision" and "ScienceDirect / academic journal" in the pilot responses.
SOURCE_TYPES = [
    "Regulator",
    "Court decision",
    "Permit or registry",
    "Municipal document",
    "Environmental impact assessment",
    "Operator disclosure",
    "Peer-reviewed article",
    "Government statistics",
    "Intergovernmental dataset",
    "News report",
    "Trade press",
    "NGO report",
    "Company directory listing",
    "Map or coordinate directory",
    "Wiki or aggregator",
]

SOURCE_TYPE_TIERS = {
    "regulator": TIER_1,
    "court decision": TIER_1,
    "permit or registry": TIER_1,
    "municipal document": TIER_1,
    "environmental impact assessment": TIER_1,
    "operator disclosure": TIER_1,
    "peer-reviewed article": TIER_2,
    "government statistics": TIER_2,
    "intergovernmental dataset": TIER_2,
    "news report": TIER_3,
    "trade press": TIER_3,
    "ngo report": TIER_3,
    "company directory listing": TIER_4,
    "map or coordinate directory": TIER_4,
    "wiki or aggregator": TIER_4,
}

# Legacy free-text source_type values seen in the pilot responses, mapped onto the closed vocabulary.
LEGACY_SOURCE_TYPES = {
    "regulator / court decision": "court decision",
    "eia": "environmental impact assessment",
    "operator website": "operator disclosure",
    "operator website / company profile": "company directory listing",
    "journal article": "peer-reviewed article",
    "academic journal": "peer-reviewed article",
    "newswire": "news report",
    "map / coordinate directory": "map or coordinate directory",
}

# Baseline provenance tokens from consolidated_facility.contributing_data_sources, plus the two
# spec tokens absent from the seed. Settled in Q7/Q13/Q25.
BASELINE_DATASET_TIERS = {
    "usa_ghgrp_2026": TIER_1,
    "canada_ghgrp_2021": TIER_1,
    "eprtr_2022": TIER_1,
    "mexico_inegi_2016": TIER_1,
    "lmop_2024": TIER_1,
    "sinir_2024": TIER_1,
    "waste_atlas_landfills_2013": TIER_3,
    "waste_atlas_dumpsites_2013": TIER_3,
    "manual_entry": TIER_3,
    "gpw_2021": TIER_4,
    "osm_2022": TIER_4,
}

# Staleness penalty applies only to genuinely time-varying quantities (Q10/Q16).
STALENESS_PENALTY_ATTRIBUTES = {
    "annual_incoming_waste_metric_tonnes",
    "waste_in_place_metric_tonnes",
}
STALENESS_GAP_YEARS = 3
STALENESS_TIER_FLOOR = TIER_3

# Source authority is attribute-dependent. A map gazetteer is a poor source for operational
# metadata but a purpose-built one for a location, and the pilot showed 14 of 15 coordinate
# sources floored at Tier 4 — which left identity unconfirmable and the distance QA check dead.
# The exception is deliberately narrow: only coordinates, and only from a coordinate directory.
# Wikis and aggregators stay at Tier 4 even for coordinates; they are not gazetteers.
# Tier 3 is the right level: matching coordinates confirm the baseline, differing ones become a
# reviewable conflict with a computed distance, and neither can silently override.
ATTRIBUTE_SOURCE_TYPE_TIERS = {
    ("found_latitude", "map or coordinate directory"): TIER_3,
    ("found_longitude", "map or coordinate directory"): TIER_3,
}


# A source with no publication date cannot exceed Tier 3 whatever its publisher (Q2).
UNDATED_TIER_CAP = TIER_3

# Tier 4 and Unrated do not auto-promote when a baseline value already exists. An EMPTY baseline
# is filled regardless of tier (pilot feedback 1): anything sourced beats nothing, and the fill
# still needs review unless the tier is high enough to auto-validate.
MAX_PROMOTABLE_TIER = TIER_3
# Only Tier 1-2 may override a non-empty baseline (Q3).
MAX_OVERRIDE_TIER = TIER_2
# Tier 1-2 fills auto-validate; Tier 3 fills go to review (Q30).
MAX_AUTO_VALIDATE_TIER = TIER_2


def tier_label(tier: int) -> str:
    return TIER_LABELS.get(tier, TIER_LABELS[TIER_5])


def normalize_source_type(value: Any) -> str:
    text = normalize_scalar(value).lower().strip()
    if not text:
        return ""
    if text in SOURCE_TYPE_TIERS:
        return text
    if text in LEGACY_SOURCE_TYPES:
        return LEGACY_SOURCE_TYPES[text]
    for legacy, mapped in LEGACY_SOURCE_TYPES.items():
        if text.startswith(legacy):
            return mapped
    head = re.split(r"\s*[/|,]\s*", text)[0].strip()
    if head in SOURCE_TYPE_TIERS:
        return head
    if head in LEGACY_SOURCE_TYPES:
        return LEGACY_SOURCE_TYPES[head]
    return ""


def assign_source_tier(source: dict[str, Any], attribute_name: str | None = None) -> tuple[int, str]:
    """Return (tier, rule_applied) for one source, ignoring any tier the agent proposed.

    `attribute_name` enables the narrow per-attribute exceptions in ATTRIBUTE_SOURCE_TYPE_TIERS.
    """
    reported = normalize_scalar(source.get("source_type"))
    source_type = normalize_source_type(reported)
    if not source_type:
        detail = f"source_type {reported!r} is not a recognized type" if reported else "no source_type given"
        return TIER_5, f"{detail}; unclassifiable."

    override = ATTRIBUTE_SOURCE_TYPE_TIERS.get((attribute_name or "", source_type))
    if override is not None:
        tier = override
        rules = [f"source_type {source_type!r} -> {tier_label(tier)} for {attribute_name}"]
    else:
        tier = SOURCE_TYPE_TIERS.get(source_type, TIER_5)
        rules = [f"source_type {source_type!r} -> {tier_label(tier)}"]
    if not normalize_scalar(source.get("publication_date")):
        if tier < UNDATED_TIER_CAP:
            rules.append(f"no publication_date; capped at {tier_label(UNDATED_TIER_CAP)}")
            tier = UNDATED_TIER_CAP
        else:
            rules.append("no publication_date")
    return tier, "; ".join(rules)


def baseline_tier(contributing_data_sources: Any) -> tuple[int, str]:
    """Best (lowest) tier among the datasets that contributed the baseline value."""
    text = normalize_scalar(contributing_data_sources)
    tokens = [token.strip().lower() for token in text.split("+") if token.strip()]
    tiers = [BASELINE_DATASET_TIERS[token] for token in tokens if token in BASELINE_DATASET_TIERS]
    unknown = [token for token in tokens if token not in BASELINE_DATASET_TIERS]
    if not tiers:
        note = f"no tiered dataset in {text!r}" if text else "no baseline provenance"
        return TIER_5, note
    best = min(tiers)
    note = f"best of {sorted(set(tiers))}"
    if unknown:
        note += f"; untiered tokens ignored: {', '.join(sorted(unknown))}"
    return best, note


def apply_staleness_penalty(
    tier: int,
    attribute_name: str,
    reference_year: Any,
    backfill_year: Any,
) -> tuple[int, str]:
    """Demote an aged backfilled baseline one tier, floored at Tier 3 (Q10/Q16).

    Tiers already at or below the floor are left alone, which now also covers Tier 5.
    """
    if attribute_name not in STALENESS_PENALTY_ATTRIBUTES or tier >= STALENESS_TIER_FLOOR:
        return tier, ""
    ref = parse_year(reference_year)
    back = parse_year(backfill_year)
    if ref is None or back is None:
        return tier, ""
    gap = ref - back
    if gap < STALENESS_GAP_YEARS:
        return tier, ""
    return tier + 1, f"backfilled {gap}y before reference year; demoted to {tier_label(tier + 1)}"


def parse_year(value: Any) -> int | None:
    match = re.search(r"\b(1[89]\d{2}|20\d{2})\b", normalize_scalar(value))
    return int(match.group(1)) if match else None


TIER_BY_LABEL = {label: tier for tier, label in TIER_LABELS.items()}


def ai_filled_tiers(ai_filled_fields: Any) -> dict[str, int]:
    """Parse `field@Tier N; field@Tier N` written by refresh-seed.

    A baseline value that a previous AI pass supplied must be tiered by the source that supplied
    it, otherwise the next pass would compare new evidence against the seed datasets' tier and
    misjudge who wins.
    """
    out: dict[str, int] = {}
    for item in normalize_scalar(ai_filled_fields).split(";"):
        item = item.strip()
        if "@" not in item:
            continue
        field, _, label = item.rpartition("@")
        tier = TIER_BY_LABEL.get(label.strip())
        field = canonical_attribute(field)
        if field and tier is not None:
            out[field.strip()] = tier
    return out


def attribute_sources(attribute_sources_text: Any) -> dict[str, str]:
    """Parse the seed's `attribute@dataset` provenance written from the value resolution ledger."""
    out: dict[str, str] = {}
    for item in normalize_scalar(attribute_sources_text).split(";"):
        item = item.strip()
        if "@" not in item:
            continue
        attribute, _, source = item.rpartition("@")
        attribute = canonical_attribute(attribute)
        if attribute and source.strip():
            out[attribute] = source.strip()
    return out


def attribute_baseline_tier(source_token: Any) -> tuple[int, str]:
    """Tier one dataset token - the source that supplied a single attribute's baseline value."""
    token = normalize_scalar(source_token).lower()
    if not token:
        return TIER_5, "no per-attribute provenance"
    tier = BASELINE_DATASET_TIERS.get(token)
    if tier is None:
        return TIER_5, f"dataset {token!r} carries no tier"
    return tier, f"baseline from {token}"


def backfilled_years(backfilled_fields: Any) -> dict[str, int]:
    """Parse the seed's `field@year; field@year` provenance string."""
    out: dict[str, int] = {}
    for item in normalize_scalar(backfilled_fields).split(";"):
        item = item.strip()
        if "@" not in item:
            continue
        field, _, year = item.rpartition("@")
        parsed = parse_year(year)
        if field.strip() and parsed is not None:
            out[field.strip()] = parsed
    return out


def is_promotable(tier: int) -> bool:
    return tier <= MAX_PROMOTABLE_TIER


def may_override_baseline(tier: int) -> bool:
    return tier <= MAX_OVERRIDE_TIER


def may_auto_validate(tier: int) -> bool:
    return tier <= MAX_AUTO_VALIDATE_TIER


def best_source(
    sources: list[dict[str, Any]],
    attribute_name: str | None = None,
) -> tuple[dict[str, Any], int, str]:
    """Pick the most credible source with a URL, replacing the old first-with-a-URL behavior."""
    scored: list[tuple[int, int, dict[str, Any], str]] = []
    for index, source in enumerate(sources):
        if not normalize_scalar(source.get("url")):
            continue
        tier, rule = assign_source_tier(source, attribute_name)
        scored.append((tier, index, source, rule))
    if not scored:
        return {}, TIER_5, "No source with a clickable URL."
    scored.sort(key=lambda item: (item[0], item[1]))
    tier, _index, source, rule = scored[0]
    return source, tier, rule
