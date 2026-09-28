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

# The standardized table this pipeline writes. `point` is on every transformed.* table but is not
# in the spec, so it is expected here and reported by scripts/sync_schema.py's column test.
TRANSFORMED_SCHEMA = "transformed"
REFERENCE_TRANSFORMED_TABLE = "transformed_osm"


def main() -> int:
    from waste_ai_search.db import DatabaseConfig
    from waste_ai_search.db_enums import DB_ENUMS
    from waste_ai_search.db_introspect import (
        CONSOLIDATION_SCHEMA,
        CONSOLIDATION_TABLE,
        compare_columns,
        fetch_check_constraint_values,
        fetch_columns,
        fetch_enum_values,
    )
    from waste_ai_search.schema import STANDARDIZED_FACILITY_COLUMNS
    from waste_ai_search.seed_source import IDENTITY_FIELDS, PROVENANCE_FIELDS, TIME_VARYING_FIELDS

    config = DatabaseConfig.from_env()
    try:
        config.validate()
    except ValueError as exc:
        print(f"Configuration error: {exc}")
        return 2

    print(f"Target: {config.describe()}\n")
    try:
        consolidated = fetch_columns(CONSOLIDATION_SCHEMA, CONSOLIDATION_TABLE, config=config)
        transformed = fetch_columns(TRANSFORMED_SCHEMA, REFERENCE_TRANSFORMED_TABLE, config=config)
        constrained = fetch_check_constraint_values(config=config)
        enum_values = fetch_enum_values(
            [name for name in DB_ENUMS if name != "waste_depth"], config=config
        )
    except Exception as exc:
        print(f"Could not read the database: {type(exc).__name__}: {exc}")
        return 2

    problems: list[str] = []

    # 1. Every column the seed SELECTs must exist on consolidated_facility. This is the check that
    #    would have caught `area` -> `area_square_meters` the day it landed.
    seed_fields = [*IDENTITY_FIELDS, *TIME_VARYING_FIELDS, *PROVENANCE_FIELDS]
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
    target = f"{TRANSFORMED_SCHEMA}.{REFERENCE_TRANSFORMED_TABLE}"
    std_missing, _ = compare_columns(STANDARDIZED_FACILITY_COLUMNS, transformed)
    if std_missing:
        problems.append(f"STANDARDIZED_FACILITY_COLUMNS names columns absent from {target}: {std_missing}")
        print(f"FAIL  standardized columns: {len(std_missing)} missing from {target}")
        for column in std_missing:
            print(f"        {column}")
    else:
        print(f"ok    standardized columns: all {len(STANDARDIZED_FACILITY_COLUMNS)} present on {target}")

    # 3. Committed vocabularies must match what the database actually permits.
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
