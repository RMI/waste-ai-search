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

# Attributes that hold a value for each year, as consolidation keeps them: its
# TIME_VARYING_ATTRIBUTE_COLUMNS, in RMI/waste_data_ingestion_pipeline's
# facility_etl/consolidation/consolidation_techniques.py, less the columns this repository never
# reads (gccs_lfg_collected_cubic_meters, mcf, ch4_emissions_metric_tonnes). Keep the two aligned.
# One value is loaded for each year a source reports one of these for (build step 5 of the
# source-matching plan); every other attribute has one value per site.
TIME_VARYING_ATTRIBUTES = [
    "facility_status",
    "waste_depth",
    "annual_incoming_waste_metric_tonnes",
    "waste_in_place_metric_tonnes",
    "cover_types",
    "gccs_ch4_flared_metric_tonnes",
    "gccs_ch4_generated_metric_tonnes",
    "gccs_ch4_collected_metric_tonnes",
    "gccs_energy_project_type",
    "gccs_current_project_status",
    "gccs_ch4_flow_to_project_metric_tonnes",
]

# Kept above so the seed, older cached responses and re-arbitrated runs still read them, but no
# longer asked of the agent (F31).
UNSEARCHED_ATTRIBUTES = {
    "gccs_ch4_generated_metric_tonnes",
    "gccs_ch4_collected_metric_tonnes",
    "gccs_ch4_flow_to_project_metric_tonnes",
}

# A location is one fact, valued "lat, lon". Arbitration once read a found location from where
# each source states the facility is; it no longer does (step 2 of the approve-sources plan), and
# the name is kept for the seed's provenance and older review queues.
LOCATION_ATTRIBUTE = "found_location"

# The two coordinates a location is made of. The seed and refresh-seed record a location's
# provenance under these names, and responses cached before build step 4 of the source-matching
# plan returned them as values.
COORDINATE_ATTRIBUTES = ["found_latitude", "found_longitude"]

# No longer requested or loaded (steps 1 and 2 of the approve-sources plan): the agent returns only
# sources about the site searched, and the name and location each source gives are only used to
# check that, never to replace ours. Kept so older cached responses and the seed's provenance still
# read them.
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
# What a source reports as a value: an attribute we fill where ours is empty. Older responses also
# gave the facility's name and coordinates as values. Those are read, and never used as values: a
# source's name and location only check that it is about the site searched.
VALUE_ATTRIBUTES = list(GAP_FILL_ATTRIBUTES)
IDENTITY_VALUES = {*IDENTITY_ATTRIBUTES, *COORDINATE_ATTRIBUTES}

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

# Ranges the spec states. Checked after conversion; out-of-range values are not used.
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

# found_values.csv: every value the search found, one row per site, year, source and attribute,
# with its status (found_values.py). It replaces evidence.csv, resolved.csv, review_queue.csv and
# supplementary_leads.csv.
FOUND_VALUES_HEADERS = [
    "run_id", "site_id", *SITE_NAME_COLUMNS, "country_iso3", "year",
    # The source, as sources.csv lists it, and its tier.
    "source_id", "source_url", "source_title", "publisher", "source_tier", "tier_rule_applied", "link_status",
    # The facility the source describes, as it writes it: its name, address and coordinates or grid
    # reference (stated_coordinates). source_coordinates is the point read from them (locate.py),
    # "lat, lon" to paste into Google Maps, coordinates_from says from what, and distance_km is how
    # far that is from our site's coordinates - or, where they were too rough to give a point, how
    # far the farthest rough reading of them is. location_note says why there is no point.
    "facility_name_in_source", "facility_name_english", "source_address", "stated_coordinates",
    "source_coordinates", "coordinates_from", "distance_km", "location_note",
    # The value, converted and mapped, and as the agent wrote it; our_value is ours for that year.
    "attribute_name", "value", "unit", "value_as_written", "value_date", "our_value",
    # The agent's quote, and whether it, and the location the source states, are on the source's page
    # as the link check kept its text (on_page.py).
    "quote", "quote_on_page", "location_on_page", "agent_note", "value_basis", "agent_confidence",
    # review_id is the site-source pair whose status this is; accept-reviews fills the last two.
    "review_id", "status", "status_reason", "sme_decision", "loaded",
]

# accepted_values.csv: the one value to load for each site, attribute and year (for a yearly
# attribute), from the auto-approved pairs and the pairs SMEs approved (accept_reviews.py). What
# ingestion loads. site_id is where the value loads, which an SME's Different site can make another
# of our sites than the one searched; review_id names the pair it came from. approved_by is "auto",
# or the reviewer.
ACCEPTED_VALUES_HEADERS = [
    "run_id", "site_id", "site_name", "country_iso3", "attribute_name", "value", "unit", "year",
    "source_url", "source_tier", "approved_by", "reviewed_date", "review_id",
]

# The Site_Review tab: one row per site-source pair whose values wait on an SME (site_review.py).
SITE_REVIEW_HEADERS = [
    "review_id",
    # Our site, as our database holds it; our_coordinates is "lat, lon" to paste into Google Maps.
    "site_id", *SITE_NAME_COLUMNS, "country_iso3", "our_coordinates",
    # The source.
    "source_url", "source_title", "publisher", "source_tier", "link_status",
    # What the source says of the facility it describes, and where it places it, as found_values.csv
    # gives them: its address and coordinates as written, the point read from them, and why there is
    # none where a source states a location we couldn't place.
    "facility_name_in_source", "facility_name_english", "source_address", "stated_coordinates",
    "source_coordinates", "coordinates_from", "distance_km", "location_note",
    # Why the pair needs a second look, and what approving it would load: "attribute = value (year)".
    "flags", "would_load",
    # The SME's.
    "decision", "other_site_id", "reviewer", "reviewed_date", "notes",
]
# What an SME fills in on Site_Review; everything else there is set by the pipeline.
SITE_REVIEW_EDITABLE = ["decision", "other_site_id", "reviewer", "reviewed_date", "notes"]

# Batch 1's resolved.csv, Review_Queue and Leads columns, as main wrote them before the
# approve-sources plan. Nothing here writes those now (found_values.csv and Site_Review replace them),
# but the batch 1 re-key (#26, relabel.py) checks batch 1's files against these, and the batch 1
# conversion (build step 5) reads those files. They are batch 1's columns, not the ones the stack's own runs
# wrote before step 2.
RESOLVED_HEADERS = [
    "site_id", "internal_facility_id", *SITE_NAME_COLUMNS, "country_iso3", "attribute_name",
    "baseline_value", "baseline_source", "baseline_tier",
    "resolved_value", "resolved_unit", "resolution", "resolution_rule",
    "winning_evidence_id", "winning_source_tier", "winning_source_url", "winning_link_status",
    "best_tier_available",
    "value_date", "confidence_score", "validation_status", "reviewer", "reviewed_date",
    "researcher_notes",
]
REVIEW_QUEUE_HEADERS = [
    "site_id", *SITE_NAME_COLUMNS, "translation_note", "country_iso3", "baseline_coordinates",
    "attribute_name", "resolution",
    "baseline_value", "baseline_source", "baseline_tier", "resolved_value", "resolved_unit",
    "winning_source_tier", "winning_source_url", "winning_link_status", "value_date",
    "agreeing_source_count",
    "evidence_summary", "validation_status", "rejection_reason", "corrected_value", "reviewer",
    "reviewed_date",
    "researcher_notes",
]
SUPPLEMENTARY_LEADS_HEADERS = [
    "run_id", "site_id", *SITE_NAME_COLUMNS, "country_iso3", "attribute_name", "lead_value",
    "source_tier", "lead_summary", "url", "exclusion_reason",
]

# The validation_status resolve gives a decision that leaves our database as it is: a source
# confirmed our value, or nothing was found.
NOTHING_TO_REVIEW = "Nothing to review"

SOURCES_HEADERS = [
    "source_id", "url_normalized", "url", "source_title", "publisher", "source_type",
    "source_tier", "tier_rule_applied", "agent_proposed_tier", "publication_date",
    "has_publication_date", "language", "paywall_flag", "subscription_followup_needed",
    "first_seen_run_id", "times_cited", "cited_site_count",
]

FOUNDRY_RUN_LOG_HEADERS = [
    "run_id", "site_id", "site_name", "country_iso3", "requested_attributes", "agent_id",
    "request_started_at", "request_finished_at", "status", "raw_response_path",
    "parsed_attribute_count", "parsed_source_count", "error_message", "retry_count",
    "web_searches",
    # The model that answered, and its tokens, every attempt and pass added up (costs.py).
    "model", "input_tokens", "cached_input_tokens", "output_tokens", "reasoning_tokens",
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

# What an SME decides of each site-source pair on the Site_Review tab (site_review.py): the source
# is about this site of ours, about another site of ours (other_site_id says which), about a
# landfill not in our database, or of no use.
CORRECT_SITE = "Correct site"
DIFFERENT_SITE = "Different site"
NEW_SITE = "New site"
JUNK = "Junk"
SITE_REVIEW_DECISIONS = [CORRECT_SITE, DIFFERENT_SITE, NEW_SITE, JUNK]

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
    "source_tier": ["Tier 1", "Tier 2", "Tier 3", "Tier 4", "Tier 5"],
    # What an SME decides of a site-source pair on the Site_Review tab.
    "decision": SITE_REVIEW_DECISIONS,
    # Booleans as found values show them.
    "true_false": ["TRUE", "FALSE"],
}

# Columns a reviewer may edit, and the vocabulary each offers. Anything absent here is
# read-only in the workbook, so a dropdown never invites editing a pipeline-set field.
FIELD_TO_DEFINITION = {
    "decision": "decision",
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



def validate_foundry_payload(payload: dict[str, Any], iso3: str = "") -> list[str]:
    """What is malformed in a cached response, read through the one reader (payload.py). `iso3` is
    the site's country, which says which grids a grid reference may be in.

    A response from before the source-first contract is reported in its own terms, attributes[i],
    as it always was; a source-first one as sources[i].values[j]. The facility a source describes
    is checked too: each field is text, and stated_coordinates holds coordinates or a grid
    reference the locate step can read, not an address. An older response's kinds are checked as
    it is read (payload.read_payload).
    """
    from .locate import NO_LOCATION_TEXT, parse_point
    from .payload import FACILITY_FIELDS, PART_SEPARATOR, REQUIRED_VALUE_FIELDS, is_converted, read_payload

    read = read_payload(payload)
    warnings = list(read["problems"])
    for index, source in enumerate(read["sources"], start=1):
        if is_converted(source):
            continue
        if not normalize_scalar(source.get("url")):
            warnings.append(f"sources[{index}] has no clickable source URL.")
        for field in FACILITY_FIELDS:
            if isinstance(source.get(field), (dict, list, tuple)):
                warnings.append(f"sources[{index}] {field} must be text.")
        unread = [
            part for part in (part.strip() for part in normalize_scalar(source.get("stated_coordinates")).split(PART_SEPARATOR))
            if part and part.lower() not in NO_LOCATION_TEXT and parse_point(part, iso3) is None
        ]
        if unread:
            warnings.append(
                f"sources[{index}] stated_coordinates {' | '.join(unread)!r} is not coordinates or a grid reference."
            )
        for value_index, value in enumerate(source["values"], start=1):
            for field in REQUIRED_VALUE_FIELDS:
                if field not in value:
                    warnings.append(f"sources[{index}].values[{value_index}] missing required field {field!r}.")
            # Same canonicalisation as extract_evidence, so a pre-rename cached response is not
            # reported as unsupported by one and accepted by the other.
            name = canonical_attribute(normalize_scalar(value.get("attribute_name")))
            if name and name not in VALUE_ATTRIBUTES and name not in IDENTITY_VALUES:
                warnings.append(f"sources[{index}].values[{value_index}] has unsupported attribute_name {name!r}.")
    return list(dict.fromkeys(warnings))
