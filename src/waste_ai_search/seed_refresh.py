"""Merge arbitrated results back into the seed so a later pass sees an updated baseline.

This is what makes the GCCS gate lossless. `has_landfill_gas_collection` is itself gap-filled, so
a facility whose collection system is only discovered during pass 1 has its GCCS attributes
requested on pass 2 once that finding is promoted into the baseline.

The refreshed seed keeps every original column, so baseline tiering, staleness and gap-fill all
continue to work. Two columns are added:

- `ai_filled_fields`  - `field@Tier N` per value this pass supplied, so the next pass tiers the
  value by the source that produced it rather than by the seed's own datasets.
- `ai_filled_run_ids` - which run supplied them.

Values still awaiting human review are merged too, because the point is to stop re-asking
questions already answered; `ai_filled_fields` keeps them distinguishable from source data.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from .credibility import ai_filled_tiers
from .schema import (
    GAP_FILL_ATTRIBUTES,
    is_blank,
    normalize_scalar,
)


REFRESH_COLUMNS = ["ai_filled_fields", "ai_filled_run_ids"]

# Only these resolutions represent a value we are willing to carry forward as a baseline.
MERGEABLE_RESOLUTIONS = {"Filled empty baseline", "Overrides baseline"}

# Identity findings update the seed's own identity columns rather than a gap-fill attribute.
IDENTITY_MERGE_COLUMNS = {
    "found_facility_name": "site_name",
    "found_latitude": "latitude",
    "found_longitude": "longitude",
}


def mergeable_rows(resolved_rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        row
        for row in resolved_rows
        if normalize_scalar(row.get("resolution")) in MERGEABLE_RESOLUTIONS
        and not is_blank(row.get("resolved_value"))
    ]


def refresh_sites(
    sites: list[dict[str, Any]],
    resolved_rows: list[dict[str, Any]],
    run_id: str,
    merge_identity: bool = False,
) -> tuple[list[dict[str, Any]], dict[str, int]]:
    """Return a new site list with promoted values merged in, plus a summary of what changed."""
    by_site: dict[str, list[dict[str, Any]]] = {}
    for row in mergeable_rows(resolved_rows):
        by_site.setdefault(normalize_scalar(row.get("site_id")), []).append(row)

    stats = {"facilities_updated": 0, "values_merged": 0, "gas_collection_discovered": 0}
    refreshed: list[dict[str, Any]] = []

    for site in sites:
        new_site = dict(site)
        for column in REFRESH_COLUMNS:
            new_site.setdefault(column, "")

        rows = by_site.get(normalize_scalar(site.get("site_id")), [])
        if not rows:
            refreshed.append(new_site)
            continue

        existing = ai_filled_tiers(new_site.get("ai_filled_fields"))
        stamps: list[str] = [
            f"{field}@{label}"
            for field, label in _existing_labels(new_site.get("ai_filled_fields")).items()
        ]
        merged_here = 0

        for row in rows:
            attribute = normalize_scalar(row.get("attribute_name"))
            value = normalize_scalar(row.get("resolved_value"))
            tier_label_text = normalize_scalar(row.get("winning_source_tier"))

            if attribute in GAP_FILL_ATTRIBUTES:
                column = attribute
            elif merge_identity and attribute in IDENTITY_MERGE_COLUMNS:
                column = IDENTITY_MERGE_COLUMNS[attribute]
            else:
                continue

            if attribute == "has_landfill_gas_collection" and value.upper() == "TRUE":
                if normalize_scalar(site.get("has_landfill_gas_collection")).upper() != "TRUE":
                    stats["gas_collection_discovered"] += 1

            new_site[column] = value
            if attribute not in existing:
                stamps.append(f"{attribute}@{tier_label_text}")
            merged_here += 1

        if merged_here:
            stats["facilities_updated"] += 1
            stats["values_merged"] += merged_here
            new_site["ai_filled_fields"] = "; ".join(dict.fromkeys(stamps))
            run_ids = [r for r in normalize_scalar(new_site.get("ai_filled_run_ids")).split(";") if r.strip()]
            if run_id not in [r.strip() for r in run_ids]:
                run_ids.append(run_id)
            new_site["ai_filled_run_ids"] = "; ".join(r.strip() for r in run_ids)

        refreshed.append(new_site)

    return refreshed, stats


def _existing_labels(value: Any) -> dict[str, str]:
    out: dict[str, str] = {}
    for item in normalize_scalar(value).split(";"):
        item = item.strip()
        if "@" in item:
            field, _, label = item.rpartition("@")
            out[field.strip()] = label.strip()
    return out


def refreshed_headers(original_headers: list[str]) -> list[str]:
    headers = list(original_headers)
    for column in REFRESH_COLUMNS:
        if column not in headers:
            headers.append(column)
    return headers


def write_refreshed_seed(
    path: Path,
    sites: list[dict[str, Any]],
    headers: list[str],
) -> Path:
    from .input_loader import write_csv_records

    write_csv_records(path, sites, refreshed_headers(headers))
    return path
