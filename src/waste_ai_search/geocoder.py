from __future__ import annotations

import bisect
import json
import math
import unicodedata
from collections import defaultdict
from datetime import date
from pathlib import Path
from typing import Any

from .locate import haversine_km
from .schema import GEOCODE_FIELDS, normalize_scalar
from .searchable_names import NAMED, site_name_class


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
    sites: list[dict[str, Any]], cache_path: Path | None, geocoder: Any, which: str = "site(s)"
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
        print(f"Places: looked up the municipality and state of {asked} {which} -> {cache_path}")
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


# --- our other sites near each site searched -----------------------------------------------------
# Named in its prompt, so the agent can tell them apart: a page about one of them is not about the
# site searched unless it describes that one too (Hugh, 30 October). A site is near if it is in the
# same country and either within NEAR_KM or, within SAME_MUNICIPALITY_KM, in the same municipality
# and state as the geocoder names them. The names alone miss some: the geocoder names a site in a
# town by the town ("Texcoco de Mora") and one outside it by its municipio ("Texcoco").
OTHER_SITES_FIELD = "our_other_sites_nearby"
NEAR_KM = 15.0
# Our records under this far away are named apart, as possibly the site searched recorded twice:
# Mexico's INEGI rows hold some dumps twice at the same point, and the prompt gives our point only
# to about 1 km, so the agent can't tell. A page about one of them may then be cited for both, which
# is how possible_duplicates.csv finds a site recorded twice.
SAME_POINT_KM = 1.0
SAME_POINT_FIELD = "our_records_within_1_km"
# How far a same-named municipality is looked for: wider than all but the largest municipios, so a
# site farther away is named only if it is within NEAR_KM. Only our sites this close to a site
# searched have their places looked up, so a 10-site test doesn't geocode a whole country first
# (Nominatim answers once a second): 236 sites for the Mexico sample, against 841 within 150 km.
SAME_MUNICIPALITY_KM = 60.0
# A city can hold dozens of our sites: the nearest are named and the rest counted.
MAX_OTHER_SITES = 10
# haversine_km's kilometres in a degree of latitude.
KM_PER_DEGREE = 6371.0088 * math.pi / 180


def _point(site: dict[str, Any]) -> tuple[float, float] | None:
    try:
        latitude, longitude = float(site.get("latitude")), float(site.get("longitude"))
    except (TypeError, ValueError):
        return None
    if not (math.isfinite(latitude) and math.isfinite(longitude)):
        return None
    return latitude, longitude


def _plain(text: Any) -> str:
    """For comparing place names: no case, accents or extra spaces."""
    decomposed = unicodedata.normalize("NFKD", normalize_scalar(text))
    return " ".join("".join(c for c in decomposed if not unicodedata.combining(c)).casefold().split())


def _how_far(km: float) -> str:
    return "under 1 km away" if km < 1 else f"{km:.0f} km away"


def _named(other: dict[str, Any], town: str, km: float) -> str:
    """One of our other sites as the prompt names it. A generic label such as "BASURERO MUNICIPAL"
    is shown as one, so the agent doesn't take a page about "el basurero municipal" for that site's."""
    where = ", ".join(part for part in (town, _how_far(km)) if part)
    label = normalize_scalar(other.get("original_site_name")) or normalize_scalar(other.get("site_name"))
    if not label:
        return f"unnamed site ({where})"
    if site_name_class(other) != NAMED:
        return f'unnamed site, recorded as "{label}" ({where})'
    return f"{label} ({where})"


def with_other_sites(
    selected: list[dict[str, Any]], sites: list[dict[str, Any]], cache_path: Path | None, geocoder: Any
) -> list[dict[str, Any]]:
    """The selected sites, each with our other sites near it, nearest first, as "NAME (TOWN, N km
    away)": those under SAME_POINT_KM in SAME_POINT_FIELD, the rest in OTHER_SITES_FIELD. A site with
    none has neither field.

    `sites` is the whole seed, so a site the run doesn't search, such as one skipped for its generic
    name, is named too. Places come from the seed or the cache. The geocoder is asked only for our
    sites within SAME_MUNICIPALITY_KM of a site searched whose place the cache doesn't have.
    """
    band = SAME_MUNICIPALITY_KM / KM_PER_DEGREE  # in degrees of latitude
    by_country: dict[str, list[tuple[float, float, dict[str, Any]]]] = defaultdict(list)
    for site in sites:
        point = _point(site)
        if point is not None:
            by_country[normalize_scalar(site.get("country_iso3")).upper()].append((*point, site))
    for rows in by_country.values():
        rows.sort(key=lambda row: row[0])
    latitudes = {iso3: [row[0] for row in rows] for iso3, rows in by_country.items()}

    selected_ids = {normalize_scalar(site.get("site_id")) for site in selected}
    near: dict[str, list[tuple[float, dict[str, Any]]]] = {}
    unplaced: dict[str, dict[str, Any]] = {}
    for site in selected:
        site_id, point = normalize_scalar(site.get("site_id")), _point(site)
        if point is None:
            continue
        iso3 = normalize_scalar(site.get("country_iso3")).upper()
        rows, lats = by_country.get(iso3, []), latitudes.get(iso3, [])
        start, end = bisect.bisect_left(lats, point[0] - band), bisect.bisect_right(lats, point[0] + band)
        found = []
        for latitude, longitude, other in rows[start:end]:
            other_id = normalize_scalar(other.get("site_id"))
            if other_id == site_id:
                continue
            km = haversine_km(point[0], point[1], latitude, longitude)
            if km <= SAME_MUNICIPALITY_KM:
                found.append((km, other))
                if other_id not in selected_ids:
                    unplaced[other_id] = other
        near[site_id] = sorted(found, key=lambda item: item[0])

    others = list(unplaced.values())
    if others:
        others = look_up_places(others, cache_path, geocoder, which="of our other sites near them")
        others = enrich_sites_from_cache(others, cache_path=cache_path, enabled=cache_path is not None)
    place_of = {
        normalize_scalar(site.get("site_id")): (
            _plain(site.get("municipality")), _plain(site.get("admin1")), normalize_scalar(site.get("municipality")),
        )
        for site in [*others, *selected]
    }

    out = []
    for site in selected:
        copy = dict(site)
        town, state, _shown = place_of.get(normalize_scalar(site.get("site_id")), ("", "", ""))
        lists: dict[str, list[str]] = {SAME_POINT_FIELD: [], OTHER_SITES_FIELD: []}
        for km, other in near.get(normalize_scalar(site.get("site_id")), []):
            other_town, other_state, shown = place_of.get(normalize_scalar(other.get("site_id")), ("", "", ""))
            if km <= NEAR_KM or (town and state and (other_town, other_state) == (town, state)):
                lists[SAME_POINT_FIELD if km < SAME_POINT_KM else OTHER_SITES_FIELD].append(_named(other, shown, km))
        for field, named in lists.items():
            if len(named) > MAX_OTHER_SITES:
                named = [*named[:MAX_OTHER_SITES], f"and {len(named) - MAX_OTHER_SITES} more"]
            if named:
                copy[field] = named
        out.append(copy)
    return out
