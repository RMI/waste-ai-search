#!/usr/bin/env python3
"""Verify connectivity to the wastemap Postgres database."""
from __future__ import annotations

import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))


def main() -> int:
    from waste_ai_search.db import DatabaseConfig, check_connection, list_tables

    config = DatabaseConfig.from_env()
    try:
        config.validate()
    except ValueError as exc:
        print(f"Configuration error: {exc}")
        return 1

    print("Target:", config.describe())
    try:
        info = check_connection(config)
    except Exception as exc:
        print(f"Connection failed: {type(exc).__name__}: {exc}")
        return 1

    print("Connected.")
    print("  database:", info.get("database"))
    print("  user:", info.get("user"))
    print("  server:", str(info.get("version", "")).split(" on ")[0])

    tables = list_tables(config=config)
    print(f"\nTables in public schema: {len(tables)}")
    for row in tables[:20]:
        print(f"  {row['table_name']}")
    if len(tables) > 20:
        print(f"  ... and {len(tables) - 20} more")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
