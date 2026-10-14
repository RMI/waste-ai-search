from waste_ai_search import snapshot, source_names
from waste_ai_search.seed_source import (
    SELECT_FIELDS,
    dedupe_facility_rows,
    fold_facility_rows,
    is_empty,
    ledger_sql,
    select_sql,
    to_seed_record,
)


def row(facility_id, year, **overrides):
    base = {
        "internal_facility_id": facility_id,
        "year": year,
        "facility_name": "Olushosun Landfill",
        "iso3c_plus": "NGA",
        "area_square_meters": 487065.6,
        "latitude": 6.59,
        "longitude": 3.37,
        "is_location_exact": True,
        "opening_year": None,
        "closing_year": None,
        "area_square_meters_data_source": "osm_2022",
        "facility_status": None,
        "facility_type": None,
        "has_landfill_gas_collection": None,
        "annual_incoming_waste_metric_tonnes": None,
        "waste_in_place_metric_tonnes": None,
        "has_cover": None,
        "cover_types": None,
        "has_biocover": None,
    }
    base.update(overrides)
    return base


def test_one_record_per_internal_facility_id():
    rows = [row(11489, 2022), row(11489, 2021), row(11489, 2020), row(11467, 2022)]

    records = dedupe_facility_rows(rows)

    assert len(records) == 2
    assert sorted(r["internal_facility_id"] for r in records) == [11467, 11489]
    assert {r["source_row_count"] for r in records} == {1, 3}


def test_latest_year_is_the_reference_row():
    rows = [row(1, 2019, facility_status="Inactive"), row(1, 2024, facility_status="Active")]

    record = fold_facility_rows(rows)

    assert record["reference_year"] == 2024
    assert record["facility_status"] == "Active"
    assert record["source_year_min"] == 2019
    assert record["source_year_max"] == 2024
    assert record["backfilled_fields"] == ""


def test_empty_baseline_fields_backfill_from_most_recent_year_with_a_value():
    rows = [
        row(1, 2024, facility_status="Active"),
        row(1, 2022, waste_in_place_metric_tonnes=500),
        row(1, 2018, waste_in_place_metric_tonnes=100, facility_type="Dumpsite"),
    ]

    record = fold_facility_rows(rows)

    assert record["reference_year"] == 2024
    assert record["waste_in_place_metric_tonnes"] == 500
    assert record["facility_type"] == "Dumpsite"
    assert "waste_in_place_metric_tonnes@2022" in record["backfilled_fields"]
    assert "facility_type@2018" in record["backfilled_fields"]


def test_backfill_can_be_disabled():
    rows = [row(1, 2024), row(1, 2022, waste_in_place_metric_tonnes=500)]

    record = fold_facility_rows(rows, backfill=False)

    assert record["waste_in_place_metric_tonnes"] is None
    assert record["backfilled_fields"] == ""


def test_false_and_zero_are_kept_and_never_backfilled_over():
    rows = [
        row(1, 2024, has_landfill_gas_collection=False, annual_incoming_waste_metric_tonnes=0),
        row(1, 2020, has_landfill_gas_collection=True, annual_incoming_waste_metric_tonnes=900),
    ]

    record = fold_facility_rows(rows)

    assert record["has_landfill_gas_collection"] is False
    assert record["annual_incoming_waste_metric_tonnes"] == 0
    assert record["backfilled_fields"] == ""


def test_identity_fields_fall_back_to_any_year_that_has_them():
    rows = [row(1, 2024, opening_year=None), row(1, 2015, opening_year=1998)]

    record = fold_facility_rows(rows)

    assert record["opening_year"] == 1998


def test_is_empty_distinguishes_missing_from_falsey():
    assert is_empty(None)
    assert is_empty("  ")
    assert is_empty([])
    assert not is_empty(False)
    assert not is_empty(0)



def test_seed_record_matches_input_loader_shape():
    record = fold_facility_rows([row(11489, 2022, cover_types=["clay cover", "organic cover"])])

    seed = to_seed_record(record)

    assert seed["site_id"] == "11489"
    assert seed["facility_id"] == "11489"
    assert seed["site_name"] == "Olushosun Landfill"
    assert seed["country_iso3"] == "NGA"
    assert seed["input_area_square_meters"] == "487065.6"
    assert seed["year"] == "2022"
    assert seed["cover_types"] == "clay cover; organic cover"
    # has_biocover is now a real column on consolidated_facility, so the seed carries whatever the
    # source recorded -- but it is still never INFERRED. This row has organic cover and a NULL
    # has_biocover, and the seed must leave it empty rather than deriving TRUE from the cover type.
    assert seed["has_biocover"] == ""


def test_static_attributes_join_the_ledger_on_the_facility_alone():
    from waste_ai_search.seed_source import attribute_source_for

    record = {"internal_facility_id": "42", "reference_year": "2016"}
    static = {(42, "facility_name"): "lmop_2024"}
    assert attribute_source_for(record, "found_facility_name", static, {}, {}) == "lmop_2024"


def test_year_varying_attributes_join_on_the_year_the_value_came_from():
    """A backfilled value came from an older year, so crediting reference_year would be wrong."""
    from waste_ai_search.seed_source import attribute_source_for

    record = {"internal_facility_id": "42", "reference_year": "2016"}
    dated = {
        (42, 2016, "waste_in_place_metric_tonnes"): "usa_ghgrp_2026",
        (42, 2013, "waste_in_place_metric_tonnes"): "waste_atlas_landfills_2013",
    }

    assert attribute_source_for(record, "waste_in_place_metric_tonnes", {}, dated, {}) == "usa_ghgrp_2026"
    backfilled = {"waste_in_place_metric_tonnes": 2013}
    assert (
        attribute_source_for(record, "waste_in_place_metric_tonnes", {}, dated, backfilled)
        == "waste_atlas_landfills_2013"
    )


def test_missing_ledger_entry_yields_no_source():
    from waste_ai_search.seed_source import attribute_source_for

    assert attribute_source_for({"internal_facility_id": "42"}, "facility_status", {}, {}, {}) == ""
    assert attribute_source_for({"internal_facility_id": ""}, "facility_status", {}, {}, {}) == ""


def test_attach_only_stamps_attributes_that_have_a_value():
    from waste_ai_search.seed_source import attach_attribute_sources

    records = [
        {
            "internal_facility_id": "42",
            "reference_year": "2016",
            "backfilled_fields": "",
            "site_name": "Olushosun Landfill",
            "facility_status": "",
        }
    ]
    static = {(42, "facility_name"): "osm_2022", (42, "facility_status"): "lmop_2024"}

    attach_attribute_sources(records, static, {})

    assert records[0]["attribute_sources"] == "found_facility_name@osm_2022"


def test_the_database_is_read_by_wastemap_id():
    # Since RMI/waste_data_ingestion_pipeline#196 the consolidation tables key a facility by
    # wastemap_id; internal_facility_id is gone from them. Every query reads wastemap_id, and
    # names it internal_facility_id, the key the rest of this code and every run file use.
    assert SELECT_FIELDS[0] == "wastemap_id"  # what scripts/check_db_schema.py checks
    assert select_sql().startswith("SELECT wastemap_id AS internal_facility_id, year,")
    assert "ORDER BY wastemap_id, year DESC NULLS LAST" in select_sql()
    assert ledger_sql().startswith("SELECT wastemap_id AS internal_facility_id, year,")
    assert source_names.SINIR_MUNICIPALITY_SQL.startswith("SELECT DISTINCT l.wastemap_id AS internal_facility_id,")
    name_sql = source_names.source_name_sql(source_names.RAW_NAME_SOURCES["osm_2022"])
    assert name_sql.startswith("SELECT DISTINCT l.wastemap_id AS internal_facility_id,")
    assert name_sql.endswith("ORDER BY l.wastemap_id, original")
    assert snapshot.CONSOLIDATION_TABLES["consolidation.consolidated_facility"] == "wastemap_id, year"
    assert snapshot.CROSSWALK_ORDER.startswith("wastemap_id,")
    every_sql = [select_sql(), ledger_sql(), name_sql, source_names.SINIR_MUNICIPALITY_SQL,
                 snapshot.CROSSWALK_ORDER, *snapshot.CONSOLIDATION_TABLES.values()]
    for sql in every_sql:
        assert "internal_facility_id," not in sql.replace("AS internal_facility_id,", "")
