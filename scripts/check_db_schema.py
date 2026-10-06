#!/usr/bin/env python3
"""Check this repository's column bindings against the live database.

The database is authoritative for structure: which columns exist, and which values a categorical
column accepts. This fails loudly when the code and the database disagree, which is the failure
that matters - a rename upstream otherwise surfaces as a query error in the middle of a run, or
worse, as a value written to a column that no longer means what the code thinks.

It deliberately does NOT check semantics. Units, numeric ranges, and the rule for deriving
`waste_depth` from a measurement exist only in the vendored spec; the database has no column
comments and no range constraints on consolidated_facility. Use scripts/sync_schema.py for that
half.

Usage:  uv run python scripts/check_db_schema.py
Exit:   0 all agree, 1 drift found, 2 could not reach the database
"""
from __future__ import annotations

import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

# The table the generated load statement actually writes to (standardized.TARGET_TABLE). It does
# not exist yet, so the check falls back to a sibling to validate the column set and says so
# loudly - a green run against a sibling would otherwise imply a load that cannot work at all.
TRANSFORMED_SCHEMA = "transformed"
FALLBACK_TRANSFORMED_TABLE = "transformed_osm"


def main() -> int:
    from waste_ai_search.db import DatabaseConfig, fetch_all
    from waste_ai_search.db_enums import DB_ENUMS
    from waste_ai_search.db_introspect import (
        CONSOLIDATION_SCHEMA,
        CONSOLIDATION_TABLE,
        compare_columns,
        fetch_check_constraint_values,
        fetch_columns,
        fetch_enum_values,
    )
    from waste_ai_search.schema import (
        ARRAY_TARGET_ATTRIBUTES,
        ATTRIBUTE_TO_STANDARD_COLUMN,
        BOOLEAN_TARGET_ATTRIBUTES,
        INTEGER_TARGET_ATTRIBUTES,
        NUMERIC_ARRAY_TARGET_ATTRIBUTES,
        NUMERIC_TARGET_ATTRIBUTES,
        STANDARDIZED_FACILITY_COLUMNS,
    )
    from waste_ai_search.source_names import (
        LEDGER_NAME_COLUMN,
        RAW_NAME_SOURCES,
        source_name_sql,
    )
    from waste_ai_search.standardized import TARGET_TABLE
    from waste_ai_search.seed_source import SELECT_FIELDS

    config = DatabaseConfig.from_env()
    try:
        config.validate()
    except ValueError as exc:
        print(f"Configuration error: {exc}")
        return 2

    print(f"Target: {config.describe()}\n")

    # The load statement names this table, so it is the one that matters.
    load_schema, _, load_table = TARGET_TABLE.partition(".")

    try:
        consolidated = fetch_columns(CONSOLIDATION_SCHEMA, CONSOLIDATION_TABLE, config=config)
        transformed = fetch_columns(load_schema, load_table, config=config)
        constrained = fetch_check_constraint_values(config=config)
        enum_values = fetch_enum_values(
            [name for name in DB_ENUMS if name != "waste_depth"], config=config
        )
    except Exception as exc:
        print(f"Could not read the database: {type(exc).__name__}: {exc}")
        return 2

    problems: list[str] = []

    checked_table = TARGET_TABLE
    if not transformed:
        # Checking a sibling still validates the column set against a table built to the same
        # spec, but it must never read as "the load will work".
        transformed = fetch_columns(TRANSFORMED_SCHEMA, FALLBACK_TRANSFORMED_TABLE, config=config)
        checked_table = f"{TRANSFORMED_SCHEMA}.{FALLBACK_TRANSFORMED_TABLE}"
        problems.append(f"{TARGET_TABLE} does not exist; the generated load statement cannot run")
        print(f"FAIL  load target: {TARGET_TABLE} does not exist")
        print(f"        falling back to {checked_table} to validate the column set")

    # 1. Every column the seed SELECTs must exist on consolidated_facility. This is the check that
    #    would have caught `area` -> `area_square_meters` the day it landed.
    #
    #    SELECT_FIELDS is the list that goes into the SQL verbatim, so it is the right thing to
    #    check. PROVENANCE_FIELDS are deliberately NOT here: reference_year, source_year_min/max,
    #    source_row_count and backfilled_fields are computed by the fold, and attribute_sources is
    #    joined from the ledger. None of them is a column on this table.
    seed_fields = list(SELECT_FIELDS)
    seed_missing, _ = compare_columns(seed_fields, consolidated)
    label = f"{CONSOLIDATION_SCHEMA}.{CONSOLIDATION_TABLE}"
    if seed_missing:
        problems.append(f"seed_source reads columns absent from {label}: {seed_missing}")
        print(f"FAIL  seed columns: {len(seed_missing)} missing from {label}")
        for column in seed_missing:
            print(f"        {column}")
    else:
        print(f"ok    seed columns: all {len(seed_fields)} present on {label}")

    # 2. Every column written to the standardized table must exist on the target.
    target = checked_table
    std_missing, _ = compare_columns(STANDARDIZED_FACILITY_COLUMNS, transformed)
    if std_missing:
        problems.append(f"STANDARDIZED_FACILITY_COLUMNS names columns absent from {target}: {std_missing}")
        print(f"FAIL  standardized columns: {len(std_missing)} missing from {target}")
        for column in std_missing:
            print(f"        {column}")
    else:
        print(f"ok    standardized columns: all {len(STANDARDIZED_FACILITY_COLUMNS)} present on {target}")

    # 3. Types, not just names. A retype is as breaking as a rename and silent without this:
    #    `waste_depth` going text -> numeric would keep every column check green while every
    #    category written to it failed.
    #
    #    Expectations are DERIVED from how the code already classifies each attribute, not from a
    #    hand-kept table of 44 types that would duplicate the spec and rot beside it.
    #    Order matters. `gccs_ch4_flow_to_project_metric_tonnes` is in BOTH the numeric and the
    #    numeric-array sets, and it is an array column, so the array tests come first. Anything
    #    left over is a categorical stored as text.
    expected_kinds: dict[str, str] = {}
    for attribute, column in ATTRIBUTE_TO_STANDARD_COLUMN.items():
        if attribute in BOOLEAN_TARGET_ATTRIBUTES:
            kind = "boolean"
        elif attribute in ARRAY_TARGET_ATTRIBUTES or attribute in NUMERIC_ARRAY_TARGET_ATTRIBUTES:
            kind = "array"
        elif attribute in INTEGER_TARGET_ATTRIBUTES:
            kind = "integer"
        elif attribute in NUMERIC_TARGET_ATTRIBUTES:
            kind = "numeric"
        else:
            kind = "text"
        expected_kinds[column] = kind

    # information_schema spellings that satisfy each kind.
    KIND_TYPES = {
        "boolean": {"boolean"},
        "integer": {"integer", "bigint", "smallint"},
        "numeric": {"numeric", "double precision", "real", "integer", "bigint"},
        "array": {"ARRAY"},
        "text": {"text", "character varying", "USER-DEFINED"},
    }

    mismatched = [
        (column, kind, transformed[column])
        for column, kind in sorted(expected_kinds.items())
        if column in transformed and transformed[column] not in KIND_TYPES[kind]
    ]
    if mismatched:
        problems.append(f"column types disagree with the code on {target}: {mismatched}")
        print(f"FAIL  column types: {len(mismatched)} disagree on {target}")
        for column, kind, actual in mismatched:
            print(f"        {column}: code treats it as {kind}, database has {actual}")
    else:
        print(f"ok    column types: {len(expected_kinds)} checked on {target}")

    # 4. Committed vocabularies must match what the database actually permits.
    for name, committed in DB_ENUMS.items():
        live = constrained.get(name) if name in constrained else enum_values.get(name)
        source = "CHECK" if name in constrained else "enum type"
        if live is None:
            problems.append(f"{name}: no {source} found in the database")
            print(f"FAIL  {name}: not found in the database")
        elif list(live) != list(committed):
            problems.append(f"{name}: committed {committed} but database allows {live}")
            print(f"FAIL  {name} ({source}): committed {committed} != database {live}")
        else:
            print(f"ok    {name} ({source}): {len(committed)} values")

    # 4b. Values the code emits that the database does not accept yet. Reported, not failed: they
    #     are declared deliberately in PENDING_UPSTREAM_FACILITY_TYPES. But a load containing one
    #     is rejected, and COPY is all-or-nothing, so it must never be a surprise.
    from waste_ai_search.schema import PENDING_UPSTREAM_FACILITY_TYPES

    # The enum and consolidated_facility's CHECK are separate surfaces and roll out separately
    # (Transfer Station reached the transformed.* CHECKs before either), so a value counts as
    # accepted only once every surface that exists carries it.
    surfaces = {
        "facility_type enum": enum_values.get("facility_type"),
        "consolidated_facility chk_facility_type": constrained.get("facility_type"),
    }
    for value in PENDING_UPSTREAM_FACILITY_TYPES:
        missing = [name for name, values in surfaces.items() if values is not None and value not in values]
        if not missing:
            print(f"ok    facility_type {value!r}: now accepted by the enum and chk_facility_type - "
                  "remove it from PENDING_UPSTREAM_FACILITY_TYPES and regenerate db_enums.py")
        else:
            print(f"WARN  facility_type {value!r}: emitted by the pipeline but NOT accepted by "
                  f"{' or '.join(missing)}; any load containing it will be rejected until both "
                  "are extended upstream")

    # 5. The raw-name joins. Every table, join key, name, translation and language column in
    #    RAW_NAME_SOURCES is named as a string, so a typo in any of them is invisible to the
    #    offline tests and to every check above -- it would surface as a failure in the middle of
    #    seed generation. Running each join proves all five names at once.
    for data_source, spec in RAW_NAME_SOURCES.items():
        try:
            matched = fetch_all(
                f"SELECT count(*) AS n FROM ({source_name_sql(spec)}) t",
                (LEDGER_NAME_COLUMN, data_source),
                config=config,
            )[0]["n"]
        except Exception as exc:
            detail = str(exc).splitlines()[0]
            problems.append(f"raw name join for {data_source} is broken: {detail}")
            print(f"FAIL  raw name join {data_source}: {detail}")
            continue

        if matched == 0:
            # The SQL is valid but nothing joins, so the key column is the wrong one.
            problems.append(f"raw name join for {data_source} matched 0 rows")
            print(f"FAIL  raw name join {data_source}: valid SQL but 0 rows matched")
        else:
            print(f"ok    raw name join {data_source}: {matched} rows via {spec.table}")

    if problems:
        print(f"\n{len(problems)} problem(s). The database is authoritative for structure:")
        print("  - regenerate vocabularies with  uv run python scripts/generate_enums.py")
        print("  - fix column names in schema.py / seed_source.py to match the database")
        print("  - then re-sync the spec for the semantics:  scripts/sync_schema.py --check")
        return 1

    print("\nCode and database agree on structure.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
