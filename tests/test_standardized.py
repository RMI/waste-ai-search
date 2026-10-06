from waste_ai_search.schema import STANDARDIZED_FACILITY_COLUMNS
from waste_ai_search.standardized import build_standard_record, to_csv_row


SITE = {
    "site_id": "11489",
    "internal_facility_id": "11489",
    "country_iso3": "NGA",
    "reference_year": "2022",
}


def resolved(attribute, value, resolution="Filled empty baseline", value_date="2024"):
    return {
        "attribute_name": attribute,
        "resolution": resolution,
        "resolved_value": value,
        "value_date": value_date,
    }


def test_column_set_matches_the_spec():
    """43 spec columns plus has_biocover, which is not published in the spec yet."""
    assert len(STANDARDIZED_FACILITY_COLUMNS) == 44
    assert STANDARDIZED_FACILITY_COLUMNS[0] == "facility_id"
    assert STANDARDIZED_FACILITY_COLUMNS[-3:] == ["data_source", "created_date", "git_sha"]
    # Grouped with the other cover columns; must match wherever the real DDL puts it.
    assert STANDARDIZED_FACILITY_COLUMNS.index("has_biocover") == (
        STANDARDIZED_FACILITY_COLUMNS.index("cover_types") + 1
    )


def test_nothing_promoted_yields_no_record():
    record, _notes = build_standard_record(SITE, [resolved("facility_status", "Active", "Conflict - needs review")], sha="x")
    assert record is None


def test_confirmations_are_included():
    record, _notes = build_standard_record(SITE, [resolved("facility_status", "Active", "Confirmed baseline")], sha="x")
    assert record is not None
    assert record["facility_status"] == "Active"


def test_facility_id_carries_the_internal_id_as_text():
    record, _notes = build_standard_record(SITE, [resolved("facility_status", "Active")], sha="x")
    assert record["facility_id"] == "11489"
    assert isinstance(record["facility_id"], str)


def test_year_is_the_latest_value_date():
    record, notes = build_standard_record(
        SITE,
        [resolved("facility_status", "Active", value_date="2024"), resolved("opening_year", "1998", value_date="2019")],
        sha="x",
    )
    assert record["year"] == 2024
    assert any("latest value_date" in note for note in notes)


def test_year_falls_back_to_reference_year_and_says_so():
    record, notes = build_standard_record(SITE, [resolved("facility_status", "Active", value_date="")], sha="x")
    assert record["year"] == 2022
    assert any("fell back" in note for note in notes)


def test_unknown_boolean_becomes_null():
    record, _notes = build_standard_record(SITE, [resolved("has_landfill_gas_collection", "Unknown")], sha="x")
    assert record is None or record["has_landfill_gas_collection"] is None


def test_boolean_true_is_stored_as_a_bool():
    record, _notes = build_standard_record(SITE, [resolved("has_landfill_gas_collection", "TRUE")], sha="x")
    assert record["has_landfill_gas_collection"] is True


def test_cover_types_becomes_an_array_and_implies_has_cover():
    record, _notes = build_standard_record(SITE, [resolved("cover_types", "clay cover; organic cover")], sha="x")
    assert record["cover_types"] == ["clay cover", "organic cover"]
    assert record["has_cover"] is True


def test_coordinates_produce_a_point():
    record, _notes = build_standard_record(
        SITE,
        [resolved("found_latitude", "6.5947"), resolved("found_longitude", "3.3769")],
        sha="x",
    )
    assert record["latitude"] == 6.5947
    assert record["point"] == "POINT(3.3769 6.5947)"


def test_identity_name_lands_in_facility_name():
    record, _notes = build_standard_record(SITE, [resolved("found_facility_name", "Olushosun Landfill")], sha="x")
    assert record["facility_name"] == "Olushosun Landfill"


def test_data_source_token_is_stamped():
    record, _notes = build_standard_record(SITE, [resolved("facility_status", "Active")], sha="deadbeef")
    assert record["data_source"] == "ai_search_2026"
    assert record["git_sha"] == "deadbeef"


def test_csv_rendering_uses_postgres_array_and_bool_literals():
    record, _notes = build_standard_record(
        SITE,
        [resolved("cover_types", "clay cover"), resolved("has_landfill_gas_collection", "TRUE")],
        sha="x",
    )
    row = to_csv_row(record)
    assert row["cover_types"] == '{"clay cover"}'
    assert row["has_landfill_gas_collection"] == "true"
    assert row["waste_depth"] == ""
    assert set(row) == set(STANDARDIZED_FACILITY_COLUMNS)


def test_notes_carry_the_site_id_separately_from_the_message():
    """Formatting the id into the text left the site_id column blank and unfilterable."""
    from waste_ai_search.standardized import build_standard_table

    sites = {"11489": dict(SITE, site_name="Olushosun Landfill")}
    rows = [dict(resolved("facility_status", "Active", value_date="2024"), site_id="11489")]

    _records, notes = build_standard_table(sites, rows)

    assert notes, "expected a year-provenance note"
    site_id, message = notes[0]
    assert site_id == "11489"
    assert not message.startswith("11489")


def test_the_load_statement_names_every_column():
    """COPY matches on position; naming the columns makes table order irrelevant."""
    from pathlib import Path

    from waste_ai_search.standardized import load_statement

    sql = load_statement(Path("std_facility_tbl_ai_search_r.csv"))

    for column in STANDARDIZED_FACILITY_COLUMNS:
        assert column in sql, column
    assert _copied_columns(sql) == STANDARDIZED_FACILITY_COLUMNS


def _copied_columns(sql):
    """The column list of the \\copy line - which psql requires to be ONE line."""
    copy_lines = [line for line in sql.splitlines() if line.startswith("\\copy")]
    assert len(copy_lines) == 1 and copy_lines[0].rstrip().endswith("HEADER true)"), copy_lines
    block = copy_lines[0].split("(", 1)[1].split(")", 1)[0]
    return [column.strip() for column in block.split(",")]


def test_a_failed_load_stops_psql():
    from pathlib import Path

    from waste_ai_search.standardized import load_statement

    assert "\\set ON_ERROR_STOP on" in load_statement(Path("x.csv"))


def test_the_load_statement_is_not_a_bare_copy():
    from pathlib import Path

    from waste_ai_search.standardized import load_statement

    sql = load_statement(Path("x.csv"))
    assert "\\copy transformed.transformed_ai_search (" in sql
    assert "HEADER true" in sql
    # a bare table reference with no column list is the failure mode being prevented
    assert "transformed_ai_search FROM" not in sql


def test_the_load_statement_survives_reordering_the_column_list(monkeypatch):
    """Moving has_biocover must change the CSV and the load statement together."""
    from pathlib import Path

    from waste_ai_search import standardized

    reordered = [c for c in STANDARDIZED_FACILITY_COLUMNS if c != "has_biocover"] + ["has_biocover"]
    monkeypatch.setattr(standardized, "STANDARDIZED_FACILITY_COLUMNS", reordered)

    sql = standardized.load_statement(Path("x.csv"))
    assert _copied_columns(sql)[-1] == "has_biocover"


def test_write_load_statement_lands_beside_the_csv(tmp_path):
    from waste_ai_search.standardized import write_load_statement

    csv_path = tmp_path / "std_facility_tbl_ai_search_r.csv"
    csv_path.write_text("facility_id\n1\n", encoding="utf-8")

    sql_path = write_load_statement(csv_path)

    assert sql_path.name == "std_facility_tbl_ai_search_r.load.sql"
    assert sql_path.parent == csv_path.parent
    assert csv_path.name in sql_path.read_text()
