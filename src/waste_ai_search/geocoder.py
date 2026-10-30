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
            # A blank cache field must not erase a value the seed already had, such as SINIR's
            # municipality from the database.
            value = normalize_scalar(record.get(field))
            if value:
                copy[field] = value
        copy["geocode_status"] = "cache_hit"
        # Blank-filled above, so setdefault never fired and a hit without its own date stayed blank.
        if not normalize_scalar(copy.get("geocoded_at")):
            copy["geocoded_at"] = today
        enriched.append(copy)
    return enriched



# Our sites' places, by site id, for the run: the municipality and state each site's prompt named,
# from the seed or the cache, which arbitration reads to show beside each source and compare states.
SITE_PLACES_FILE = "site_places.json"
PLACE_FIELDS = ["municipality", "admin1", "admin2"]


def look_up_places(
    sites: list[dict[str, Any]], cache_path: Path | None, geocoder: Any
) -> list[dict[str, Any]]:
    """Fill each site's municipality and state from where our database puts it, where the seed has
    neither, asking `geocoder` only for a site the cache doesn't have. Every answer goes into the
    cache, so a site is looked up once whatever run it is in. A geocoder that can't look a point up
    (only Nominatim can) leaves the sites as they are.

    The answer names a municipality and a state, never a street, so it says no more of our point
    than the prompt's rounded coordinates do.
    """
    if geocoder is None or not hasattr(geocoder, "reverse") or cache_path is None:
        return sites
    cache = load_geocode_cache(cache_path)
    filled: list[dict[str, Any]] = []
    asked = 0
    for site in sites:
        copy = dict(site)
        if normalize_scalar(copy.get("municipality")) or normalize_scalar(copy.get("admin1")) or lookup_geocode(copy, cache):
            filled.append(copy)
            continue
        key = coord_key(copy.get("latitude"), copy.get("longitude"))
        if not key:
            filled.append(copy)
            continue
        latitude, longitude = (float(part) for part in key.split(","))
        place = geocoder.reverse(latitude, longitude)
        asked += 1
        record = {
            "site_id": normalize_scalar(copy.get("site_id")),
            "coord_key": key,
            **{field: place.get(field, "") for field in PLACE_FIELDS},
            "formatted_address": "",
            "geocoder_provider": geocoder.name,
            "geocoder_confidence": "",
            "geocoded_at": date.today().isoformat(),
        }
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        with cache_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")
        cache[f"site:{record['site_id']}"] = record
        cache[f"coord:{key}"] = record
        filled.append(copy)
    if asked:
        print(f"Places: looked up {asked} site(s)' municipality and state -> {cache_path}")
    return filled


def save_site_places(run_dir: Path, sites: list[dict[str, Any]]) -> None:
    """Add these sites' places to the run's site_places.json, keeping those of sites searched before."""
    path = run_dir / SITE_PLACES_FILE
    places = load_site_places(run_dir)
    for site in sites:
        place = {field: normalize_scalar(site.get(field)) for field in PLACE_FIELDS}
        if any(place.values()):
            places[normalize_scalar(site.get("site_id"))] = place
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(dict(sorted(places.items())), indent=2, ensure_ascii=False), encoding="utf-8")


def load_site_places(run_dir: Path) -> dict[str, dict[str, str]]:
    path = run_dir / SITE_PLACES_FILE
    if not path.exists():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}
    return payload if isinstance(payload, dict) else {}


def with_places(sites: list[dict[str, Any]], run_dir: Path) -> list[dict[str, Any]]:
    """The run's sites with the places their prompts named (site_places.json), where the seed has
    none of its own."""
    places = load_site_places(run_dir)
    out = []
    for site in sites:
        place = places.get(normalize_scalar(site.get("site_id"))) or {}
        copy = dict(site)
        for field in PLACE_FIELDS:
            if not normalize_scalar(copy.get(field)) and place.get(field):
                copy[field] = place[field]
        out.append(copy)
    return out


def place_text(site: dict[str, Any]) -> str:
    """Our site's place as one line: "San Sebastián Tecomaxtlahuaca, Oaxaca"."""
    return ", ".join(dict.fromkeys(
        part for part in (normalize_scalar(site.get("municipality")), normalize_scalar(site.get("admin1"))) if part
    ))
