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
    "bulk_waste_type",
    # WP-531. Review layer only for now: the upstream standardized spec has no operator column, so
    # it is deliberately absent from ATTRIBUTE_TO_STANDARD_COLUMN and never reaches the
    # standardized table. consolidated_facility has no operator either, so the baseline is always
    # empty and it is asked of every searched facility. Promotion is a follow-up once upstream adds
    # the column - one entry in ATTRIBUTE_TO_STANDARD_COLUMN and STANDARDIZED_FACILITY_COLUMNS.
    "operator",
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
    "has_flare",
    "flare_efficiency",
    "gccs_ch4_generated_metric_tonnes",
    "gccs_ch4_collected_metric_tonnes",
    "gccs_ch4_flow_to_project_metric_tonnes",
    "gccs_energy_project_type",
    "gccs_current_project_status",
    "gccs_collection_efficiency",
]

# Kept above so the seed, older cached responses and re-arbitrated runs still read them, but no
# longer asked of the agent (F31).
UNSEARCHED_ATTRIBUTES = {
    "gccs_ch4_generated_metric_tonnes",
    "gccs_ch4_collected_metric_tonnes",
    "gccs_ch4_flow_to_project_metric_tonnes",
}

# A location is one fact, valued "lat, lon", so it is reviewed and loaded as a pair, from one
# source, or not at all. It is read from where each source states the facility is (locate.py),
# not from coordinates the agent returns as values.
LOCATION_ATTRIBUTE = "found_location"

# The two coordinates a location is made of. The seed and refresh-seed record a location's
# provenance under these names, and responses cached before build step 4 of the source-matching
# plan returned them as values.
COORDINATE_ATTRIBUTES = ["found_latitude", "found_longitude"]

# Always requested, except where a Tier 1-2 source already supplied them. They are how we know
# the agent found the *right* facility, so they are not gap-filled: the identity check rests on
# them (Q34). The name is a value; the location comes from what each source states.
IDENTITY_ATTRIBUTES = [
    "found_facility_name",
    LOCATION_ATTRIBUTE,
]

# Attribute names retired in favour of a new spelling, mapped to their replacement. Data written
# before a rename - cached agent responses, pinned seed snapshots, and the `attribute@dataset`
# provenance in a seed - still carries the old name, so everything that reads an attribute name
# from such data goes through canonical_attribute(). Without it, re-arbitrating an older run would
# drop the attribute as unsupported, silently.
ATTRIBUTE_ALIASES = {
    # WP-531: aligned with the upstream `facility_name` column.
    "found_site_name": "found_facility_name",
    # b5033f3: renamed with the move to the standardized facility schema. waste_depth_meters was
    # a depth in metres, which waste_depth still reads before binning it.
    "area": "area_square_meters",
    "waste_depth_meters": "waste_depth",
}


def canonical_attribute(name: Any) -> str:
    """The current spelling of an attribute name, accepting retired ones."""
    text = str(name).strip() if name is not None else ""
    return ATTRIBUTE_ALIASES.get(text, text)


# --- country policy ------------------------------------------------------------------------
# US facilities are already well covered by usa_ghgrp_2026 and lmop_2024, both Tier 1, so an AI
# search there mostly produces equal-tier conflicts for a human to adjudicate rather than new
# data. Brazil's searchable sites are SINIR facilities whose coordinates are a municipality centre;
# on the WP-543 test web search returned no usable coordinates for any of 20, so searching all
# 3,913 is spend with no return. Excluded by default; --include-excluded-countries, naming the
# country with --iso3, or naming sites with --site-ids overrides it.
DEFAULT_EXCLUSION_REASONS = {
    "USA": "already Tier 1 covered",
    "BRA": "web search found no coordinates for SINIR's municipality-centre sites in WP-543",
}
DEFAULT_EXCLUDED_ISO3 = set(DEFAULT_EXCLUSION_REASONS)

# Countries where the ONLY thing worth searching is the coordinates, and only for facilities whose
# recorded location is flagged inexact. Every other attribute for these facilities comes from a
# government source, so searching it risks overwriting better data than the search can find.
# Today that is Brazil's 3,913 SINIR facilities, whose coordinates are a municipality centre.
COORDINATES_ONLY_ISO3 = {"BRA"}

# The full run in six batches, run in order (F30). Batches 1-2 are English-speaking, so SMEs learn
# the review process on familiar sources: English as the main language, then as an official one.
# Batches 3-5 follow the most blank core fields (searchable sites x share of status, type, opening
# year, gas collection, incoming waste, waste in place and area left blank), measured on the
# 3 Oct 2026 seed at about 2,000-2,300 sites each. The last batch is every country not listed, so
# a country new to the database is never left out. Default exclusions still apply in every batch.
COUNTRY_BATCHES = {
    1: ["CAN", "GBR", "AUS", "NZL", "IRL"],
    2: [
        "IND", "PHL", "NGA", "ZAF", "GHA", "KEN", "UGA", "TZA", "ZMB", "ZWE", "BWA", "NAM", "LSO",
        "SGP", "MLT", "TTO", "GUY", "BLZ", "FJI", "PNG", "MUS", "SLE", "GMB", "PAK", "SSD", "JAM",
        "LBR", "MWI", "RWA",
    ],
    3: ["MEX"],
    4: ["DEU", "RUS", "TUR"],
    5: ["CHN", "FRA", "ITA", "POL", "ESP", "IDN"],
}
LAST_BATCH = max(COUNTRY_BATCHES) + 1


def in_batch(iso3: str, batch: int) -> bool:
    """Whether a country belongs to a batch; the last batch takes every country not listed."""
    code = iso3.strip().upper()
    if batch == LAST_BATCH:
        return not any(code in countries for countries in COUNTRY_BATCHES.values())
    return code in COUNTRY_BATCHES.get(batch, [])


# --- not a waste facility (WP-525) -------------------------------------------------------------
# For facilities nothing independently confirms, facility_type may also come back as
# "Not a Waste Facility": a source positively says the site is, and always was, something else -
# a quarry, a mine, a yard that never took waste. It is a contradiction, not a verification:
# finding nothing leaves facility_type empty, never "Not a Waste Facility".
#
# It is handled like any other facility_type value: it goes to review, as every found value that
# differs from ours does, and it reaches the next pass's seed.
#
# PENDING UPSTREAM: the database's `facility_type` enum and chk_facility_type do not carry this
# value yet, so consolidating a validated one will be rejected until it is added upstream.
NOT_A_WASTE_FACILITY = "Not a Waste Facility"
# A site where recyclables are dropped off, sorted or processed and nothing is disposed of. Like
# "Not a Waste Facility" it is PENDING UPSTREAM: requested for the database enum and spec, and
# offered here so recycling centres stop being forced into a landfill or dumpsite type.
RECYCLING_CENTER = "Recycling Center"
# A site where collected waste is consolidated for transport and nothing is disposed of. In the spec
# and in chk_facility_type on every transformed.* table, but NOT yet in the `facility_type` enum type
# or consolidation.consolidated_facility's chk_facility_type, so it is pending too.
TRANSFER_STATION = "Transfer Station"
PENDING_UPSTREAM_FACILITY_TYPES = [TRANSFER_STATION, RECYCLING_CENTER, NOT_A_WASTE_FACILITY]
FACILITY_TYPE_ALLOWED = [*FACILITY_TYPE_VALUES, *PENDING_UPSTREAM_FACILITY_TYPES]
# What every prompt offers. "Not a Waste Facility" is offered only to unconfirmed sites (WP-525).
FACILITY_TYPE_OFFERED = [value for value in FACILITY_TYPE_ALLOWED if value != NOT_A_WASTE_FACILITY]

# Asked only where nothing independently confirms a disposal site exists: facilities whose every
# contributing source is one of these Tier 4 datasets. OSM is a crowd-mapped polygon and Global
# Plastic Watch a satellite detection, and neither says anything about what the site is used for.
# Together 8,564 of 19,492 facilities, none with an operational baseline.
CONTRADICTION_CHECK_SOURCES = {"osm_2022", "gpw_2021"}

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


# Computed locally from found coordinates. No source, no tier (Q12).
CALCULATED_ATTRIBUTES = [
    "distance_to_original_coordinates_km",
]

TARGET_ATTRIBUTES = [*IDENTITY_ATTRIBUTES, *CALCULATED_ATTRIBUTES, *GAP_FILL_ATTRIBUTES]
REQUESTABLE_ATTRIBUTES = [*IDENTITY_ATTRIBUTES, *GAP_FILL_ATTRIBUTES]
# What a source reports as a value. A location is not one: each source states where the facility
# is, and arbitration reads the found location from those (locate.py).
VALUE_ATTRIBUTES = [
    name for name in REQUESTABLE_ATTRIBUTES if name not in COORDINATE_ATTRIBUTES and name != LOCATION_ATTRIBUTE
]

BOOLEAN_TARGET_ATTRIBUTES = {"has_landfill_gas_collection", "has_cover", "has_biocover", "has_flare"}
ARRAY_TARGET_ATTRIBUTES = {"cover_types", "gccs_energy_project_type", "gccs_current_project_status"}
# Stored as a numeric array in the spec, so multiple project rows can collapse into one record.
NUMERIC_ARRAY_TARGET_ATTRIBUTES = {"gccs_ch4_flow_to_project_metric_tonnes"}
# Spec requires a fraction between 0 and 1, never a percentage.
FRACTION_TARGET_ATTRIBUTES = {"gccs_collection_efficiency", "flare_efficiency"}

# Gas collection and control system attributes. Only meaningful at a facility that has a gas
# collection system, so they are not requested where the baseline says there is none: asking
# about methane flaring at a site with no capture system spends prompt on a certain "nothing".
# A flare burns collected gas, so the flare attributes are gated the same way (F31).
GCCS_ATTRIBUTES = {
    "gccs_ch4_flared_metric_tonnes",
    "has_flare",
    "flare_efficiency",
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
    "flare_efficiency",
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
    "flare_efficiency": (0.0, 1.0),
    "found_latitude": (-90.0, 90.0),
    "found_longitude": (-180.0, 180.0),
}

# Attribute -> standardized facility column it populates.
ATTRIBUTE_TO_STANDARD_COLUMN = {
    "found_facility_name": "facility_name",
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
    "transfer station": TRANSFER_STATION,
    "waste transfer station": TRANSFER_STATION,
    "recycling center": RECYCLING_CENTER,
    "recycling centre": RECYCLING_CENTER,
    "recycling depot": RECYCLING_CENTER,
    "materials recovery facility": RECYCLING_CENTER,
    "not a waste facility": NOT_A_WASTE_FACILITY,
    "not a waste site": NOT_A_WASTE_FACILITY,
    "not waste": NOT_A_WASTE_FACILITY,
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

# F31. No database enum or standardized column exists yet, so these values are this project's own;
# like `operator`, bulk_waste_type reaches review but has no standardized column.
BULK_WASTE_TYPE_VALUES = ["municipal solid waste", "inert waste", "others"]
# The distinction that matters is municipal solid waste, which decomposes and generates methane,
# against inert waste, which does not; the prompt defines each and classifies by the bulk received.
BULK_WASTE_TYPE_MAP = {
    "msw": "municipal solid waste",
    "municipal waste": "municipal solid waste",
    "household waste": "municipal solid waste",
    "domestic waste": "municipal solid waste",
    "commercial waste": "municipal solid waste",
    "inert": "inert waste",
    "construction and demolition waste": "inert waste",
    "c&d waste": "inert waste",
    "demolition waste": "inert waste",
    "rubble": "inert waste",
    "other": "others",
    # Not knowing is not a finding: stored NULL, so the site is asked again next pass.
    "unknown": None,
    "mixed": None,
    "industrial": "others",
    "industrial waste": "others",
    "hazardous": "others",
    "hazardous waste": "others",
    # The prompt's own examples of `others`, and common spellings of the other two.
    "mining waste": "others",
    "mine waste": "others",
    "sewage sludge": "others",
    "sludge": "others",
    "municipal solid waste (msw)": "municipal solid waste",
    "c&d": "inert waste",
    "construction waste": "inert waste",
    "construction and demolition debris": "inert waste",
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


ENUM_MAPS = {
    "facility_status": (FACILITY_STATUS_MAP, FACILITY_STATUS_VALUES),
    "facility_type": (FACILITY_TYPE_MAP, FACILITY_TYPE_ALLOWED),
    "cover_types": (COVER_TYPE_MAP, COVER_TYPE_VALUES),
    "gccs_energy_project_type": (GCCS_ENERGY_PROJECT_TYPE_MAP, GCCS_ENERGY_PROJECT_TYPE_VALUES),
    "gccs_current_project_status": (GCCS_CURRENT_PROJECT_STATUS_MAP, GCCS_CURRENT_PROJECT_STATUS_VALUES),
    "bulk_waste_type": (BULK_WASTE_TYPE_MAP, BULK_WASTE_TYPE_VALUES),
}

BOOLEAN_INPUT_MAP = {
    "yes": True, "y": True, "true": True, "t": True, "1": True,
    "no": False, "n": False, "false": False, "f": False, "0": False,
    "unknown": None, "": None,
}


# --- output levels (Q5) ------------------------------------------------------------------------
# WP-531: every SME-facing output carries the translated site_name beside the source's own
# spelling and its language, so a reviewer can check results against the original name.
SITE_NAME_COLUMNS = ["site_name", "original_site_name", "source_language"]

SOURCE_FACILITY_COLUMNS = [
    "facility_name_in_source", "facility_name_english", "stated_location", "stated_location_kind",
    "location_basis", "located_latitude", "located_longitude", "location_reason",
]

EVIDENCE_HEADERS = [
    "evidence_id", "run_id", "dataset_version", "site_id", *SITE_NAME_COLUMNS, "internal_facility_id",
    "attribute_name", "claimed_value", "claimed_unit", "normalized_value", "normalized_unit",
    "unit_conversion_note", "mapped_value", "mapping_note", "value_basis", "value_date",
    "agent_confidence", "source_id", "link_status", "source_tier", "agent_proposed_tier", "tier_rule_applied",
    "evidence_summary", "quoted_evidence_short", "search_terms_used", "promotion_eligible",
    "exclusion_reason", "record_created_date",
    # The facility each source describes, as it writes it, and the point read from its location
    # (locate.py): every row of a source carries them. location_reason says why a stated location
    # is not a usable point.
    *SOURCE_FACILITY_COLUMNS,
]

# resolved.csv is written with this header and no rows. Nothing is accepted automatically, so every
# value goes to review, and ingestion reads the SMEs' decisions from the review workbook. The file
# stays because ingestion's loader expects it, and runs from before still carry rows.
RESOLVED_HEADERS = [
    "site_id", "internal_facility_id", *SITE_NAME_COLUMNS, "country_iso3", "attribute_name",
    "baseline_value", "baseline_source", "baseline_tier",
    "resolved_value", "resolved_unit", "resolution", "resolution_rule",
    "winning_evidence_id", "winning_source_tier", "winning_source_url", "winning_link_status",
    "best_tier_available",
    "value_date", "confidence_score", "validation_status", "reviewer", "reviewed_date",
    "researcher_notes",
]

# The validation_status of a row that leaves our database as it is: a source confirmed our value,
# or nothing was found. It is neither queued nor loaded. A found value that differs from ours is
# "Needs review", and one weaker than ours "Routed to leads": nothing is accepted automatically.
NOTHING_TO_REVIEW = "Nothing to review"

SOURCES_HEADERS = [
    "source_id", "url_normalized", "url", "source_title", "publisher", "source_type",
    "source_tier", "tier_rule_applied", "agent_proposed_tier", "publication_date",
    "has_publication_date", "language", "paywall_flag", "subscription_followup_needed",
    "first_seen_run_id", "times_cited", "cited_site_count",
]

# The filtered queue an SME actually works through (Q31).
REVIEW_QUEUE_HEADERS = [
    # translation_note sits beside the names it is about: the SME compares the two spellings and
    # records a translation problem there, separately from researcher_notes.
    # baseline_coordinates is "lat, lon" as Google Maps accepts it pasted into its search box.
    "site_id", *SITE_NAME_COLUMNS, "translation_note", "country_iso3", "baseline_coordinates",
    "attribute_name", "resolution",
    "baseline_value", "baseline_source", "baseline_tier", "resolved_value", "resolved_unit",
    "winning_source_tier", "winning_source_url", "winning_link_status", "value_date",
    "agreeing_source_count",
    # corrected_value is the right value when the found one is wrong. resolved_value is never
    # edited, so the AI's own answer survives for measuring its error rate. corrected_value_date
    # is the date a correction applies to, where that isn't the source's value_date - a current
    # figure replacing one rejected as outdated, say.
    "evidence_summary", "validation_status", "rejection_reason", "corrected_value",
    "corrected_value_date", "reviewer", "reviewed_date",
    "researcher_notes",
]

SUPPLEMENTARY_LEADS_HEADERS = [
    "run_id", "site_id", *SITE_NAME_COLUMNS, "country_iso3", "attribute_name", "lead_value",
    "source_tier", "lead_summary", "url", "exclusion_reason",
]

FOUNDRY_RUN_LOG_HEADERS = [
    "run_id", "site_id", "site_name", "country_iso3", "requested_attributes", "agent_id",
    "request_started_at", "request_finished_at", "status", "raw_response_path",
    "parsed_attribute_count", "parsed_source_count", "error_message", "retry_count",
    "web_searches",
]

PARSE_WARNING_HEADERS = ["run_id", "site_id", "site_name", "warning"]

# --- standardized facility table (Q19) --------------------------------------------------------
AI_SEARCH_DATA_SOURCE = "ai_search_2026"

# The columns of the standardized facility table, in the order StandardizedFacilityTableSpecification.md
# defines them (as of upstream 8c0bb3fe, `has_biocover` sits beside the other cover columns). This
# repository no longer writes the table: ingestion builds it from reviewed values. The list stays so
# the attribute bindings above can be checked against the vendored spec and the database.
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
    "facility_type": FACILITY_TYPE_ALLOWED,
    "cover_type": COVER_TYPE_VALUES,
    "waste_depth": WASTE_DEPTH_VALUES,
    "gccs_energy_project_type": GCCS_ENERGY_PROJECT_TYPE_VALUES,
    "gccs_current_project_status": GCCS_CURRENT_PROJECT_STATUS_VALUES,
    "bulk_waste_type": BULK_WASTE_TYPE_VALUES,
    "boolean_unknown": ["Yes", "No", "Unknown"],
    "value_basis": ["Direct", "Inferred", "Conflicting", "Not found"],
    "confidence_score": ["High", "Medium", "Low", "Excluded candidate"],
    "resolution": RESOLUTION_VALUES,
    "source_tier": ["Tier 1", "Tier 2", "Tier 3", "Tier 4", "Tier 5"],
    "validation_status": ["Needs review", "Validated", "Rejected", NOTHING_TO_REVIEW, "Routed to leads"],
    # What a reviewer may choose. The other statuses are set by the pipeline, not by hand.
    "review_decision": ["Validated", "Rejected"],
    # Why a reviewer rejected a value. A fixed list, so rejections can be counted by cause to find
    # where the AI goes wrong; detail goes in researcher_notes.
    "rejection_reason": ["Wrong facility", "Not in source", "Wrong value", "Outdated", "Other"],
    # Booleans as resolved_value shows them, so a correction reads like the value it replaces.
    "true_false": ["TRUE", "FALSE"],
}

# What corrected_value offers, per attribute under review: (Definitions column, strict). A strict
# list accepts nothing else; a multi-value field suggests single values but still takes a typed
# combination ("clay cover; sand cover"). Numbers, years and names stay free text.
CORRECTED_VALUE_CHOICES = {
    "facility_status": ("facility_status", True),
    "facility_type": ("facility_type", True),
    "bulk_waste_type": ("bulk_waste_type", True),
    "waste_depth": ("waste_depth", True),
    "has_landfill_gas_collection": ("true_false", True),
    "has_cover": ("true_false", True),
    "has_biocover": ("true_false", True),
    "has_flare": ("true_false", True),
    "cover_types": ("cover_type", False),
    "gccs_energy_project_type": ("gccs_energy_project_type", False),
    "gccs_current_project_status": ("gccs_current_project_status", False),
}

# Columns a reviewer may edit, and the vocabulary each offers. Anything absent here is
# read-only in the workbook, so a dropdown never invites editing a pipeline-set field.
FIELD_TO_DEFINITION = {
    "validation_status": "review_decision",
    "rejection_reason": "rejection_reason",
}

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
            if candidate == normalize_scalar(value):
                return candidate, ""
            return candidate, f"normalized case {normalize_scalar(value)!r} -> {candidate!r}"
    return None, f"{field_name} value {normalize_scalar(value)!r} is not a valid enum member; stored as NULL."



def validate_foundry_payload(payload: dict[str, Any]) -> list[str]:
    """What is malformed in a cached response, read through the one reader (payload.py).

    A response from before the source-first contract is reported in its own terms, attributes[i],
    as it always was; a source-first one as sources[i].values[j].
    """
    from .locate import LOCATION_KINDS, PART_SEPARATOR
    from .payload import REQUIRED_VALUE_FIELDS, is_converted, read_payload

    read = read_payload(payload)
    warnings = list(read["problems"])
    for index, source in enumerate(read["sources"], start=1):
        if is_converted(source):
            continue
        if not normalize_scalar(source.get("url")):
            warnings.append(f"sources[{index}] has no clickable source URL.")
        kinds = [part.strip().lower() for part in normalize_scalar(source.get("stated_location_kind")).split(PART_SEPARATOR)]
        unknown = [kind for kind in kinds if kind and kind not in LOCATION_KINDS]
        if unknown:
            warnings.append(f"sources[{index}] has unrecognised stated_location_kind {', '.join(unknown)!r}.")
        for value_index, value in enumerate(source["values"], start=1):
            for field in REQUIRED_VALUE_FIELDS:
                if field not in value:
                    warnings.append(f"sources[{index}].values[{value_index}] missing required field {field!r}.")
            # Same canonicalisation as extract_evidence, so a pre-rename cached response is not
            # reported as unsupported by one and accepted by the other.
            name = canonical_attribute(normalize_scalar(value.get("attribute_name")))
            if name and name not in VALUE_ATTRIBUTES:
                warnings.append(f"sources[{index}].values[{value_index}] has unsupported attribute_name {name!r}.")
    return list(dict.fromkeys(warnings))
