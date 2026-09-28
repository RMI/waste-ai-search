"""Read the standardized schema's *structure* from the live database.

The database is authoritative for structure - which columns exist, their types, and which values
a categorical column accepts. The vendored spec stays authoritative for semantics: what a column
means, its units, its numeric ranges, and how a derived value like `waste_depth` is produced.
Neither source subsumes the other, and this module covers only the first half.

Two places carry allowed values, and both have to be read:

- **`pg_enum`**, for columns backed by a real enum type (`facility_status`, `cover_type`, ...).
- **CHECK constraints**, for `text` columns constrained to a value list. `waste_depth` is one of
  these: it has a `chk_waste_depth` constraint and no enum type at all, so a generator that reads
  only `pg_enum` cannot see it.
"""
from __future__ import annotations

import re
from typing import Any


CONSOLIDATION_SCHEMA = "consolidation"
CONSOLIDATION_TABLE = "consolidated_facility"

# Postgres renders an allowed-value list as ARRAY['a'::text, 'b'::text] inside the constraint,
# whether the test is `col = ANY (...)` for a scalar or `col <@ ...` for an array column.
_ARRAY_BLOCK = re.compile(r"ARRAY\[(.*?)\]", re.DOTALL)
_TEXT_LITERAL = re.compile(r"'((?:[^']|'')*)'::text")


def parse_check_constraint_values(definition: str) -> list[str]:
    """Extract the allowed values from a CHECK constraint definition, in declared order.

    Returns [] when the constraint does not carry a value list - a range or NOT NULL test, say -
    so callers can tell "no vocabulary here" from "an empty vocabulary".
    """
    block = _ARRAY_BLOCK.search(definition or "")
    if block is None:
        return []

    values: list[str] = []
    for match in _TEXT_LITERAL.finditer(block.group(1)):
        value = match.group(1).replace("''", "'")
        if value not in values:
            values.append(value)
    return values


def fetch_check_constraint_values(
    schema: str = CONSOLIDATION_SCHEMA,
    table: str = CONSOLIDATION_TABLE,
    config: Any = None,
) -> dict[str, list[str]]:
    """{column: allowed values} for every single-column CHECK constraint carrying a value list.

    The column comes from `conkey` rather than from the constraint's name, so a constraint named
    off-pattern is still attributed to the right column.
    """
    from .db import fetch_all

    rows = fetch_all(
        """
        SELECT a.attname AS column_name,
               pg_get_constraintdef(con.oid) AS definition
        FROM pg_constraint con
        JOIN pg_class rel ON rel.oid = con.conrelid
        JOIN pg_namespace n ON n.oid = rel.relnamespace
        JOIN LATERAL unnest(con.conkey) AS k(attnum) ON TRUE
        JOIN pg_attribute a ON a.attrelid = rel.oid AND a.attnum = k.attnum
        WHERE n.nspname = %s AND rel.relname = %s AND con.contype = 'c'
        ORDER BY a.attname
        """,
        (schema, table),
        config=config,
    )

    found: dict[str, list[str]] = {}
    for row in rows:
        values = parse_check_constraint_values(row["definition"])
        if values:
            found[row["column_name"]] = values
    return found


def fetch_enum_values(enum_types: list[str], config: Any = None) -> dict[str, list[str]]:
    """{type name: labels} for the named Postgres enum types."""
    from .db import fetch_all

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
        (enum_types,),
        config=config,
    )
    return {row["name"]: list(row["labels"]) for row in rows}


def fetch_columns(schema: str, table: str, config: Any = None) -> dict[str, str]:
    """{column: data type} in ordinal order, for one table."""
    from .db import fetch_all

    rows = fetch_all(
        """
        SELECT column_name, data_type
        FROM information_schema.columns
        WHERE table_schema = %s AND table_name = %s
        ORDER BY ordinal_position
        """,
        (schema, table),
        config=config,
    )
    return {row["column_name"]: row["data_type"] for row in rows}


def compare_columns(expected: list[str], actual: dict[str, str]) -> tuple[list[str], list[str]]:
    """(columns the code expects but the table lacks, columns the table has that the code omits)."""
    missing = [column for column in expected if column not in actual]
    extra = [column for column in actual if column not in expected]
    return missing, extra
