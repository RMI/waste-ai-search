from __future__ import annotations

import csv
from pathlib import Path
from typing import Any

from .schema import GEOCODE_FIELDS, WASTE_SITE_BASE_EXTRA_FIELDS, normalize_scalar


def read_csv_records(path: Path) -> tuple[list[dict[str, Any]], list[str]]:
    with path.open(newline="", encoding="utf-8-sig") as handle:
        reader = csv.DictReader(handle)
        rows = [dict(row) for row in reader]
        return rows, list(reader.fieldnames or [])


def normalize_site_record(row: dict[str, Any]) -> dict[str, Any]:
    normalized = dict(row)
    site_id = row.get("facility_id") or row.get("site_id") or row.get("id")
    site_name = row.get("facility_name") or row.get("site_name") or row.get("name")
    country_iso3 = row.get("iso3c_plus") or row.get("country_iso3") or row.get("iso3")
    area = row.get("area") or row.get("input_area_square_meters")

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

