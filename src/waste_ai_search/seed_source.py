"""Seed the metadata search from consolidation.consolidated_facility.

The table holds one row per (internal_facility_id, year): 44k rows describing 15.5k
physical facilities. Identity columns (name, country, coordinates, area_square_meters) are constant
within a facility, so collapsing to one row per internal_facility_id is lossless for
identity. The time-varying measurements are not constant, so the fold keeps the latest
year as the reference row and backfills only the still-empty baseline fields from the
most recent year that has a value, recording where each came from.
"""
from __future__ import annotations

from typing import Any, Iterable, Sequence

from .schema import ATTRIBUTE_TO_STANDARD_COLUMN, REQUESTABLE_ATTRIBUTES, normalize_scalar


SOURCE_SCHEMA = "consolidation"
SOURCE_TABLE = "consolidated_facility"
LEDGER_TABLE = "value_resolution_ledger"
FACILITY_KEY = "internal_facility_id"

# Which ledger column holds the provenance for each attribute we search. The three identity
# attributes are named for the search, not for the database, so they need mapping.
LEDGER_COLUMNS = {
    attribute: ATTRIBUTE_TO_STANDARD_COLUMN.get(attribute, attribute)
    for attribute in REQUESTABLE_ATTRIBUTES
}

# Constant within a facility (verified: 0 of 3,304 multi-row facilities vary on these).
IDENTITY_FIELDS = [
    "facility_name",
    "iso3c_plus",
    "area_square_meters",
    "latitude",
    "longitude",
    "is_location_exact",
    "opening_year",
    "closing_year",
    "area_square_meters_data_source",
]

# Vary by year. Backfilled from the most recent non-null year when the reference row is empty.
TIME_VARYING_FIELDS = [
    "facility_status",
    "facility_type",
    "has_landfill_gas_collection",
    "waste_depth",
    "annual_incoming_waste_metric_tonnes",
    "waste_in_place_metric_tonnes",
    "has_cover",
    "cover_types",
    # Added upstream in 8c0bb3fe. Before it existed the baseline was always empty, so gap-fill
    # asked every facility; reading it means only facilities with no recorded value are searched.
    "has_biocover",
    "gccs_ch4_flared_metric_tonnes",
    "gccs_ch4_generated_metric_tonnes",
    "gccs_ch4_collected_metric_tonnes",
    "gccs_energy_project_type",
    "gccs_current_project_status",
    "gccs_ch4_flow_to_project_metric_tonnes",
    "gccs_ch4_percent",
    "gccs_collection_efficiency",
    "oxidation",
    "mcf",
    "ch4_emissions_metric_tonnes",
    "food_percent_by_weight",
    "green_percent_by_weight",
    "wood_percent_by_weight",
    "paper_cardboard_percent_by_weight",
    "textiles_percent_by_weight",
    "plastic_percent_by_weight",
    "metal_percent_by_weight",
    "glass_percent_by_weight",
    "rubber_percent_by_weight",
    "other_percent_by_weight",
    "contributing_data_sources",
    "entity_linkage_strategy",
    "consolidation_strategy",
]

PROVENANCE_FIELDS = [
    "reference_year",
    "source_year_min",
    "source_year_max",
    "source_row_count",
    "backfilled_fields",
    # Per-attribute provenance from the ledger, so credibility is judged on the dataset that
    # supplied *this* value rather than the best of everything the facility draws on.
    "attribute_sources",
]

SELECT_FIELDS = [FACILITY_KEY, "year", *IDENTITY_FIELDS, *TIME_VARYING_FIELDS]

def select_sql(where: str = "") -> str:
    columns = ", ".join(SELECT_FIELDS)
    clause = f"WHERE {where}\n" if where else ""
    return (
        f"SELECT {columns}\n"
        f"FROM {SOURCE_SCHEMA}.{SOURCE_TABLE}\n"
        f"{clause}"
        f"ORDER BY {FACILITY_KEY}, year DESC NULLS LAST"
    )


def is_empty(value: Any) -> bool:
    """Empty for folding purposes. False and 0 are real values and are never empty."""
    if value is None:
        return True
    if isinstance(value, str):
        return value.strip() == ""
    if isinstance(value, (list, tuple)):
        return len(value) == 0
    return False


def format_value(value: Any) -> str:
    if isinstance(value, (list, tuple)):
        return "; ".join(normalize_scalar(item) for item in value if not is_empty(item))
    return normalize_scalar(value)



def fold_facility_rows(rows: Sequence[dict[str, Any]], backfill: bool = True) -> dict[str, Any]:
    """Collapse all year-rows of one physical facility into a single seed record."""
    if not rows:
        raise ValueError("fold_facility_rows requires at least one row.")

    ordered = sorted(
        rows,
        key=lambda row: (row.get("year") is not None, row.get("year") or 0),
        reverse=True,
    )
    reference = ordered[0]
    record: dict[str, Any] = {FACILITY_KEY: reference.get(FACILITY_KEY)}

    for field in IDENTITY_FIELDS:
        value = next((row.get(field) for row in ordered if not is_empty(row.get(field))), None)
        record[field] = value

    backfilled: list[str] = []
    for field in TIME_VARYING_FIELDS:
        value = reference.get(field)
        if is_empty(value) and backfill:
            for row in ordered[1:]:
                if not is_empty(row.get(field)):
                    value = row.get(field)
                    backfilled.append(f"{field}@{normalize_scalar(row.get('year'))}")
                    break
        record[field] = value

    years = [row.get("year") for row in ordered if row.get("year") is not None]
    record["reference_year"] = reference.get("year")
    record["source_year_min"] = min(years) if years else None
    record["source_year_max"] = max(years) if years else None
    record["source_row_count"] = len(rows)
    record["backfilled_fields"] = "; ".join(backfilled)
    return record


def dedupe_facility_rows(
    rows: Iterable[dict[str, Any]],
    backfill: bool = True,
) -> list[dict[str, Any]]:
    """One record per internal_facility_id, in first-seen facility order."""
    grouped: dict[Any, list[dict[str, Any]]] = {}
    for row in rows:
        key = row.get(FACILITY_KEY)
        if key is None:
            continue
        grouped.setdefault(key, []).append(row)
    return [fold_facility_rows(group, backfill=backfill) for group in grouped.values()]


def to_seed_record(record: dict[str, Any]) -> dict[str, Any]:
    """Map a folded record onto the CSV seed shape consumed by input_loader.load_sites."""
    seed: dict[str, Any] = {"facility_id": normalize_scalar(record.get(FACILITY_KEY))}
    for field in [*IDENTITY_FIELDS, *TIME_VARYING_FIELDS, *PROVENANCE_FIELDS]:
        seed[field] = format_value(record.get(field))
    seed["year"] = normalize_scalar(record.get("reference_year"))
    seed["internal_facility_id"] = seed["facility_id"]
    seed["site_id"] = seed["facility_id"]
    seed["site_name"] = seed["facility_name"]
    seed["country_iso3"] = seed["iso3c_plus"]
    seed["input_area_square_meters"] = seed["area_square_meters"]
    seed["pilot_selection_reason"] = ""
    return seed


def seed_headers() -> list[str]:
    from .input_loader import merge_headers
    from .schema import GEOCODE_FIELDS

    return merge_headers(
        ["facility_id", "year", *IDENTITY_FIELDS, *TIME_VARYING_FIELDS],
        PROVENANCE_FIELDS,
        ["internal_facility_id", "site_id", "site_name", "country_iso3", "input_area_square_meters", "pilot_selection_reason"],
        list(GEOCODE_FIELDS),
    )


def load_seed_sites(
    config: Any = None,
    iso3: Sequence[str] | None = None,
    require_coordinates: bool = False,
    backfill: bool = True,
) -> list[dict[str, Any]]:
    """Read consolidated_facility and return one seed record per physical facility."""
    from .db import fetch_all

    clauses: list[str] = []
    params: list[Any] = []
    if iso3:
        clauses.append("iso3c_plus = ANY(%s)")
        params.append([code.strip().upper() for code in iso3])
    if require_coordinates:
        clauses.append("latitude IS NOT NULL AND longitude IS NOT NULL")

    rows = fetch_all(select_sql(" AND ".join(clauses)), tuple(params) or None, config=config)
    folded = dedupe_facility_rows(rows, backfill=backfill)
    records = [to_seed_record(record) for record in folded]

    static, dated = load_attribute_sources(config=config)
    attach_attribute_sources(records, static, dated)
    records.sort(key=lambda item: int(item["facility_id"]) if item["facility_id"].isdigit() else 0)
    return records


def ledger_sql() -> str:
    return (
        "SELECT internal_facility_id, year, column_name, data_source, resolution_granularity\n"
        f"FROM {SOURCE_SCHEMA}.{LEDGER_TABLE}\n"
        "WHERE column_name = ANY(%s)"
    )


def load_attribute_sources(config: Any = None) -> tuple[dict[Any, str], dict[Any, str]]:
    """Read per-attribute provenance from the value resolution ledger.

    Returns two lookups because the ledger records provenance at two grains:
    `facility` rows carry no year (static attributes), `facility_year` rows do.
    """
    from .db import fetch_all

    wanted = sorted(set(LEDGER_COLUMNS.values()))
    rows = fetch_all(ledger_sql(), (wanted,), config=config)
    static: dict[Any, str] = {}
    dated: dict[Any, str] = {}
    for row in rows:
        facility_id = row["internal_facility_id"]
        column = row["column_name"]
        source = normalize_scalar(row["data_source"])
        if normalize_scalar(row["resolution_granularity"]) == "facility":
            static[(facility_id, column)] = source
        else:
            dated[(facility_id, row["year"], column)] = source
    return static, dated


def attribute_source_for(
    record: dict[str, Any],
    attribute: str,
    static: dict[Any, str],
    dated: dict[Any, str],
    backfilled: dict[str, int],
) -> str:
    """The dataset that supplied one attribute's baseline value.

    A static attribute is keyed on the facility alone. A year-varying one is keyed on the year the
    seed fold actually took the value from, which is the backfill year when the reference year was
    empty — otherwise the lookup would credit the wrong year's source.
    """
    column = LEDGER_COLUMNS.get(attribute, attribute)
    # The ledger keys are integers; a seed record has already stringified its ids and years.
    facility_id = as_int(record.get(FACILITY_KEY))
    if facility_id is None:
        return ""
    if (facility_id, column) in static:
        return static[(facility_id, column)]
    year = backfilled.get(column, as_int(record.get("reference_year")))
    return dated.get((facility_id, year, column), "")


def as_int(value: Any) -> int | None:
    text = normalize_scalar(value)
    try:
        return int(text)
    except (TypeError, ValueError):
        return None


def attach_attribute_sources(
    records: list[dict[str, Any]],
    static: dict[Any, str],
    dated: dict[Any, str],
) -> int:
    """Stamp `attribute_sources` as `attribute@dataset` pairs, for attributes that have a value."""
    from .credibility import backfilled_years

    stamped = 0
    for record in records:
        backfilled = backfilled_years(record.get("backfilled_fields"))
        pairs = []
        for attribute in LEDGER_COLUMNS:
            column = LEDGER_COLUMNS[attribute]
            seed_column = "site_name" if column == "facility_name" else column
            if is_empty(record.get(seed_column)):
                continue
            source = attribute_source_for(record, attribute, static, dated, backfilled)
            if source:
                pairs.append(f"{attribute}@{source}")
        record["attribute_sources"] = "; ".join(pairs)
        stamped += len(pairs)
    return stamped
