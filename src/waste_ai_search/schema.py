from __future__ import annotations

import re
from datetime import date, datetime
from typing import Any

from .db_enums import (
    COVER_TYPE_VALUES,
    FACILITY_STATUS_VALUES,
    FACILITY_TYPE_VALUES,
    GCCS_CURRENT_PROJECT_STATUS_VALUES,
    GCCS_ENERGY_PROJECT_TYPE_VALUES,
    WASTE_DEPTH_VALUES,
)


GEOCODE_FIELDS = [
    "municipality",
    "admin1",
    "admin2",
    "formatted_address",
    "geocoder_provider",
    "geocoder_confidence",
    "geocoded_at",
    "geocode_status",
]

WASTE_SITE_BASE_EXTRA_FIELDS = [
    "site_id",
    "site_name",
    "country_iso3",
    "input_area_square_meters",
    "pilot_selection_reason",
    # The source's own name for the facility, before translation, and the language it is in.
    "original_site_name",
    "source_language",
    "name_data_source",
    *GEOCODE_FIELDS,
]


# --- target attributes -------------------------------------------------------------------------
# Metadata attributes requested only when the facility's baseline value is empty (gap-fill, Q29).
GAP_FILL_ATTRIBUTES = [
    "facility_status",
    "facility_type",
    "opening_year",
    "closing_year",
    "has_landfill_gas_collection",
    "annual_incoming_waste_metric_tonnes",
    "waste_in_place_metric_tonnes",
    "area_square_meters",
    "waste_depth",
    "has_cover",
    "cover_types",
    "has_biocover",
    "gccs_ch4_flared_metric_tonnes",
    "gccs_ch4_generated_metric_tonnes",
    "gccs_ch4_collected_metric_tonnes",
    "gccs_ch4_flow_to_project_metric_tonnes",
    "gccs_energy_project_type",
    "gccs_current_project_status",
    "gccs_collection_efficiency",
]

# Always requested. These are how we know the agent found the *right* facility, so they are not
# gap-filled: identity confirmation is what makes an auto-validated fill defensible (Q30).
IDENTITY_ATTRIBUTES = [
    "found_site_name",
    "found_latitude",
    "found_longitude",
]

# --- country policy ------------------------------------------------------------------------
# US facilities are already well covered by usa_ghgrp_2026 and lmop_2024, both Tier 1, so an AI
# search there mostly produces equal-tier conflicts for a human to adjudicate rather than new
# data. Excluded by default; --include-excluded-countries or naming the country explicitly
# overrides it.
DEFAULT_EXCLUDED_ISO3 = {"USA"}

# Countries where the ONLY thing worth searching is the coordinates, and only for facilities whose
# recorded location is flagged inexact. Every other attribute for these facilities comes from a
# government source, so searching it risks overwriting better data than the search can find.
# No facility currently satisfies this - `is_location_exact` is TRUE corpus-wide - so the rule
# matches nothing until inexact-location facilities enter the consolidation. It is a default now
# so that those facilities are scoped correctly the moment they arrive.
COORDINATES_ONLY_ISO3 = {"BRA"}

COORDINATE_ATTRIBUTES = ["found_latitude", "found_longitude"]


# --- site-type contradiction (WP-525) ---------------------------------------------------------
# A contradiction detector, not a verifier. The agent answers only when a source positively says
# the site is something other than a waste facility - a quarry, a mine, a yard that never took
# waste. Finding nothing is recorded as "No contradiction found", never as a negative.
#
# Deliberately outside GAP_FILL_ATTRIBUTES, so seed_refresh never merges an unreviewed verdict
# into the next pass's seed, and outside ATTRIBUTE_TO_STANDARD_COLUMN, so it can never reach the
# standardized table: the verdict lives in the review layer only.
SITE_TYPE_CONTRADICTION = "site_type_contradiction"
SITE_TYPE_CONTRADICTED = "Contradicted"
NO_CONTRADICTION_FOUND = "No contradiction found"
SITE_TYPE_CONTRADICTION_VALUES = [SITE_TYPE_CONTRADICTED, NO_CONTRADICTION_FOUND]
CONTRADICTION_CHECK_ATTRIBUTES = [SITE_TYPE_CONTRADICTION]

# Asked only where nothing independently confirms a disposal site exists. As written in WP-525
# that is OSM-only facilities: 6,848 of 19,492, all with no operational baseline at all.
# gpw_2021 (Global Plastic Watch, also Tier 4) meets the same no-corroboration test and would add
# 1,716 more; it is left out pending a decision on the ticket, and adding it is this one line.
CONTRADICTION_CHECK_SOURCES = {"osm_2022"}

# What counts as corroboration. `area_square_meters` is deliberately absent: for an OSM site it is
# measured from the same polygon that is in question, so it corroborates nothing. Once a pass has
# filled any of these, the site is no longer uncorroborated and is not asked again.
CORROBORATING_ATTRIBUTES = [
    "facility_status",
    "facility_type",
    "opening_year",
    "closing_year",
    "has_landfill_gas_collection",
    "annual_incoming_waste_metric_tonnes",
    "waste_in_place_metric_tonnes",
]

# Never signed off by the pipeline, whatever the tier. A Tier 1 regulator calling a site a quarry
# would otherwise auto-validate as an ordinary empty-baseline fill.
ALWAYS_REVIEW_ATTRIBUTES = {SITE_TYPE_CONTRADICTION}


# Computed locally from found coordinates. No source, no tier (Q12).
CALCULATED_ATTRIBUTES = [
    "distance_to_original_coordinates_km",
]

TARGET_ATTRIBUTES = [
    *IDENTITY_ATTRIBUTES, *CALCULATED_ATTRIBUTES, *GAP_FILL_ATTRIBUTES, *CONTRADICTION_CHECK_ATTRIBUTES,
]
REQUESTABLE_ATTRIBUTES = [*IDENTITY_ATTRIBUTES, *GAP_FILL_ATTRIBUTES, *CONTRADICTION_CHECK_ATTRIBUTES]

BOOLEAN_TARGET_ATTRIBUTES = {"has_landfill_gas_collection", "has_cover", "has_biocover"}
ARRAY_TARGET_ATTRIBUTES = {"cover_types", "gccs_energy_project_type", "gccs_current_project_status"}
# Stored as a numeric array in the spec, so multiple project rows can collapse into one record.
NUMERIC_ARRAY_TARGET_ATTRIBUTES = {"gccs_ch4_flow_to_project_metric_tonnes"}
# Spec requires a fraction between 0 and 1, never a percentage.
FRACTION_TARGET_ATTRIBUTES = {"gccs_collection_efficiency"}

# Gas collection and control system attributes. Only meaningful at a facility that has a gas
# collection system, so they are not requested where the baseline says there is none: asking
# about methane flaring at a site with no capture system spends prompt on a certain "nothing".
GCCS_ATTRIBUTES = {
    "gccs_ch4_flared_metric_tonnes",
    "gccs_ch4_generated_metric_tonnes",
    "gccs_ch4_collected_metric_tonnes",
    "gccs_ch4_flow_to_project_metric_tonnes",
    "gccs_energy_project_type",
    "gccs_current_project_status",
    "gccs_collection_efficiency",
}
INTEGER_TARGET_ATTRIBUTES = {"opening_year", "closing_year"}
NUMERIC_TARGET_ATTRIBUTES = {
    "area_square_meters",
    "opening_year",
    "closing_year",
    "found_latitude",
    "found_longitude",
    "annual_incoming_waste_metric_tonnes",
    "waste_in_place_metric_tonnes",
    "gccs_ch4_flared_metric_tonnes",
    "gccs_ch4_generated_metric_tonnes",
    "gccs_ch4_collected_metric_tonnes",
    "gccs_ch4_flow_to_project_metric_tonnes",
    "gccs_collection_efficiency",
}

# Ranges the spec states. Checked after conversion; out-of-range values are not promotable.
ATTRIBUTE_RANGES = {
    "area_square_meters": (0.0, None),
    "annual_incoming_waste_metric_tonnes": (0.0, None),
    "waste_in_place_metric_tonnes": (0.0, None),
    "gccs_ch4_flared_metric_tonnes": (0.0, None),
    "gccs_ch4_generated_metric_tonnes": (0.0, None),
    "gccs_ch4_collected_metric_tonnes": (0.0, None),
    "gccs_ch4_flow_to_project_metric_tonnes": (0.0, None),
    "gccs_collection_efficiency": (0.0, 1.0),
    "found_latitude": (-90.0, 90.0),
    "found_longitude": (-180.0, 180.0),
}

# Attribute -> standardized facility column it populates.
ATTRIBUTE_TO_STANDARD_COLUMN = {
    "found_site_name": "facility_name",
    "found_latitude": "latitude",
    "found_longitude": "longitude",
    "facility_status": "facility_status",
    "facility_type": "facility_type",
    "opening_year": "opening_year",
    "closing_year": "closing_year",
    "has_landfill_gas_collection": "has_landfill_gas_collection",
    "annual_incoming_waste_metric_tonnes": "annual_incoming_waste_metric_tonnes",
    "waste_in_place_metric_tonnes": "waste_in_place_metric_tonnes",
    "area_square_meters": "area_square_meters",
    "waste_depth": "waste_depth",
    "has_cover": "has_cover",
    "cover_types": "cover_types",
    "has_biocover": "has_biocover",
    "gccs_ch4_flared_metric_tonnes": "gccs_ch4_flared_metric_tonnes",
    "gccs_ch4_generated_metric_tonnes": "gccs_ch4_generated_metric_tonnes",
    "gccs_ch4_collected_metric_tonnes": "gccs_ch4_collected_metric_tonnes",
    "gccs_ch4_flow_to_project_metric_tonnes": "gccs_ch4_flow_to_project_metric_tonnes",
    "gccs_energy_project_type": "gccs_energy_project_type",
    "gccs_current_project_status": "gccs_current_project_status",
    "gccs_collection_efficiency": "gccs_collection_efficiency",
}


# --- categorical mapping onto the live database enums (Q20/Q26) --------------------------------
FACILITY_STATUS_MAP = {
    "active": "Active",
    "inactive": "Inactive",
    "closed": "Inactive",
    "temporarily closed": "Inactive",
    "planned": None,
    "under construction": None,
    "unknown": None,
}

FACILITY_TYPE_MAP = {
    "sanitary landfill": "Sanitary Landfill",
    "landfill": "Sanitary Landfill",
    "controlled dumpsite": "Controlled Dumpsite",
    "dumpsite": "Dumpsite",
    "dump": "Dumpsite",
    "open dump": "Dumpsite",
    "legacy landfill": "Dumpsite",
    "incineration facility": "Incineration Facility",
    "incinerator": "Incineration Facility",
    # The spec added 'Transfer Station' in upstream 5cfeadfa, but the database has NOT: both the
    # `facility_type` enum type and `chk_facility_type` still list four values, so a row carrying
    # it would be rejected on load. Kept as None until the database catches up, at which point
    # generate_enums.py will pick the value up and this line becomes
    # "transfer station": "Transfer Station".
    "transfer station": None,
    "unknown": None,
}

COVER_TYPE_MAP = {
    "clay cover": "clay cover",
    "clay": "clay cover",
    "organic cover": "organic cover",
    "organic": "organic cover",
    "biocover": "organic cover",
    "bio-cover": "organic cover",
    "sand cover": "sand cover",
    "sand": "sand cover",
    "other soil mixture": "other soil mixture",
    "soil": "other soil mixture",
    "soil cover": "other soil mixture",
    "unknown": None,
}

GCCS_ENERGY_PROJECT_TYPE_MAP = {
    "electricity generation": "Electricity Generation",
    "electricity": "Electricity Generation",
    "power generation": "Electricity Generation",
    "electricity generation project": "Electricity Generation",
    "direct use": "Direct Use",
    "direct-use": "Direct Use",
    "renewable natural gas": "Renewable Natural Gas",
    "rng": "Renewable Natural Gas",
    "pipeline injection": "Renewable Natural Gas",
    "other": "Other",
    "flaring": None,
    "flare": None,
    "none": None,
    "unknown": None,
}

GCCS_CURRENT_PROJECT_STATUS_MAP = {
    "operational": "Operational",
    "active": "Operational",
    "in operation": "Operational",
    "under construction": "Under Construction",
    "construction": "Under Construction",
    "planned": "Planned",
    "proposed": "Planned",
    "closed": "Closed",
    "shutdown": "Closed",
    "decommissioned": "Closed",
    "unknown": None,
}

# `waste_depth` is the one categorical the spec derives from a numeric source measurement rather
# than from a source category, so it has no mapping table and is not in ENUM_MAPS. Its values come
# from db_enums.py like every other vocabulary, but via the CHECK constraint on
# consolidated_facility rather than a Postgres enum type - there is no enum type for this column.
#
# The 5m boundary below is NOT in the database. The constraint states the two permitted strings;
# only the spec says how to produce one from a measurement in metres.
#
# The spec's boundary: at or below 5 metres is '<=5m', above it is '>5m'.
WASTE_DEPTH_BOUNDARY_METERS = 5.0

# Named explicitly rather than indexed out of WASTE_DEPTH_VALUES. That list is generated from the
# CHECK constraint in the order Postgres happens to render it, which carries no meaning: a
# harmless reordering of the constraint would silently swap shallow and deep on the next
# regeneration. The constraint decides which labels are *allowed*, not which is which.
WASTE_DEPTH_SHALLOW = "<=5m"
WASTE_DEPTH_DEEP = ">5m"

assert {WASTE_DEPTH_SHALLOW, WASTE_DEPTH_DEEP} <= set(WASTE_DEPTH_VALUES), (
    f"Depth labels {WASTE_DEPTH_SHALLOW!r}/{WASTE_DEPTH_DEEP!r} are not both permitted by the "
    f"database, which allows {WASTE_DEPTH_VALUES}. Regenerate db_enums.py, then reconcile."
)


def bucket_waste_depth(meters: Any) -> tuple[str | None, str]:
    """Convert a depth already normalized to metres into the spec's depth category.

    Returns (None, note) rather than a category when the measurement cannot support one. The spec
    is explicit that a reported depth of zero or less is not a measurement, so it is left NULL
    instead of being assigned to '<=5m' - a zero would otherwise read as a genuine shallow site.
    """
    text = normalize_scalar(meters)
    if not text:
        return None, ""

    # A converted range ("12 to 18") keeps both ends; bucket on the shallower one only when both
    # ends agree, since a range straddling the boundary does not determine a category.
    try:
        parsed = [float(part.strip()) for part in text.split(" to ")]
    except ValueError:
        return None, f"waste_depth value {text!r} is not numeric; stored as NULL."

    if any(value <= 0 for value in parsed):
        return None, (
            f"waste_depth of {text} metres is not a measurement (zero or negative); stored as NULL."
        )

    buckets = {
        WASTE_DEPTH_SHALLOW if value <= WASTE_DEPTH_BOUNDARY_METERS else WASTE_DEPTH_DEEP
        for value in parsed
    }
    if len(buckets) > 1:
        return None, (
            f"waste_depth range {text} metres straddles the {WASTE_DEPTH_BOUNDARY_METERS:g}m "
            "boundary, so it does not determine a category; stored as NULL."
        )

    category = buckets.pop()
    return category, f"Binned {text} metres to {category!r}."


SITE_TYPE_CONTRADICTION_MAP = {
    "contradicted": SITE_TYPE_CONTRADICTED,
    # The agent is told to omit the attribute when nothing contradicts the site. If it answers
    # anyway, that answer carries no evidence of anything, so it maps to NULL and is dropped.
    "no contradiction found": None,
    "no contradiction": None,
    "not contradicted": None,
    "none": None,
    "no": None,
    "unknown": None,
}

ENUM_MAPS = {
    "facility_status": (FACILITY_STATUS_MAP, FACILITY_STATUS_VALUES),
    SITE_TYPE_CONTRADICTION: (SITE_TYPE_CONTRADICTION_MAP, [SITE_TYPE_CONTRADICTED]),
    "facility_type": (FACILITY_TYPE_MAP, FACILITY_TYPE_VALUES),
    "cover_types": (COVER_TYPE_MAP, COVER_TYPE_VALUES),
    "gccs_energy_project_type": (GCCS_ENERGY_PROJECT_TYPE_MAP, GCCS_ENERGY_PROJECT_TYPE_VALUES),
    "gccs_current_project_status": (GCCS_CURRENT_PROJECT_STATUS_MAP, GCCS_CURRENT_PROJECT_STATUS_VALUES),
}

BOOLEAN_INPUT_MAP = {
    "yes": True, "y": True, "true": True, "t": True, "1": True,
    "no": False, "n": False, "false": False, "f": False, "0": False,
    "unknown": None, "": None,
}


# --- output levels (Q5) ------------------------------------------------------------------------
EVIDENCE_HEADERS = [
    "evidence_id", "run_id", "dataset_version", "site_id", "internal_facility_id",
    "attribute_name", "claimed_value", "claimed_unit", "normalized_value", "normalized_unit",
    "unit_conversion_note", "mapped_value", "mapping_note", "value_basis", "value_date",
    "agent_confidence", "source_id", "source_tier", "agent_proposed_tier", "tier_rule_applied",
    "evidence_summary", "quoted_evidence_short", "search_terms_used", "promotion_eligible",
    "exclusion_reason", "record_created_date",
]

RESOLVED_HEADERS = [
    "site_id", "internal_facility_id", "site_name", "country_iso3", "attribute_name",
    "baseline_value", "baseline_source", "baseline_tier",
    "resolved_value", "resolved_unit", "resolution", "resolution_rule",
    "winning_evidence_id", "winning_source_tier", "winning_source_url", "best_tier_available",
    "value_date", "confidence_score", "validation_status", "reviewer", "reviewed_date",
    "researcher_notes",
]

# Who is recorded as having signed off a row the pipeline decided without a human.
AUTO_REVIEWER = "AI Agent"

SOURCES_HEADERS = [
    "source_id", "url_normalized", "url", "source_title", "publisher", "source_type",
    "source_tier", "tier_rule_applied", "agent_proposed_tier", "publication_date",
    "has_publication_date", "language", "paywall_flag", "subscription_followup_needed",
    "first_seen_run_id", "times_cited", "cited_site_count",
]

# The filtered queue an SME actually works through (Q31).
REVIEW_QUEUE_HEADERS = [
    "site_id", "site_name", "country_iso3", "attribute_name", "resolution",
    "baseline_value", "baseline_source", "baseline_tier", "resolved_value", "resolved_unit",
    "winning_source_tier", "winning_source_url", "value_date", "agreeing_source_count",
    "evidence_summary", "validation_status", "reviewer", "reviewed_date", "researcher_notes",
]

SUPPLEMENTARY_LEADS_HEADERS = [
    "run_id", "site_id", "site_name", "country_iso3", "attribute_name", "lead_value",
    "source_tier", "lead_summary", "url", "exclusion_reason",
]

FOUNDRY_RUN_LOG_HEADERS = [
    "run_id", "site_id", "site_name", "country_iso3", "requested_attributes", "agent_id",
    "request_started_at", "request_finished_at", "status", "raw_response_path",
    "parsed_attribute_count", "parsed_source_count", "error_message", "retry_count",
]

PARSE_WARNING_HEADERS = ["run_id", "site_id", "site_name", "warning"]

# A read-only lens on the Review_Queue rows that carry a Contradicted verdict. The decision is
# still recorded on the Review_Queue row; this view exists so the verdicts can be read together,
# beside what the same run found about closure.
CONTRADICTION_HEADERS = [
    "site_id", "site_name", "country_iso3", "verdict", "winning_source_tier",
    "winning_source_url", "evidence_summary", "quoted_evidence_short", "closure_also_reported",
    "validation_status",
]


# --- standardized facility table (Q19) --------------------------------------------------------
AI_SEARCH_DATA_SOURCE = "ai_search_2026"

# Column order follows StandardizedFacilityTableSpecification.md, which as of upstream 8c0bb3fe
# defines `has_biocover` beside the other cover columns - where this table had already placed it.
# Order still matters: the generated load statement names every column, but a positional COPY
# against a table whose DDL disagrees would misalign everything after the first mismatch.
STANDARDIZED_FACILITY_COLUMNS = [
    "facility_id", "year", "facility_name", "iso3c_plus", "area_square_meters", "facility_status",
    "facility_type", "opening_year", "closing_year", "has_landfill_gas_collection",
    "waste_depth", "annual_incoming_waste_metric_tonnes", "waste_in_place_metric_tonnes",
    "has_cover", "cover_types", "has_biocover",
    "gccs_ch4_flared_metric_tonnes", "gccs_ch4_generated_metric_tonnes",
    "gccs_ch4_collected_metric_tonnes", "gccs_energy_project_type", "gccs_current_project_status",
    "gccs_ch4_flow_to_project_metric_tonnes", "gccs_ch4_percent", "gccs_collection_efficiency",
    "oxidation", "mcf", "latitude", "longitude", "point", "is_location_exact",
    "ch4_emissions_metric_tonnes", "food_percent_by_weight", "green_percent_by_weight",
    "wood_percent_by_weight", "paper_cardboard_percent_by_weight", "textiles_percent_by_weight",
    "plastic_percent_by_weight", "metal_percent_by_weight", "glass_percent_by_weight",
    "rubber_percent_by_weight", "other_percent_by_weight", "data_source", "created_date", "git_sha",
]


# --- resolution + review vocabularies ----------------------------------------------------------
RESOLUTION_VALUES = [
    "Confirmed baseline",
    "Filled empty baseline",
    "Overrides baseline",
    "Conflict - needs review",
    "Conflict - lower credibility",
    "Calculated",
    "Not found",
]

# Resolutions whose rows belong in the leads sheet and must NOT appear in the review queue.
LEAD_ROUTED_RESOLUTIONS = {"Conflict - lower credibility"}

DEFINITION_VALUES = {
    "attribute_name": REQUESTABLE_ATTRIBUTES,
    "facility_status": FACILITY_STATUS_VALUES,
    "facility_type": FACILITY_TYPE_VALUES,
    "cover_type": COVER_TYPE_VALUES,
    "waste_depth": WASTE_DEPTH_VALUES,
    SITE_TYPE_CONTRADICTION: SITE_TYPE_CONTRADICTION_VALUES,
    "gccs_energy_project_type": GCCS_ENERGY_PROJECT_TYPE_VALUES,
    "gccs_current_project_status": GCCS_CURRENT_PROJECT_STATUS_VALUES,
    "boolean_unknown": ["Yes", "No", "Unknown"],
    "value_basis": ["Direct", "Inferred", "Conflicting", "Not found"],
    "confidence_score": ["High", "Medium", "Low", "Excluded candidate"],
    "resolution": RESOLUTION_VALUES,
    "source_tier": ["Tier 1", "Tier 2", "Tier 3", "Tier 4", "Tier 5"],
    "validation_status": ["Needs review", "Validated", "Rejected", "Auto-validated", "Routed to leads"],
    # What a reviewer may choose. The other statuses are set by the pipeline, not by hand.
    "review_decision": ["Validated", "Rejected"],
}

# Columns a reviewer may edit, and the vocabulary each offers. Anything absent here is
# read-only in the workbook, so a dropdown never invites editing a pipeline-set field.
FIELD_TO_DEFINITION = {
    "validation_status": "review_decision",
}

FOUNDRY_REQUIRED_ATTRIBUTE_FIELDS = [
    "attribute_name",
    "value",
    "value_basis",
    "confidence_score",
    "sources",
]


# --- scalar helpers ----------------------------------------------------------------------------
def normalize_scalar(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, datetime):
        return value.replace(microsecond=0).isoformat()
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, bool):
        return "TRUE" if value else "FALSE"
    if isinstance(value, (list, tuple)):
        return "; ".join(normalize_scalar(item) for item in value if item is not None)
    return str(value).strip()



def parse_tristate_bool(value: Any) -> tuple[bool | None, str]:
    """Yes/No/Unknown -> True/False/None. Unknown becomes NULL, per the boolean spec column."""
    if isinstance(value, bool):
        return value, ""
    text = normalize_scalar(value).lower()
    if text in BOOLEAN_INPUT_MAP:
        return BOOLEAN_INPUT_MAP[text], ""
    return None, f"Unrecognized boolean value {normalize_scalar(value)!r}; stored as NULL."


def is_blank(value: Any) -> bool:
    return normalize_scalar(value) == ""


def normalize_for_compare(value: Any) -> str:
    return normalize_scalar(value).lower().replace(",", "").strip()


def map_enum_value(field_name: str, value: Any) -> tuple[Any, str]:
    """Map a search-vocabulary categorical onto the live database enum. Unmappable -> NULL."""
    if field_name not in ENUM_MAPS:
        return normalize_scalar(value), ""
    mapping, allowed = ENUM_MAPS[field_name]

    if field_name in ARRAY_TARGET_ATTRIBUTES:
        raw_items = value if isinstance(value, (list, tuple)) else re.split(r"[;,]", normalize_scalar(value))
        mapped: list[str] = []
        notes: list[str] = []
        for item in raw_items:
            text = normalize_scalar(item).lower().strip()
            if not text:
                continue
            if text in mapping and mapping[text] is not None:
                if mapping[text] not in mapped:
                    mapped.append(mapping[text])
            elif text in allowed:
                if text not in mapped:
                    mapped.append(text)
            else:
                notes.append(f"dropped unmappable {field_name} value {text!r}")
        return mapped, "; ".join(notes)

    text = normalize_scalar(value).lower().strip()
    if not text:
        return None, ""
    if text in mapping:
        mapped_value = mapping[text]
        if mapped_value is None:
            return None, f"{field_name} value {normalize_scalar(value)!r} has no database enum; stored as NULL."
        note = "" if normalize_scalar(value) == mapped_value else f"mapped {normalize_scalar(value)!r} -> {mapped_value!r}"
        return mapped_value, note
    for candidate in allowed:
        if candidate.lower() == text:
            return candidate, f"normalized case {normalize_scalar(value)!r} -> {candidate!r}"
    return None, f"{field_name} value {normalize_scalar(value)!r} is not a valid enum member; stored as NULL."



def validate_foundry_payload(payload: dict[str, Any]) -> list[str]:
    warnings: list[str] = []
    attributes = payload.get("attributes", [])
    if not isinstance(attributes, list):
        return ["Payload field 'attributes' must be a list."]

    for idx, attribute in enumerate(attributes, start=1):
        if not isinstance(attribute, dict):
            warnings.append(f"attributes[{idx}] must be an object.")
            continue
        for field in FOUNDRY_REQUIRED_ATTRIBUTE_FIELDS:
            if field not in attribute:
                warnings.append(f"attributes[{idx}] missing required field {field!r}.")
        name = normalize_scalar(attribute.get("attribute_name"))
        if name and name not in REQUESTABLE_ATTRIBUTES:
            warnings.append(f"attributes[{idx}] has unsupported attribute_name {name!r}.")
        sources = attribute.get("sources", [])
        if not isinstance(sources, list):
            warnings.append(f"attributes[{idx}] sources must be a list.")
        elif not any(isinstance(source, dict) and source.get("url") for source in sources):
            warnings.append(f"attributes[{idx}] has no clickable source URL.")
    return warnings
