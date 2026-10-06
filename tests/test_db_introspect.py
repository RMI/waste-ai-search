"""Parsing of CHECK constraint value lists.

The definitions below are the real `pg_get_constraintdef` output from
`consolidation.consolidated_facility` on the dev server, captured verbatim. Postgres renders
these three shapes differently, and `waste_depth` -- the one categorical with no enum type behind
it -- only appears in the first, so a parser that handles `<@` but not `= ANY` would miss exactly
the column this reader exists for.

These tests are offline. Whether the live database still matches is `scripts/check_db_schema.py`.
"""
from __future__ import annotations

import pytest

from waste_ai_search.db_introspect import compare_columns, parse_check_constraint_values


# `col = ANY (ARRAY[...])` -- a scalar text column.
CHK_WASTE_DEPTH = (
    "CHECK (((waste_depth IS NULL) OR (waste_depth = ANY (ARRAY['<=5m'::text, '>5m'::text]))))"
)
CHK_FACILITY_STATUS = (
    "CHECK (((facility_status IS NULL) OR (facility_status = ANY "
    "(ARRAY['Active'::text, 'Inactive'::text]))))"
)

# `col <@ ARRAY[...]` -- an array column tested by containment.
CHK_COVER_TYPES = (
    "CHECK (((cover_types IS NULL) OR (cover_types <@ ARRAY['clay cover'::text, "
    "'organic cover'::text, 'sand cover'::text, 'other soil mixture'::text])))"
)

# Containment plus a cardinality guard -- two predicates, still one ARRAY block.
CHK_GCCS_ENERGY_PROJECT_TYPE = (
    "CHECK (((gccs_energy_project_type IS NULL) OR ((cardinality(gccs_energy_project_type) > 0) "
    "AND (gccs_energy_project_type <@ ARRAY['Electricity Generation'::text, 'Direct Use'::text, "
    "'Renewable Natural Gas'::text, 'Other'::text]))))"
)


def test_scalar_equality_constraint_yields_its_values():
    assert parse_check_constraint_values(CHK_WASTE_DEPTH) == ["<=5m", ">5m"]


def test_the_waste_depth_values_are_the_ones_the_code_uses():
    """The whole point of reading constraints: waste_depth has no enum type to generate from."""
    from waste_ai_search.schema import WASTE_DEPTH_VALUES

    assert parse_check_constraint_values(CHK_WASTE_DEPTH) == WASTE_DEPTH_VALUES


def test_array_containment_constraint_yields_its_values():
    assert parse_check_constraint_values(CHK_COVER_TYPES) == [
        "clay cover",
        "organic cover",
        "sand cover",
        "other soil mixture",
    ]


def test_cardinality_guard_does_not_confuse_the_parser():
    """The `> 0` predicate sits outside the ARRAY block and must not leak into the values."""
    assert parse_check_constraint_values(CHK_GCCS_ENERGY_PROJECT_TYPE) == [
        "Electricity Generation",
        "Direct Use",
        "Renewable Natural Gas",
        "Other",
    ]


def test_declared_order_is_preserved():
    assert parse_check_constraint_values(CHK_FACILITY_STATUS) == ["Active", "Inactive"]


@pytest.mark.parametrize(
    "definition",
    [
        "CHECK ((year >= 1950))",
        "CHECK (((area_square_meters IS NULL) OR (area_square_meters >= (0)::numeric)))",
        "",
    ],
)
def test_a_constraint_with_no_value_list_yields_nothing(definition):
    """A range check is not a vocabulary; [] lets the caller skip it rather than record it empty."""
    assert parse_check_constraint_values(definition) == []


def test_an_escaped_quote_in_a_value_survives():
    definition = "CHECK ((x = ANY (ARRAY['it''s'::text, 'plain'::text])))"
    assert parse_check_constraint_values(definition) == ["it's", "plain"]


def test_duplicate_values_are_collapsed():
    definition = "CHECK ((x = ANY (ARRAY['a'::text, 'a'::text, 'b'::text])))"
    assert parse_check_constraint_values(definition) == ["a", "b"]


# --- column comparison --------------------------------------------------------------------
def test_compare_columns_reports_both_directions():
    missing, extra = compare_columns(
        ["facility_id", "waste_depth", "point"],
        {"facility_id": "text", "waste_depth": "text", "area_square_meters": "numeric"},
    )
    assert missing == ["point"]
    assert extra == ["area_square_meters"]


def test_compare_columns_is_quiet_when_they_agree():
    assert compare_columns(["a", "b"], {"a": "text", "b": "numeric"}) == ([], [])


# --- the generated file round-trips --------------------------------------------------------
def test_committed_db_enums_is_exactly_what_the_generator_renders():
    """`generate_enums.py --check` compares bytes, so a drifting renderer would fail every run.

    This pins the committed file to the renderer offline, using the values read from the dev
    server. It does not prove the database still holds these values -- that is
    `scripts/check_db_schema.py`, which needs a connection.
    """
    import importlib.util
    from pathlib import Path

    root = Path(__file__).resolve().parents[1]
    spec = importlib.util.spec_from_file_location("gen", root / "scripts" / "generate_enums.py")
    gen = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(gen)

    from waste_ai_search import db_enums

    enums = {name: db_enums.DB_ENUMS[name] for name in gen.ENUM_TYPES}
    checks = {column: db_enums.DB_ENUMS[column] for column in gen.CHECK_CONSTRAINT_COLUMNS}
    rendered = gen.render_module(
        "wastemap-database-dev.postgres.database.azure.com/postgres", enums, checks
    )

    committed = (root / "src" / "waste_ai_search" / "db_enums.py").read_text(encoding="utf-8")
    assert rendered == committed, (
        "db_enums.py is not what generate_enums.py would write. Regenerate it rather than "
        "editing it by hand."
    )


def test_waste_depth_is_exported_as_a_check_backed_vocabulary():
    """Guards the blind spot: waste_depth must not quietly fall out of the generated file."""
    import importlib.util
    from pathlib import Path

    root = Path(__file__).resolve().parents[1]
    spec = importlib.util.spec_from_file_location("gen", root / "scripts" / "generate_enums.py")
    gen = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(gen)

    from waste_ai_search.db_enums import DB_ENUMS

    assert "waste_depth" in gen.CHECK_CONSTRAINT_COLUMNS
    assert "waste_depth" not in gen.ENUM_TYPES  # there is no enum type for it
    assert DB_ENUMS["waste_depth"] == ["<=5m", ">5m"]


@pytest.mark.parametrize(
    "answer, expected",
    [
        ("transfer station", "Transfer Station"),
        ("waste transfer station", "Transfer Station"),
        ("recycling center", "Recycling Center"),
        ("recycling centre", "Recycling Center"),
        ("recycling depot", "Recycling Center"),
        ("materials recovery facility", "Recycling Center"),
    ],
)
def test_every_new_facility_type_answer_maps_to_its_value(answer, expected):
    from waste_ai_search.schema import map_enum_value

    assert map_enum_value("facility_type", answer)[0] == expected


def test_transfer_station_is_emitted_as_a_pending_value():
    """In every transformed.* CHECK, but not yet the enum type or consolidated_facility's CHECK."""
    from waste_ai_search.db_enums import FACILITY_TYPE_VALUES
    from waste_ai_search.schema import FACILITY_TYPE_MAP, PENDING_UPSTREAM_FACILITY_TYPES

    assert FACILITY_TYPE_MAP["transfer station"] == "Transfer Station"
    assert "Transfer Station" in PENDING_UPSTREAM_FACILITY_TYPES

    # Nothing the map emits may fall outside what the database accepts - except values explicitly
    # declared as waiting on an upstream enum change. That list is the only way past this check,
    # so a stray value still fails here rather than at load time.
    from waste_ai_search.schema import PENDING_UPSTREAM_FACILITY_TYPES

    emitted = {v for v in FACILITY_TYPE_MAP.values() if v is not None}
    assert emitted <= set(FACILITY_TYPE_VALUES) | set(PENDING_UPSTREAM_FACILITY_TYPES)


def test_the_pending_upstream_facility_types_are_pinned():
    """Pins the exception. Adding to it is a deliberate decision to emit a value the DB rejects."""
    from waste_ai_search.db_enums import FACILITY_TYPE_VALUES
    from waste_ai_search.schema import PENDING_UPSTREAM_FACILITY_TYPES

    assert PENDING_UPSTREAM_FACILITY_TYPES == ["Transfer Station", "Recycling Center", "Not a Waste Facility"]
    # The enum alone is not the gate: consolidated_facility's chk_facility_type is separate and can
    # lag. When this fails, run check_db_schema.py and remove the value from the pending list only
    # once it reports the value accepted by both.
    for value in PENDING_UPSTREAM_FACILITY_TYPES:
        assert value not in FACILITY_TYPE_VALUES, (
            f"{value!r} is now in the enum. Remove it from PENDING_UPSTREAM_FACILITY_TYPES only once "
            "check_db_schema.py reports it accepted by the enum AND chk_facility_type."
        )


def test_first_present_keeps_a_zero(): 
    """A zero area is a real value; `or`-chaining silently skipped an int 0 (Copilot review)."""
    from waste_ai_search.input_loader import first_present

    assert first_present({"a": 0, "b": 999}, "a", "b") == 0
    assert first_present({"a": "0", "b": "999"}, "a", "b") == "0"
    assert first_present({"a": "", "b": 999}, "a", "b") == 999
    assert first_present({"a": None, "b": 999}, "a", "b") == 999
    assert first_present({}, "a", "b") is None
