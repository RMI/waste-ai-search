#!/usr/bin/env python3
"""Generate src/waste_ai_search/db_enums.py from the live Postgres enum types.

The standardized facility spec is explicit that the database is the single source of truth for
categorical values and that a hand-maintained Python copy would drift. This script keeps one
committed copy that is regenerated on demand rather than read at runtime, so arbitration stays
runnable offline.

Usage:  uv run python scripts/generate_enums.py
"""
from __future__ import annotations

import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

TARGET = ROOT / "src" / "waste_ai_search" / "db_enums.py"

ENUM_TYPES = [
    "facility_status",
    "facility_type",
    "cover_type",
    "gccs_energy_project_type",
    "gccs_current_project_status",
]


def main() -> int:
    from waste_ai_search.db import DatabaseConfig, fetch_all

    config = DatabaseConfig.from_env()
    try:
        config.validate()
    except ValueError as exc:
        print(f"Configuration error: {exc}")
        return 1

    rows = fetch_all(
        """
        SELECT t.typname AS name,
               array_agg(e.enumlabel ORDER BY e.enumsortorder) AS labels
        FROM pg_type t
        JOIN pg_enum e ON e.enumtypid = t.oid
        WHERE t.typname = ANY(%s)
        GROUP BY t.typname
        ORDER BY t.typname
        """,
        (ENUM_TYPES,),
        config=config,
    )
    found = {row["name"]: list(row["labels"]) for row in rows}
    missing = [name for name in ENUM_TYPES if name not in found]
    if missing:
        print(f"Missing enum types in database: {', '.join(missing)}")
        return 1

    lines = [
        '"""Categorical values generated from the Postgres enum types.',
        "",
        "DO NOT EDIT BY HAND. Regenerate with `uv run python scripts/generate_enums.py`.",
        f"Source: {config.host}/{config.dbname}",
        '"""',
        "from __future__ import annotations",
        "",
        "",
    ]
    for name in ENUM_TYPES:
        const = name.upper() + "_VALUES"
        lines.append(f"{const}: list[str] = [")
        for label in found[name]:
            lines.append(f"    {label!r},")
        lines.append("]")
        lines.append("")

    lines.append("DB_ENUMS: dict[str, list[str]] = {")
    for name in ENUM_TYPES:
        lines.append(f"    {name!r}: {name.upper()}_VALUES,")
    lines.append("}")
    lines.append("")

    TARGET.write_text("\n".join(lines), encoding="utf-8")
    print(f"Wrote {TARGET.relative_to(ROOT)}")
    for name in ENUM_TYPES:
        print(f"  {name}: {len(found[name])} labels -> {found[name]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
