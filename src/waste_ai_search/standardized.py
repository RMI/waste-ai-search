"""Emit a spec-conformant standardized facility table from arbitrated results.

Shape follows `schema/StandardizedFacilityTableSpecification.md` (vendored and pinned - see
`schema/SCHEMA_SOURCE.json`) and the live `transformed.transformed_*` tables: the spec's 43
columns plus `point`, one `data_source` per table.

Three accepted deviations from the spec, all settled deliberately:

- **`point` is emitted although the spec does not define it.** It is a real column on all 11
  `transformed.*` tables, derived here from latitude and longitude. The spec documents the two
  coordinate columns but not the geometry built from them.

- **One record per facility, not per facility-year (Q24).** The spec says `year` is the year to
  which all non-null values in the record apply, and would split a facility into several records
  when measurements come from different years. We keep a single record whose `year` is the latest
  year of record. A value dated earlier can therefore sit in a later-year record. Per-attribute
  `value_date` is preserved in the Resolved review layer, which the 43-column spec has nowhere to
  record. The seed makes the same compromise and tracks it in `backfilled_fields`.
- **`facility_id` carries `internal_facility_id` (Q27).** The spec types it as the source's own
  ID; AI search is keyed to facilities that already exist, so the internal ID is the useful key.
"""
from __future__ import annotations

import subprocess
from datetime import datetime
from pathlib import Path
from typing import Any

from .credibility import parse_year
from .schema import (
    AI_SEARCH_DATA_SOURCE,
    ARRAY_TARGET_ATTRIBUTES,
    ATTRIBUTE_TO_STANDARD_COLUMN,
    BOOLEAN_TARGET_ATTRIBUTES,
    INTEGER_TARGET_ATTRIBUTES,
    NUMERIC_ARRAY_TARGET_ATTRIBUTES,
    NUMERIC_TARGET_ATTRIBUTES,
    STANDARDIZED_FACILITY_COLUMNS,
    normalize_scalar,
    parse_tristate_bool,
)


# Resolutions that represent a value the source actually reported (Q28 keeps confirmations).
PROMOTED_RESOLUTIONS = {"Confirmed baseline", "Filled empty baseline", "Overrides baseline"}


def git_sha() -> str:
    try:
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=Path(__file__).resolve().parents[2],
            capture_output=True,
            text=True,
            timeout=5,
        )
        return result.stdout.strip() if result.returncode == 0 else ""
    except (OSError, subprocess.SubprocessError):
        return ""


def format_standard_value(attribute_name: str, value: Any) -> tuple[Any, str]:
    """Coerce a resolved value into the standardized column's type."""
    text = normalize_scalar(value)
    if not text:
        return None, ""

    if attribute_name in BOOLEAN_TARGET_ATTRIBUTES:
        parsed, note = parse_tristate_bool(text)
        return parsed, note

    if attribute_name in ARRAY_TARGET_ATTRIBUTES:
        items = [item.strip() for item in text.split(";") if item.strip()]
        return items or None, ""

    if attribute_name in NUMERIC_ARRAY_TARGET_ATTRIBUTES:
        values: list[float] = []
        for item in text.split(";"):
            item = item.strip().replace(",", "")
            if not item:
                continue
            try:
                values.append(float(item))
            except ValueError:
                return None, f"Could not parse number from {item!r}."
        return values or None, ""

    if attribute_name in INTEGER_TARGET_ATTRIBUTES:
        year = parse_year(text)
        return (year, "") if year is not None else (None, f"Could not parse year from {text!r}.")

    if attribute_name in NUMERIC_TARGET_ATTRIBUTES:
        try:
            return float(text.replace(",", "")), ""
        except ValueError:
            return None, f"Could not parse number from {text!r}."

    return text, ""


def build_standard_record(
    site: dict[str, Any],
    resolved_rows: list[dict[str, Any]],
    created_at: datetime | None = None,
    sha: str | None = None,
) -> tuple[dict[str, Any] | None, list[str]]:
    """One standardized record for a facility, or None if nothing was promoted."""
    promoted = [
        row
        for row in resolved_rows
        if normalize_scalar(row.get("resolution")) in PROMOTED_RESOLUTIONS
        and normalize_scalar(row.get("resolved_value"))
    ]
    if not promoted:
        return None, []

    notes: list[str] = []
    record: dict[str, Any] = {column: None for column in STANDARDIZED_FACILITY_COLUMNS}

    for row in promoted:
        attribute = normalize_scalar(row.get("attribute_name"))
        column = ATTRIBUTE_TO_STANDARD_COLUMN.get(attribute)
        if not column:
            continue
        value, note = format_standard_value(attribute, row.get("resolved_value"))
        if note:
            notes.append(f"{attribute}: {note}")
        if value is not None:
            record[column] = value

    # has_cover follows from cover_types; the spec carries both columns.
    if record.get("cover_types") and record.get("has_cover") is None:
        record["has_cover"] = True

    years = [parse_year(row.get("value_date")) for row in promoted]
    years = [year for year in years if year is not None]
    reference = parse_year(site.get("reference_year"))
    if years:
        record["year"] = max(years)
        if reference is not None and max(years) != reference:
            notes.append(f"year set to latest value_date {max(years)} (seed reference_year {reference}).")
    elif reference is not None:
        record["year"] = reference
        notes.append("No value_date on any promoted value; year fell back to seed reference_year.")

    record["facility_id"] = normalize_scalar(site.get("internal_facility_id") or site.get("site_id"))
    record["iso3c_plus"] = normalize_scalar(site.get("country_iso3")) or None
    record["data_source"] = AI_SEARCH_DATA_SOURCE
    record["created_date"] = (created_at or datetime.now()).replace(microsecond=0).isoformat()
    record["git_sha"] = sha if sha is not None else git_sha()

    if record.get("latitude") is not None and record.get("longitude") is not None:
        record["point"] = f"POINT({record['longitude']} {record['latitude']})"

    return record, notes


def build_standard_table(
    sites_by_id: dict[str, dict[str, Any]],
    resolved_rows: list[dict[str, Any]],
    created_at: datetime | None = None,
) -> tuple[list[dict[str, Any]], list[tuple[str, str]]]:
    """Group resolved rows by facility and emit one standardized record each.

    Notes come back as (site_id, note) pairs so the caller can put the site in its own column.
    Formatting the id into the message text made it unfilterable and left the column blank.
    """
    grouped: dict[str, list[dict[str, Any]]] = {}
    for row in resolved_rows:
        grouped.setdefault(normalize_scalar(row.get("site_id")), []).append(row)

    sha = git_sha()
    records: list[dict[str, Any]] = []
    all_notes: list[tuple[str, str]] = []
    for site_id, rows in grouped.items():
        site = sites_by_id.get(site_id)
        if site is None:
            continue
        record, notes = build_standard_record(site, rows, created_at=created_at, sha=sha)
        if record is not None:
            records.append(record)
        all_notes.extend((site_id, note) for note in notes)

    records.sort(key=lambda item: int(item["facility_id"]) if str(item["facility_id"]).isdigit() else 0)
    return records, all_notes


# The live database names its standardized source tables `transformed_<source>`; the spec
# suggests `std_facility_tbl_<source>`. Follow the database.
TARGET_TABLE = "transformed.transformed_ai_search"


def load_statement(csv_path: Path, table: str = TARGET_TABLE) -> str:
    """A psql load statement that names every column explicitly.

    `COPY` matches columns **by position, not by name**, and `WITH (HEADER)` only skips the first
    line - it does not align on names (`HEADER MATCH` needs PostgreSQL 16; the dev server is 15).
    So a bare `COPY tbl FROM file` silently depends on the CSV and the table agreeing on column
    ORDER. Naming the columns removes that dependency: the table can define them in any order.
    """
    # psql reads a backslash command's arguments from the rest of ONE line, so \copy must not
    # wrap: split across lines it stopped at the opening bracket and loaded nothing.
    columns = ", ".join(STANDARDIZED_FACILITY_COLUMNS)
    return (
        "-- Generated by waste-ai-search. Load by COLUMN NAME, never by position.\n"
        "--\n"
        "-- Run from this run's folder (the CSV path is relative to psql's working directory):\n"
        "--   psql \"$WASTEMAP_DSN\" -f " + csv_path.with_suffix(".load.sql").name + "\n"
        "--\n"
        "-- Every column is named, so the order of columns in the target table does not need to\n"
        "-- match the order in the CSV. Do NOT replace this with a bare COPY: that would match on\n"
        "-- position and load silently wrong data if the two orders ever diverge.\n"
        "\\set ON_ERROR_STOP on\n"
        f"\\copy {table} ({columns}) FROM '{csv_path.name}' WITH (FORMAT csv, HEADER true)\n"
    )


def write_load_statement(csv_path: Path, table: str = TARGET_TABLE) -> Path:
    path = csv_path.with_suffix(".load.sql")
    path.write_text(load_statement(csv_path, table), encoding="utf-8")
    return path


def to_csv_row(record: dict[str, Any]) -> dict[str, str]:
    """Render a record for CSV, keeping Postgres array literals for the array columns."""
    out: dict[str, str] = {}
    for column in STANDARDIZED_FACILITY_COLUMNS:
        value = record.get(column)
        if value is None:
            out[column] = ""
        elif isinstance(value, bool):
            out[column] = "true" if value else "false"
        elif isinstance(value, (list, tuple)):
            inner = ",".join(f'"{str(item)}"' for item in value)
            out[column] = "{" + inner + "}"
        else:
            out[column] = str(value)
    return out
