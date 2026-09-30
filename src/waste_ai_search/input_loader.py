from __future__ import annotations

import csv
from pathlib import Path
from typing import Any

from .schema import GEOCODE_FIELDS, WASTE_SITE_BASE_EXTRA_FIELDS, is_blank, normalize_scalar


def read_csv_records(path: Path) -> tuple[list[dict[str, Any]], list[str]]:
    with path.open(newline="", encoding="utf-8-sig") as handle:
        reader = csv.DictReader(handle)
        rows = [dict(row) for row in reader]
        return rows, list(reader.fieldnames or [])


def first_present(row: dict[str, Any], *keys: str) -> Any:
    """The first key whose value is not blank, treating 0 and False as present."""
    for key in keys:
        if not is_blank(row.get(key)):
            return row.get(key)
    return None


def normalize_site_record(row: dict[str, Any]) -> dict[str, Any]:
    normalized = dict(row)
    site_id = row.get("facility_id") or row.get("site_id") or row.get("id")
    site_name = row.get("facility_name") or row.get("site_name") or row.get("name")
    country_iso3 = row.get("iso3c_plus") or row.get("country_iso3") or row.get("iso3")
    # `area` is the pre-8c0bb3fe spelling; seed CSVs generated before the rename still use
    # it, so both are accepted on read and only the new name is written back out.
    #
    # Picked on normalized emptiness rather than with `or`, because a zero area is a real value
    # the schema permits and `seed_source.is_empty` is careful to preserve. Reading a CSV these
    # arrive as the string "0", which is truthy, so `or` happened to work; an int 0 from any
    # other caller would silently fall through to an older alias.
    area = first_present(row, "area_square_meters", "input_area_square_meters", "area")

    normalized["site_id"] = normalize_scalar(site_id)
    normalized["site_name"] = normalize_scalar(site_name)
    normalized["country_iso3"] = normalize_scalar(country_iso3)
    normalized["input_area_square_meters"] = normalize_scalar(area)
    normalized.setdefault("pilot_selection_reason", "")
    for field in GEOCODE_FIELDS:
        normalized.setdefault(field, "")
    return normalized


def load_sites(path: Path) -> tuple[list[dict[str, Any]], list[str]]:
    rows, headers = read_csv_records(path)
    normalized = [normalize_site_record(row) for row in rows]
    output_headers = merge_headers(headers, WASTE_SITE_BASE_EXTRA_FIELDS)
    return normalized, output_headers


def merge_headers(*header_groups: list[str]) -> list[str]:
    headers: list[str] = []
    seen: set[str] = set()
    for group in header_groups:
        for header in group:
            if header and header not in seen:
                headers.append(header)
                seen.add(header)
    return headers


def write_csv_records(path: Path, rows: list[dict[str, Any]], headers: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=headers, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)



def resolve_sites(config: Any) -> tuple[list[dict[str, Any]], list[str]]:
    """The run's seed, from the database by default or from a CSV when one is configured.

    The database is the source of truth, so a run reads it directly rather than depending on a
    checked-in export that is stale the moment consolidation moves. `--input-csv` stays for two
    cases that still need a file: pinning an exact corpus, and the gas-collection follow-up,
    which rewrites the seed between passes.

    Country filtering is pushed into SQL rather than applied after loading, so a single-country
    run stops pulling the whole corpus to keep a handful of rows.
    """
    if config.input_csv is not None:
        return load_sites(config.input_csv)

    from .seed_source import load_seed_sites, seed_headers

    sites = load_seed_sites(iso3=config.iso3 or None)
    return sites, seed_headers()


def snapshot_seed(config: Any, sites: list[dict[str, Any]], headers: list[str]) -> Path | None:
    """Record what a database-seeded run actually searched, inside the run directory.

    Without this a run is not reproducible: the corpus moves under you between runs and nothing
    says which version produced a given output. A CSV-seeded run already has that file, so it is
    skipped there.
    """
    if config.input_csv is not None:
        return None
    path = config.run_dir / "seed.csv"
    write_csv_records(path, sites, headers)
    return path
