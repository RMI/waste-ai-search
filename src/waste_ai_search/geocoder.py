from __future__ import annotations

import json
from datetime import date
from pathlib import Path
from typing import Any

from .schema import GEOCODE_FIELDS, normalize_scalar


def coord_key(latitude: Any, longitude: Any) -> str:
    try:
        lat = round(float(latitude), 5)
        lon = round(float(longitude), 5)
    except (TypeError, ValueError):
        return ""
    return f"{lat:.5f},{lon:.5f}"


def load_geocode_cache(path: Path | None) -> dict[str, dict[str, Any]]:
    if path is None or not path.exists():
        return {}
    cache: dict[str, dict[str, Any]] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        record = json.loads(line)
        site_id = normalize_scalar(record.get("site_id"))
        key = normalize_scalar(record.get("coord_key"))
        if site_id:
            cache[f"site:{site_id}"] = record
        if key:
            cache[f"coord:{key}"] = record
    return cache


def lookup_geocode(site: dict[str, Any], cache: dict[str, dict[str, Any]]) -> dict[str, Any] | None:
    site_id = normalize_scalar(site.get("site_id"))
    if site_id and f"site:{site_id}" in cache:
        return cache[f"site:{site_id}"]
    key = coord_key(site.get("latitude"), site.get("longitude"))
    if key and f"coord:{key}" in cache:
        return cache[f"coord:{key}"]
    return None


def enrich_sites_from_cache(
    sites: list[dict[str, Any]],
    cache_path: Path | None = None,
    enabled: bool = False,
) -> list[dict[str, Any]]:
    cache = load_geocode_cache(cache_path) if enabled else {}
    enriched: list[dict[str, Any]] = []
    today = date.today().isoformat()
    for site in sites:
        copy = dict(site)
        for field in GEOCODE_FIELDS:
            copy.setdefault(field, "")
        if not enabled:
            copy["geocode_status"] = "disabled"
            enriched.append(copy)
            continue
        record = lookup_geocode(site, cache)
        if not record:
            copy["geocode_status"] = "cache_miss"
            enriched.append(copy)
            continue
        for field in GEOCODE_FIELDS:
            if field in record:
                copy[field] = normalize_scalar(record[field])
        copy["geocode_status"] = "cache_hit"
        copy.setdefault("geocoded_at", today)
        enriched.append(copy)
    return enriched

