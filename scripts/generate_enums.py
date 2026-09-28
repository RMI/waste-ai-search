#!/usr/bin/env python3
"""Generate src/waste_ai_search/db_enums.py from the live database's categorical definitions.

The standardized facility spec is explicit that the database is the single source of truth for
categorical values and that a hand-maintained Python copy would drift. This script keeps one
committed copy that is regenerated on demand rather than read at runtime, so arbitration stays
runnable offline.

Allowed values live in two places and both are read:

- **`pg_enum`**, for columns backed by a real enum type.
- **CHECK constraints** on `consolidation.consolidated_facility`, for `text` columns constrained
  to a value list. `waste_depth` is one of these - it has no enum type, so a generator reading
  only `pg_enum` would miss it entirely, which is how the 5m depth vocabulary went unnoticed
  when upstream introduced it.

Usage:  uv run python scripts/generate_enums.py
        uv run python scripts/generate_enums.py --check   # fail if the committed copy is stale
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

TARGET = ROOT / "src" / "waste_ai_search" / "db_enums.py"

# Categoricals backed by a real Postgres enum type.
ENUM_TYPES = [
    "facility_status",
    "facility_type",
    "cover_type",
    "gccs_energy_project_type",
    "gccs_current_project_status",
]

# Categoricals defined only by a CHECK constraint on consolidated_facility. The key is the
# column; the value is the constant name, which is spelled to match the concept rather than the
# column where they differ (`cover_types` the column vs `cover_type` the enum).
CHECK_CONSTRAINT_COLUMNS = {
    "waste_depth": "WASTE_DEPTH_VALUES",
}


def render_module(source: str, enums: dict[str, list[str]], checks: dict[str, list[str]]) -> str:
    """Render db_enums.py. Pure, so the output can be produced and tested without a database."""
    lines = [
        '"""Categorical values generated from the live database.',
        "",
        "DO NOT EDIT BY HAND. Regenerate with `uv run python scripts/generate_enums.py`.",
        f"Source: {source}",
        "",
        "Values come from Postgres enum types where one exists, and otherwise from the CHECK",
        "constraint on consolidation.consolidated_facility. `waste_depth` is the second kind: it",
        "is a text column with a constraint and no enum type.",
        '"""',
        "from __future__ import annotations",
        "",
        "",
    ]

    for name in ENUM_TYPES:
        lines.append(f"{name.upper()}_VALUES: list[str] = [")
        lines.extend(f"    {label!r}," for label in enums[name])
        lines.append("]")
        lines.append("")

    for column, const in CHECK_CONSTRAINT_COLUMNS.items():
        lines.append(f"# From the CHECK constraint on consolidated_facility.{column}.")
        lines.append(f"{const}: list[str] = [")
        lines.extend(f"    {label!r}," for label in checks[column])
        lines.append("]")
        lines.append("")

    lines.append("DB_ENUMS: dict[str, list[str]] = {")
    lines.extend(f"    {name!r}: {name.upper()}_VALUES," for name in ENUM_TYPES)
    lines.extend(f"    {column!r}: {const}," for column, const in CHECK_CONSTRAINT_COLUMNS.items())
    lines.append("}")
    lines.append("")

    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--check", action="store_true", help="exit 1 if the committed file is out of date"
    )
    args = parser.parse_args()

    from waste_ai_search.db import DatabaseConfig
    from waste_ai_search.db_introspect import fetch_check_constraint_values, fetch_enum_values

    config = DatabaseConfig.from_env()
    try:
        config.validate()
    except ValueError as exc:
        print(f"Configuration error: {exc}")
        return 1

    # Exit 2 for "could not reach the database" so a network blip is never mistaken for a stale
    # committed file, which is what --check exists to report.
    try:
        enums = fetch_enum_values(ENUM_TYPES, config=config)
        constrained = fetch_check_constraint_values(config=config)
    except Exception as exc:
        print(f"Could not read the database: {type(exc).__name__}: {exc}")
        return 2

    missing = [name for name in ENUM_TYPES if name not in enums]
    if missing:
        print(f"Missing enum types in database: {', '.join(missing)}")
        return 1

    missing_checks = [column for column in CHECK_CONSTRAINT_COLUMNS if column not in constrained]
    if missing_checks:
        print(
            "No CHECK constraint with a value list on consolidated_facility for: "
            f"{', '.join(missing_checks)}"
        )
        return 1

    # A vocabulary the database constrains but this script does not export is the exact blind
    # spot that let `waste_depth` slip through, so name it rather than silently ignoring it.
    enum_backed = {"cover_types": "cover_type", **{name: name for name in ENUM_TYPES}}
    unexported = [
        column
        for column in constrained
        if column not in CHECK_CONSTRAINT_COLUMNS and column not in enum_backed
    ]

    rendered = render_module(f"{config.host}/{config.dbname}", enums, constrained)

    stale = False
    if args.check:
        current = TARGET.read_text(encoding="utf-8") if TARGET.exists() else ""
        if current == rendered:
            print(f"{TARGET.relative_to(ROOT)} is up to date with {config.host}.")
        else:
            print(f"{TARGET.relative_to(ROOT)} is STALE; re-run without --check.")
            stale = True
    else:
        TARGET.write_text(rendered, encoding="utf-8")
        print(f"Wrote {TARGET.relative_to(ROOT)}")

    for name in ENUM_TYPES:
        print(f"  {name}: {len(enums[name])} labels (enum type) -> {enums[name]}")
    for column in CHECK_CONSTRAINT_COLUMNS:
        print(f"  {column}: {len(constrained[column])} labels (CHECK) -> {constrained[column]}")
    if unexported:
        print(
            "\nThese columns are constrained to a value list but are not exported here, so "
            "nothing\ndownstream can see them - the same blind spot that hid `waste_depth`. Add "
            "each to\nCHECK_CONSTRAINT_COLUMNS, or to the enum_backed map if an enum type "
            "already covers it:"
        )
        for column in unexported:
            print(f"  {column} -> {constrained[column]}")

    if args.check and (stale or unexported):
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
