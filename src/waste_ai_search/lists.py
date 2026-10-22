"""The lists arbitration writes for ingestion beside found_values.csv.

Every source is filed under the site that was searched (step 2 of the approve-sources plan), so
what is left for ingestion is three lists:

- possible_duplicates.csv: pairs of our sites cited under the same page and the same facility
  name, which may be one facility recorded twice;
- datasets_found.csv: pages that look like datasets, better loaded once through ingestion than
  found again by every search that needs one of their rows;
- new_facility_candidates.csv: landfills none of our sites is, which only an SME's New site
  decision adds now.

Pure and offline, like the rest of arbitration.
"""
from __future__ import annotations

import hashlib
import math
import re
import unicodedata
from dataclasses import dataclass
from typing import Any

from .facility_names import distinctive_words
from .locate import MATCHING_BASES, haversine_km, seed_point
from .payload import normalize_url
from .schema import normalize_scalar


# Within this distance two points a page gives are one facility: landfills are big, and
# addresses and coordinates can be off.
SAME_PLACE_KM = 2.0
# A reported area is not always the landfill's own footprint, so past this radius it is not
# trusted to stretch a site's edge (#225's MAX_AREA_RADIUS_METERS).
MAX_AREA_RADIUS_KM = 2.0

NEW_FACILITY_FILE = "new_facility_candidates.csv"
POSSIBLE_DUPLICATES_FILE = "possible_duplicates.csv"
DATASETS_FOUND_FILE = "datasets_found.csv"

NEW_FACILITY_HEADERS = [
    "candidate_id", "found_by", "country_iso3", "source_urls", "facility_names_in_source",
    "facility_names_english", "stated_locations", "latitude", "longitude", "location_basis",
    "nearest_site_id", "nearest_site_name", "nearest_site_km", "searched_site_ids", "note",
]
POSSIBLE_DUPLICATES_HEADERS = [
    "site_id_a", "site_name_a", "site_id_b", "site_name_b", "distance_km", "source_urls",
    "facility_names_in_source", "searched_site_ids", "reason",
]
DATASETS_FOUND_HEADERS = [
    "source_url", "source_title", "publisher", "source_type", "source_tier", "publication_date",
    "searches", "facilities_named", "facilities_placed", "searched_site_ids",
    "facility_names_in_source", "why",
]
# A page that several searches found, or that names or places several facilities, is probably a
# dataset: a regulator's list of its landfills, say. A search reports only the entry it used, so
# the rest of such a page goes unread, and every site on it pays its own search to find it again.
# It is better loaded once, through ingestion, as a source of its own, as E-PRTR is.
DATASET_MIN_SEARCHES = 3
DATASET_MIN_FACILITIES = 2
# Being found by several searches marks a dataset only from a publisher of them. A town's landfill
# page is found by every nearby search too: batch 1's Brandon page by fourteen.
DATASET_SOURCE_TYPES = {"regulator", "permit or registry", "government statistics", "intergovernmental dataset"}
# How many names a datasets_found row lists before it says "and N more".
MAX_DATASET_NAMES = 12

# Who listed a new-facility candidate. Arbitration once listed the located sources that matched
# none of our sites; since the matcher went (step 2 of the approve-sources plan) only an SME does,
# and a later arbitration keeps what they listed.
FOUND_BY_ARBITRATION = "arbitration"
FOUND_BY_SME = "SME"


def plain(text: Any) -> str:
    """Case, accents and punctuation ignored, in any script."""
    folded = unicodedata.normalize("NFKD", normalize_scalar(text).casefold())
    folded = "".join(char for char in folded if not unicodedata.combining(char))
    return " ".join(re.split(r"[\W_]+", folded)).strip()


def id_order(site_id: str) -> tuple[int, str]:
    """Site ids in number order where they are numbers."""
    return (int(site_id), "") if str(site_id).isdigit() else (0, str(site_id))


def candidate_id(found_by: str, *parts: str) -> str:
    """A new-facility candidate's id, the same each time the same sources are listed."""
    digest = hashlib.sha256("|".join([found_by, *parts]).encode("utf-8")).hexdigest()[:12]
    return f"WAINEW-{digest}"


# --- our sites, to name the one nearest a new-facility candidate --------------------------------
@dataclass
class Site:
    """One of our sites, as the nearest-site search sees it."""

    site_id: str
    name: str
    point: tuple[float, float] | None
    radius_km: float

    @classmethod
    def from_seed(cls, row: dict[str, Any]) -> "Site":
        area = _float(row.get("input_area_square_meters") or row.get("area_square_meters"))
        radius = math.sqrt(area / math.pi) / 1000 if area and area > 0 else 0.0
        return cls(
            site_id=normalize_scalar(row.get("site_id")),
            name=normalize_scalar(row.get("site_name")) or normalize_scalar(row.get("facility_name")),
            point=seed_point(row),
            radius_km=min(radius, MAX_AREA_RADIUS_KM),
        )


def _float(value: Any) -> float | None:
    try:
        return float(normalize_scalar(value).replace(",", ""))
    except ValueError:
        return None


class SiteIndex:
    """Our sites in half-degree cells, to find the ones near a point without measuring them all.

    Distance is to a site's approximate edge: the distance to its point, less the radius of a
    circle with its area, the radius capped at 2 km, as entity linkage's precise_v1 measures it
    (RMI/waste_data_ingestion_pipeline#225). A site with no area is its point.
    """

    CELL_DEGREES = 0.5

    def __init__(self, sites: list[Site]) -> None:
        self.cells: dict[tuple[int, int], list[Site]] = {}
        self.columns = round(360 / self.CELL_DEGREES)
        for site in sites:
            if site.point is not None:
                self.cells.setdefault(self._cell(*site.point), []).append(site)

    def _cell(self, latitude: float, longitude: float) -> tuple[int, int]:
        return (
            math.floor(latitude / self.CELL_DEGREES),
            math.floor((longitude + 180) / self.CELL_DEGREES) % self.columns,
        )

    def near(self, point: tuple[float, float], km: float) -> list[tuple[float, Site]]:
        """Every site whose point is within `km` of `point`, nearest first, with its distance."""
        latitude, longitude = point
        d_lat = km / 110.5
        cos = math.cos(math.radians(min(abs(latitude) + d_lat, 89.9)))
        d_lon = 180.0 if cos <= 0 else min(km / (111.32 * cos), 180.0)
        rows = range(math.floor((latitude - d_lat) / self.CELL_DEGREES), math.floor((latitude + d_lat) / self.CELL_DEGREES) + 1)
        first = math.floor((longitude - d_lon + 180) / self.CELL_DEGREES)
        last = math.floor((longitude + d_lon + 180) / self.CELL_DEGREES)
        columns = range(self.columns) if last - first + 1 >= self.columns else {c % self.columns for c in range(first, last + 1)}
        found = []
        for row in rows:
            for column in columns:
                for site in self.cells.get((row, column), ()):
                    km_away = haversine_km(*point, *site.point)
                    if km_away <= km:
                        found.append((km_away, site))
        found.sort(key=lambda item: (item[0], item[1].site_id))
        return found

    def within_edge(self, point: tuple[float, float], km: float) -> list[tuple[float, Site]]:
        """Every site whose approximate edge is within `km` of `point`, nearest edge first."""
        found = [
            (max(km_away - site.radius_km, 0.0), site)
            for km_away, site in self.near(point, km + MAX_AREA_RADIUS_KM)
        ]
        found = [(edge, site) for edge, site in found if edge <= km]
        found.sort(key=lambda item: (item[0], item[1].site_id))
        return found

    def nearest(self, point: tuple[float, float]) -> tuple[float, Site] | None:
        """The site with the nearest edge, looking ever wider, out to 1,000 km."""
        for km in (5.0, 25.0, 100.0, 1000.0):
            found = self.within_edge(point, km)
            if found:
                return found[0]
        return None


# --- possible duplicates -------------------------------------------------------------------------
def possible_duplicates(
    shared: dict[tuple[str, str], list[dict[str, Any]]], sites_by_id: dict[str, dict[str, Any]]
) -> list[dict[str, Any]]:
    """possible_duplicates.csv: every two of our sites cited under the same page and facility name.

    `shared` maps each (page, name) cited for two or more of our sites to those citations. The agent
    returns only sources about the site searched, so a page and name cited for two sites says they
    may be one facility recorded twice, as E-PRTR re-registrations are (Cowpen Bewley, Foxhall), or
    that one search claimed another site's source, as batch 1's Brandon page under 14 sites was.
    """
    rows: dict[tuple[str, str], dict[str, Any]] = {}
    for cited in shared.values():
        site_ids = sorted({normalize_scalar(citation.get("site_id")) for citation in cited}, key=id_order)
        for index, one in enumerate(site_ids):
            for two in site_ids[index + 1:]:
                a, b = sites_by_id.get(one, {}), sites_by_id.get(two, {})
                points = seed_point(a), seed_point(b)
                row = rows.setdefault((one, two), {
                    "site_id_a": one,
                    "site_name_a": normalize_scalar(a.get("site_name")),
                    "site_id_b": two,
                    "site_name_b": normalize_scalar(b.get("site_name")),
                    "distance_km": f"{haversine_km(*points[0], *points[1]):.2f}" if None not in points else "",
                    "_urls": set(), "_names": set(),
                })
                for citation in cited:
                    row["_urls"].add(normalize_scalar(citation.get("url")))
                    name = normalize_scalar(citation.get("facility_name_in_source") or citation.get("facility_name_english"))
                    if name:
                        row["_names"].add(name)
    out = []
    for (one, two), row in sorted(rows.items(), key=lambda item: (id_order(item[0][0]), id_order(item[0][1]))):
        out.append({
            **{key: value for key, value in row.items() if not key.startswith("_")},
            "source_urls": "; ".join(sorted(url for url in row["_urls"] if url)),
            "facility_names_in_source": "; ".join(sorted(row["_names"])),
            "searched_site_ids": f"{one}; {two}",
            "reason": "Both sites' searches cited the same page under the same facility name",
        })
    return out


# --- datasets ------------------------------------------------------------------------------------
def datasets_found(citations: list[dict[str, Any]], sources: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Pages that look like datasets, for ingestion to load once: naming or placing
    DATASET_MIN_FACILITIES or more facilities, or, from a publisher of datasets, found by
    DATASET_MIN_SEARCHES or more searches.

    Each of `citations` is one search's citation of a page, with the facility it names and where it
    places it. Names are told apart by their distinctive words, so "Solid Waste & Recycling
    Facility" and "Solid Waste and Recycling Facility" are one. A facility a page places is a point
    its citations give, from coordinates, a grid reference or an address placed on its building or
    street; points within SAME_PLACE_KM of each other are one facility.
    """
    by_url = {normalize_scalar(row.get("url_normalized")): row for row in sources}
    by_page: dict[str, list[dict[str, Any]]] = {}
    for citation in citations:
        url = normalize_url(citation.get("url"))
        if url:
            by_page.setdefault(url, []).append(citation)

    rows: list[dict[str, Any]] = []
    for url, cited in by_page.items():
        searches = sorted({normalize_scalar(citation.get("site_id")) for citation in cited}, key=id_order)
        names = list(dict.fromkeys(
            name for name in (
                normalize_scalar(citation.get("facility_name_in_source") or citation.get("facility_name_english"))
                for citation in cited
            ) if plain(name)
        ))
        named = len({frozenset(distinctive_words(name)) or plain(name) for name in names})
        places: list[tuple[float, float]] = []
        for citation in cited:
            point = citation_point(citation)
            if point is not None and not any(haversine_km(*point, *other) <= SAME_PLACE_KM for other in places):
                places.append(point)
        source = by_url.get(url, {})
        why = []
        publishes = normalize_scalar(source.get("source_type")).lower() in DATASET_SOURCE_TYPES
        if len(searches) >= DATASET_MIN_SEARCHES and publishes:
            why.append(f"found by {len(searches)} searches")
        if named >= DATASET_MIN_FACILITIES:
            why.append(f"names {named} facilities")
        if len(places) >= DATASET_MIN_FACILITIES:
            why.append(f"places {len(places)} facilities")
        if not why:
            continue
        shown = names[:MAX_DATASET_NAMES]
        more = len(names) - len(shown)
        rows.append({
            "source_url": normalize_scalar(source.get("url")) or normalize_scalar(cited[0].get("url")),
            "source_title": normalize_scalar(source.get("source_title")),
            "publisher": normalize_scalar(source.get("publisher")),
            "source_type": normalize_scalar(source.get("source_type")),
            "source_tier": normalize_scalar(source.get("source_tier")),
            "publication_date": normalize_scalar(source.get("publication_date")),
            "searches": len(searches),
            "facilities_named": named,
            "facilities_placed": len(places),
            "searched_site_ids": "; ".join(searches),
            "facility_names_in_source": "; ".join(shown) + (f"; and {more} more" if more else ""),
            "why": "; ".join(why),
        })
    rows.sort(key=lambda row: (-row["searches"], -max(row["facilities_named"], row["facilities_placed"]), row["source_url"]))
    return rows


def citation_point(citation: dict[str, Any]) -> tuple[float, float] | None:
    """Where a citation places the facility, from a basis good enough to tell two facilities apart:
    coordinates, a grid reference, or an address placed on its building or street."""
    if normalize_scalar(citation.get("location_basis")) not in MATCHING_BASES:
        return None
    latitude, longitude = _float(citation.get("located_latitude")), _float(citation.get("located_longitude"))
    return None if latitude is None or longitude is None else (latitude, longitude)
