"""File each source under the site of ours it is about (build step 5 of the source-matching plan).

Everything the agent returned used to be filed under the site it was asked about, and nothing
checked that a source was about that site. Now each source is matched to one of our sites by where
it says the facility is, with its name as a check, and its values are reviewed under that site.
Pure and offline, like the rest of arbitration.

Our sites are every site in the run's seed, in every country. Distance is to a site's approximate
edge: the distance to its point, less the radius of a circle with its area, the radius capped at
2 km, as entity linkage's precise_v1 measures it (RMI/waste_data_ingestion_pipeline#225). A site
with no area is its point.

A source has a location when the locate step read it as coordinates, a grid reference, or an
address placed on its building or its street. A hint, a location copied from the prompt and a
doubtful one count as none. The same exact page, under the same facility name, is one source
however many searches cited it, and a source with a location is matched once.

| The source has | Rule | Filed under |
|---|---|---|
| A location | 1. Exactly one of our sites within 2 km | That site, whether or not it was searched |
| A location | 2. Two or more within 2 km | The one its name strongly matches, if exactly one does. Otherwise the site searched if it is one of them, else the nearest: a tie, listed in possible_duplicates.csv |
| A location | 3. None within 2 km, but its name strongly matches the site searched within 5 km (25 km where our location is flagged inexact, any distance where we hold none) | The site searched; its location is a coordinate correction |
| A location | 4. Anything else | No site: new_facility_candidates.csv, grouped within 2 km, unless it says the facility is "Not a Waste Facility" |
| No location | 5. Its name strongly matches exactly one other site within 200 km of the site searched, and not the site searched | That site, moved by name |
| No location | 6. Anything else | The site searched, flagged, naming any namesakes within 200 km |

Only sites in the run's selection (run_selection) get values filed under them. A match outside
it is listed in outside_run_matches.csv, since ingestion #261 stops on a facility with values
from two runs.
"""
from __future__ import annotations

import hashlib
import math
import re
import unicodedata
from collections import Counter
from dataclasses import dataclass, field
from functools import lru_cache
from typing import Any

from .credibility import attribute_sources
from .facility_names import distinctive_words, name_similarity
from .locate import MATCHING_BASES, ROOFTOP, STREET, haversine_km, seed_point
from .payload import normalize_url
from .prompt_builder import location_is_inexact
from .schema import DEFAULT_EXCLUDED_ISO3, normalize_scalar
from .searchable_names import NAMED, name_class


# A source within this distance of one of our sites' edges is about that site, if it is the only
# one. Landfills are big, and addresses and coordinates can be off (decision 3: 2 km, not 1.5).
MATCH_KM = 2.0
# A reported area is not always the landfill's own footprint, so past this radius it is not
# trusted to stretch a match (#225's MAX_AREA_RADIUS_METERS).
MAX_AREA_RADIUS_KM = 2.0
# How far a strongly matching name may stretch a match to the site searched (#225's
# strong_name_max_meters), and where our location is flagged inexact, as SINIR's municipality
# centres are (its approximate_location_max_meters).
NAME_RESCUE_KM = 5.0
INEXACT_NAME_RESCUE_KM = 25.0
# How far from the site searched another site of ours may be for a source with no location to be
# moved to it by name, or named as a namesake. In batch 1, a source name fitting another site
# strongly was mostly noise beyond this.
NAMESAKE_KM = 200.0

# A strong name match scores at least this (#225's strong_name), and the two names share at least
# two distinctive words, so the shorter has two or more: one shared word is never strong.
# "Eastview Landfill" is a Brandon and a Guelph site, 1,716 km apart, and "Hallam Road Landfill"
# reads 0.8 like "Ball Road Landfill". A qualifier both names have counts as a shared word, as the
# plan states the rule: in "North Bay", "West Coast" or "Highway 48" it is part of the place's name,
# and the Merrick Landfill's source, "Merrick Landfill - North Bay", is about our "North Bay
# Landfill". #225's shared_words sets qualifiers aside, since it also links on one shared word.
STRONG_NAME_SCORE = 0.8
STRONG_NAME_WORDS = 2

# What became of a source. The first word of each is how the match column starts, which the
# workbook colours by.
LOCATED = "Located"
NAMED_AMONG_SEVERAL = "Named among several"
TIE = "Tie"
NAME_NEARBY = "Name nearby"
NEW_FACILITY = "New facility"
MOVED_BY_NAME = "Moved by name"
NO_LOCATION = "No location"
OUTCOMES = [LOCATED, NAMED_AMONG_SEVERAL, TIE, NAME_NEARBY, NEW_FACILITY, MOVED_BY_NAME, NO_LOCATION]
# Not rules but destinations: a source matched to a site outside the run's selection, and a located
# source that matches none of our sites but calls the facility "Not a Waste Facility". That one
# describes no landfill, so it is no new-facility candidate, and only its verdict goes on, to Leads.
OUTSIDE_RUN = "Outside the run"
NOT_A_FACILITY = "Not a waste facility"

NEW_FACILITY_FILE = "new_facility_candidates.csv"
POSSIBLE_DUPLICATES_FILE = "possible_duplicates.csv"
OUTSIDE_RUN_FILE = "outside_run_matches.csv"

NEW_FACILITY_HEADERS = [
    "candidate_id", "found_by", "country_iso3", "source_urls", "facility_names_in_source",
    "facility_names_english", "stated_locations", "latitude", "longitude", "location_basis",
    "nearest_site_id", "nearest_site_name", "nearest_site_km", "searched_site_ids", "note",
]
POSSIBLE_DUPLICATES_HEADERS = [
    "site_id_a", "site_name_a", "site_id_b", "site_name_b", "distance_km", "source_urls",
    "searched_site_ids", "reason",
]
OUTSIDE_RUN_HEADERS = [
    "source_url", "facility_name_in_source", "facility_name_english", "stated_location",
    "matched_site_id", "matched_site_name", "country_iso3", "searched_site_id", "match",
]
# Who listed a new-facility candidate: arbitration, or an SME's rejection, which
# collect-new-facilities appends and a later arbitration keeps.
FOUND_BY_ARBITRATION = "arbitration"
FOUND_BY_SME = "SME"

# How many sites a match text names before it says "and N more".
MAX_NAMED = 5

BASIS_WORDS = {
    "coordinates": "its coordinates",
    "grid reference": "its grid reference",
    ROOFTOP: "its address",
    STREET: "its address, placed on its street",
}


# --- names -------------------------------------------------------------------------------------
@dataclass(frozen=True)
class NameFit:
    """How well a source's name fits one of ours: a #225 score, or None where either name can't be
    judged, and whether it is strong."""

    score: float | None = None
    strong: bool = False

    @property
    def fits(self) -> bool:
        """At least the strong score, on any number of words: a namesake worth naming."""
        return self.score is not None and self.score >= STRONG_NAME_SCORE


NO_FIT = NameFit()


def shared_words(theirs: str, ours: str) -> int:
    """How many distinctive words two names have in common, a qualifier both have included. Names
    whose qualifiers conflict (North and South) already score 0."""
    return len(distinctive_words(theirs) & distinctive_words(ours))


@lru_cache(maxsize=None)
def compare_names(theirs: str, ours: str) -> NameFit:
    """One source name against one of ours."""
    score = name_similarity(theirs, ours)
    if score is None:
        return NO_FIT
    strong = score >= STRONG_NAME_SCORE and shared_words(theirs, ours) >= STRONG_NAME_WORDS
    return NameFit(score, strong)


def judged_name(name: Any) -> str:
    """One of our names, where it says which facility it is. A placeholder ("OpenStreetMap
    Landfill"), a generic label ("domestic landfill") or something that is not a name can't be
    judged, and is ""."""
    text = normalize_scalar(name)
    return text if text and name_class(text) == NAMED and distinctive_words(text) else ""


# Datasets whose names are addresses looked up from the site's point (Global Plastic Watch's "Emu
# Mountain Road, Doonan, Noosa Shire, Queensland, 4566, Australia"): they say where a site is, not
# which one, and two sites in one district share every word. #225 judges them the same way
# (UNNAMED_SOURCES). 662 of batch 2's 856 sites are named so.
UNNAMED_SOURCES = ("gpw",)


def name_is_an_address(row: dict[str, Any]) -> bool:
    """Whether our name for a site came from a dataset that names a site by its address."""
    source = attribute_sources(row.get("attribute_sources")).get("found_facility_name", "")
    if not source:
        # No per-attribute provenance: a site drawn from that dataset alone has its name.
        contributing = [part.strip() for part in re.split(r"[+;,]", normalize_scalar(row.get("contributing_data_sources")))]
        source = contributing[0] if len(contributing) == 1 else ""
    return source.lower().startswith(UNNAMED_SOURCES)


# --- our sites ---------------------------------------------------------------------------------
@dataclass
class Site:
    """One of our sites, as matching sees it."""

    site_id: str
    name: str
    country: str
    point: tuple[float, float] | None
    radius_km: float
    inexact: bool
    # The names a source is compared with, where they can be judged.
    english: str
    original: str

    @classmethod
    def from_seed(cls, row: dict[str, Any]) -> "Site":
        area = _float(row.get("input_area_square_meters") or row.get("area_square_meters"))
        radius = math.sqrt(area / math.pi) / 1000 if area and area > 0 else 0.0
        # Our English name is site_name, which the seed copies from facility_name.
        english = normalize_scalar(row.get("site_name")) or normalize_scalar(row.get("facility_name"))
        unnamed = name_is_an_address(row)
        return cls(
            site_id=normalize_scalar(row.get("site_id")),
            name=english,
            country=normalize_scalar(row.get("country_iso3")).upper(),
            point=seed_point(row),
            radius_km=min(radius, MAX_AREA_RADIUS_KM),
            inexact=location_is_inexact(row),
            english="" if unnamed else judged_name(english),
            original="" if unnamed else judged_name(row.get("original_site_name")),
        )

    def edge_km(self, point: tuple[float, float]) -> float | None:
        """How far a point is from this site's approximate edge, or None where we have no point."""
        if self.point is None:
            return None
        return max(haversine_km(*self.point, *point) - self.radius_km, 0.0)

    def label(self) -> str:
        return f"{self.name or '(no name)'} ({self.site_id})"


def _float(value: Any) -> float | None:
    try:
        return float(normalize_scalar(value).replace(",", ""))
    except ValueError:
        return None


class SiteIndex:
    """Our sites in half-degree cells, to find the ones near a point without measuring them all."""

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


def run_selection(sites: list[dict[str, Any]], searched: set[str]) -> set[str]:
    """The sites this run may file values under.

    The countries the run searched, minus the default-excluded countries the run did not itself
    search: in the US or Brazil only the sites it searched count, since those countries are left out
    so that web-search values don't reach them. Another batch's country is outside the selection,
    and a site there gets no values from this run.
    """
    countries = {
        normalize_scalar(site.get("country_iso3")).upper()
        for site in sites
        if normalize_scalar(site.get("site_id")) in searched
    }
    selected = set()
    for site in sites:
        site_id = normalize_scalar(site.get("site_id"))
        country = normalize_scalar(site.get("country_iso3")).upper()
        if country not in countries:
            continue
        if country in DEFAULT_EXCLUDED_ISO3 and site_id not in searched:
            continue
        selected.add(site_id)
    return selected


# --- sources -----------------------------------------------------------------------------------
@dataclass
class Citation:
    """One source as one search cited it: the facility it describes, and where it was located."""

    searched_site_id: str
    source_index: int
    url: str = ""
    name_in_source: str = ""
    name_english: str = ""
    stated_location: str = ""
    basis: str = ""
    point: tuple[float, float] | None = None
    # Why a stated location is not a usable point (locate.py), for the "no location" note.
    location_reason: str = ""
    # It calls the site "Not a Waste Facility": never a new-landfill candidate.
    not_a_waste_facility: bool = False

    @property
    def located(self) -> bool:
        return self.point is not None and self.basis in MATCHING_BASES

    @property
    def english(self) -> str:
        """The name compared with our English one. A source that gave no English rendering, as
        responses cached before the source-first contract never do, is compared as it writes it."""
        return self.name_english or self.name_in_source

    @property
    def key(self) -> tuple[str, str, str]:
        """The same page under the same facility name is one source. A page cited with no name, a
        registry say, is told apart by the location it states, so two of its entries stay two."""
        url = normalize_url(self.url) or f"#{self.searched_site_id}-{self.source_index}"
        name = _plain(self.name_in_source or self.name_english)
        return url, name, "" if name else _plain(self.stated_location)


def _plain(text: Any) -> str:
    """Case, accents and punctuation ignored, in any script."""
    folded = unicodedata.normalize("NFKD", normalize_scalar(text).casefold())
    folded = "".join(char for char in folded if not unicodedata.combining(char))
    return " ".join(re.split(r"[\W_]+", folded)).strip()


def name_fit(citation: Citation, site: Site | None) -> NameFit:
    """The better of the source's English name against ours, and its own name against our original
    name where we have one."""
    if site is None:
        return NO_FIT
    fits = []
    if site.english and citation.english:
        fits.append(compare_names(citation.english, site.english))
    if site.original and citation.name_in_source:
        fits.append(compare_names(citation.name_in_source, site.original))
    judged = [fit for fit in fits if fit.score is not None]
    if not judged:
        return NO_FIT
    return NameFit(max(fit.score for fit in judged), any(fit.strong for fit in judged))


@dataclass
class Filing:
    """Where one search's citation of a source is filed, and why."""

    # The site it is filed under; "" for none (a new-facility candidate).
    site_id: str
    outcome: str
    # The rule and its reason, as the SME reads it in the match column.
    match: str
    # The sites an SME might re-file it under, the filed one first, for corrected_site_id.
    choices: list[str] = field(default_factory=list)
    # Whether the site it is filed under is in the run's selection.
    in_run: bool = True


@dataclass
class Matching:
    filings: dict[tuple[str, int], Filing]
    new_facilities: list[dict[str, Any]]
    duplicates: list[dict[str, Any]]
    outside: list[dict[str, Any]]
    # Each outcome, counted once per source with a location and once per search's citation of a
    # source without one; OUTSIDE_RUN counts those filed under a site outside the run.
    counts: Counter


def _km(km: float) -> str:
    return f"{km:.1f} km" if km < 10 else f"{km:,.0f} km"


def _named(sites: list[Site], km: dict[str, float] | None = None) -> str:
    """Sites as "Name (id, 3.1 km)", the first few of them."""
    parts = [
        f"{site.name or '(no name)'} ({site.site_id}{', ' + _km(km[site.site_id]) if km and site.site_id in km else ''})"
        for site in sites[:MAX_NAMED]
    ]
    if len(sites) > MAX_NAMED:
        parts.append(f"and {len(sites) - MAX_NAMED} more")
    return ", ".join(parts)


def _ids(sites: list[Site]) -> str:
    ids = [site.site_id for site in sites[:MAX_NAMED]]
    return ", ".join(ids) + (f" and {len(sites) - MAX_NAMED} more" if len(sites) > MAX_NAMED else "")


class Matcher:
    """Matches a run's sources to our sites. See the module docstring for the rules."""

    def __init__(self, sites: list[dict[str, Any]], selection: set[str]) -> None:
        self.sites = {site.site_id: site for site in (Site.from_seed(row) for row in sites)}
        self.index = SiteIndex(list(self.sites.values()))
        self.selection = selection
        self.counts: Counter = Counter()
        self.filings: dict[tuple[str, int], Filing] = {}
        self.new_sources: list[tuple[Citation, list[Citation]]] = []
        self.duplicates: dict[frozenset, dict[str, Any]] = {}
        self.outside: list[dict[str, Any]] = []
        self._near: dict[str, list[tuple[float, Site]]] = {}

    # --- the two paths ---
    def match(self, citations: list[Citation]) -> Matching:
        by_key: dict[tuple[str, str, str], list[Citation]] = {}
        for citation in citations:
            by_key.setdefault(citation.key, []).append(citation)
        pages: dict[str, set[str]] = {}
        for citation in citations:
            if normalize_url(citation.url):
                pages.setdefault(normalize_url(citation.url), set()).add(citation.searched_site_id)

        for cited in by_key.values():
            located = next((citation for citation in cited if citation.located), None)
            if located is not None:
                self._match_located(located, cited)
            else:
                for citation in cited:
                    others = pages.get(normalize_url(citation.url), set()) - {citation.searched_site_id}
                    self._match_unlocated(citation, sorted(others, key=_id_order))
        self._same_page_duplicates(pages, citations)
        return Matching(
            filings=self.filings,
            new_facilities=self._new_facilities(),
            duplicates=sorted(self.duplicates.values(), key=lambda row: (_id_order(row["site_id_a"]), _id_order(row["site_id_b"]))),
            outside=self.outside,
            counts=self.counts,
        )

    def _match_located(self, source: Citation, cited: list[Citation]) -> None:
        """Rules 1-4, once for the source, whichever searches cited it."""
        point = source.point
        searched_ids = list(dict.fromkeys(citation.searched_site_id for citation in cited))
        how = BASIS_WORDS.get(source.basis, "its location")
        candidates = self.index.within_edge(point, MATCH_KM)
        edge = {site.site_id: km for km, site in candidates}

        if len(candidates) == 1:
            site = candidates[0][1]
            self._file_located(cited, site, LOCATED, f"by {how}, the source places it {_km(edge[site.site_id])} from this site", [site])
            return

        if candidates:
            sites = [site for _km_away, site in candidates]
            strong = [site for site in sites if name_fit(source, site).strong]
            if len(strong) == 1:
                site = strong[0]
                reason = (
                    f"by {how}, the source places it within {MATCH_KM:g} km of {len(sites)} of our sites "
                    f"({_ids(sites)}), and its name matches only this one, {_km(edge[site.site_id])} away"
                )
                self._file_located(cited, site, NAMED_AMONG_SEVERAL, reason, sites)
                return
            # A tie is among the candidates the name fits, where it fits several, else among them all.
            # Every pair of them may be one facility recorded twice.
            tied = strong or sites
            searched = [site for site in tied if site.site_id in searched_ids]
            site = (searched or tied)[0]
            fits = f"fits {len(strong)} of them" if strong else "picks none of them"
            reason = (
                f"by {how}, the source places it within {MATCH_KM:g} km of {len(sites)} of our sites "
                f"({_ids(sites)}), and its name {fits}; filed under "
                f"{'the site searched' if searched else 'the nearest'}, {_km(edge[site.site_id])} away. "
                f"Listed in {POSSIBLE_DUPLICATES_FILE}"
            )
            self._file_located(cited, site, TIE, reason, sites)
            why = (
                f"A source places it within {MATCH_KM:g} km of both, and its name fits both"
                if strong else f"A source places it within {MATCH_KM:g} km of both, and its name picks neither"
            )
            for index, one in enumerate(tied):
                for two in tied[index + 1:]:
                    self._duplicate(one, two, source.url, searched_ids, why)
            return

        rescued = []
        for searched_id in searched_ids:
            site = self.sites.get(searched_id)
            if site is None or not name_fit(source, site).strong:
                continue
            # Where we hold no location for the site searched there is nothing to measure, and the
            # name alone decides.
            km = site.edge_km(point)
            if km is None or km <= (INEXACT_NAME_RESCUE_KM if site.inexact else NAME_RESCUE_KM):
                rescued.append((math.inf if km is None else km, site))
        if rescued:
            km, site = min(rescued, key=lambda item: (item[0], item[1].site_id))
            where = f"{_km(km)} from this site" if km != math.inf else "where we hold no location for this site"
            reason = (
                f"by {how}, the source places it {where}, and no site of ours is within {MATCH_KM:g} km "
                "of it, but its name matches this one. Its location is a coordinate correction"
            )
            self._file_located(cited, site, NAME_NEARBY, reason, [site])
            return

        # A source calling the site "Not a Waste Facility" describes no landfill, so it is never a
        # new-landfill candidate; its verdict goes to Leads.
        listed = not any(citation.not_a_waste_facility for citation in cited)
        outcome = NEW_FACILITY if listed else NOT_A_FACILITY
        where = f"by {how}, the source places it more than {MATCH_KM:g} km from every site of ours"
        match = (
            f"{NEW_FACILITY}: {where}. Listed in {NEW_FACILITY_FILE}." if listed
            else f"{NOT_A_FACILITY}: {where}, and says it is not a waste facility, so it is not listed."
        )
        self.counts[outcome] += 1
        for citation in cited:
            self.filings[(citation.searched_site_id, citation.source_index)] = Filing("", outcome, match, in_run=False)
        if listed:
            self.new_sources.append((source, cited))

    def _match_unlocated(self, citation: Citation, other_searches: list[str]) -> None:
        """Rules 5 and 6, for one search's citation of a source with no location."""
        searched = self.sites.get(citation.searched_site_id)
        own = name_fit(citation, searched)
        namesakes: list[tuple[float, Site, NameFit]] = []
        if searched is not None and searched.point is not None and (citation.english or citation.name_in_source):
            for km, site in self._neighbours(searched):
                if site.site_id == searched.site_id:
                    continue
                fit = name_fit(citation, site)
                if fit.fits:
                    namesakes.append((km, site, fit))
        strong = [(km, site) for km, site, fit in namesakes if fit.strong]
        key = (citation.searched_site_id, citation.source_index)

        if len(strong) == 1 and not own.strong:
            km, site = strong[0]
            match = (
                f"{MOVED_BY_NAME}: the source gives no location, and its name matches this site, "
                f"{_km(km)} from the site searched ({citation.searched_site_id}), and not the site searched."
            )
            self.counts[MOVED_BY_NAME] += 1
            self._file(key, citation, site, MOVED_BY_NAME, match, [site.site_id, citation.searched_site_id])
            return

        if normalize_scalar(citation.stated_location) and citation.location_reason:
            opening = f"the source's location can't be used ({citation.location_reason})"
        else:
            opening = "the source gives no location"
        match = f"{NO_LOCATION}: {opening}, so it stays with the site searched."
        if namesakes:
            km = {site.site_id: km_away for km_away, site, _fit in namesakes}
            match += f" Its name also fits {_named([site for _km_away, site, _fit in namesakes], km)}, within {NAMESAKE_KM:g} km."
        if other_searches:
            listed = ", ".join(other_searches[:MAX_NAMED]) + (
                f" and {len(other_searches) - MAX_NAMED} more" if len(other_searches) > MAX_NAMED else ""
            )
            match += f" The same page was cited for {len(other_searches)} other search(es): {listed}."
        self.counts[NO_LOCATION] += 1
        # The SME may re-file it under a namesake, or under a site whose own search cited the page:
        # batch 1's Prairie Green pages, 283-311 km from Prairie Green, were cited by its search too.
        choices = [citation.searched_site_id, *(site.site_id for _km_away, site, _fit in namesakes), *other_searches]
        if searched is None:
            self.filings[key] = Filing(citation.searched_site_id, NO_LOCATION, match, choices, citation.searched_site_id in self.selection)
            return
        self._file(key, citation, searched, NO_LOCATION, match, choices)

    def _neighbours(self, site: Site) -> list[tuple[float, Site]]:
        """Our sites within NAMESAKE_KM of a site searched, kept, since its sources ask again."""
        if site.site_id not in self._near:
            self._near[site.site_id] = self.index.near(site.point, NAMESAKE_KM)
        return self._near[site.site_id]

    # --- filing ---
    def _file_located(self, cited: list[Citation], site: Site, outcome: str, reason: str, candidates: list[Site]) -> None:
        self.counts[outcome] += 1
        if site.site_id not in self.selection:
            self.counts[OUTSIDE_RUN] += 1
        for citation in cited:
            text = f"{outcome}: {reason}"
            if citation.searched_site_id != site.site_id:
                searched = self.sites.get(citation.searched_site_id)
                km = searched.edge_km(site.point) if searched is not None and site.point is not None else None
                where = f"{_km(km)} from" if km is not None else "away from"
                text += f"; {where} the site searched ({citation.searched_site_id})"
            choices = [site.site_id, *(candidate.site_id for candidate in candidates), citation.searched_site_id]
            self._file((citation.searched_site_id, citation.source_index), citation, site, outcome, text + ".", choices, count=False)

    def _file(
        self, key: tuple[str, int], citation: Citation, site: Site, outcome: str, match: str,
        choices: list[str], count: bool = True,
    ) -> None:
        in_run = site.site_id in self.selection
        if count and not in_run:
            self.counts[OUTSIDE_RUN] += 1
        choices = [choice for choice in dict.fromkeys(choices) if choice in self.selection]
        self.filings[key] = Filing(site.site_id, outcome, match, choices, in_run)
        if not in_run:
            self.outside.append({
                "source_url": citation.url,
                "facility_name_in_source": citation.name_in_source,
                "facility_name_english": citation.name_english,
                "stated_location": citation.stated_location,
                "matched_site_id": site.site_id,
                "matched_site_name": site.name,
                "country_iso3": site.country,
                "searched_site_id": citation.searched_site_id,
                "match": match,
            })

    # --- the lists ---
    def _duplicate(self, one: Site, two: Site, url: str, searched: list[str], reason: str) -> None:
        a, b = sorted([one, two], key=lambda site: _id_order(site.site_id))
        row = self.duplicates.setdefault(frozenset((a.site_id, b.site_id)), {
            "site_id_a": a.site_id, "site_name_a": a.name, "site_id_b": b.site_id, "site_name_b": b.name,
            "distance_km": (
                f"{haversine_km(*a.point, *b.point):.2f}" if a.point is not None and b.point is not None else ""
            ),
            "_urls": set(), "_searched": set(), "_reasons": set(),
        })
        if url:
            row["_urls"].add(url)
        row["_searched"].update(searched)
        row["_reasons"].add(reason)

    def _same_page_duplicates(self, pages: dict[str, set[str]], citations: list[Citation]) -> None:
        """Two of our sites both searched, citing one page, and within 2 km of each other's edges:
        likely one facility recorded twice, as E-PRTR re-registrations are (Cowpen Bewley, Foxhall)."""
        urls = {normalize_url(citation.url): citation.url for citation in citations if normalize_url(citation.url)}
        for page, searched in pages.items():
            sites = sorted((self.sites[sid] for sid in searched if sid in self.sites and self.sites[sid].point), key=lambda s: _id_order(s.site_id))
            for index, one in enumerate(sites):
                for two in sites[index + 1:]:
                    apart = haversine_km(*one.point, *two.point) - one.radius_km - two.radius_km
                    if apart <= MATCH_KM:
                        self._duplicate(one, two, urls[page], [one.site_id, two.site_id], "Both searches cited the same page")

    def _new_facilities(self) -> list[dict[str, Any]]:
        """Located sources that match none of our sites, grouped where they lie within 2 km of
        each other."""
        groups: list[list[tuple[Citation, list[Citation]]]] = []
        for item in self.new_sources:
            point = item[0].point
            joined = [group for group in groups if any(haversine_km(*point, *other[0].point) <= MATCH_KM for other in group)]
            merged = [item]
            for group in joined:
                merged = group + merged
                groups.remove(group)
            groups.append(merged)

        rows = []
        for group in groups:
            sources = [source for source, _cited in group]
            cited = [citation for _source, many in group for citation in many]
            latitude = sum(source.point[0] for source in sources) / len(sources)
            longitude = sum(source.point[1] for source in sources) / len(sources)
            nearest = self.index.nearest((latitude, longitude))
            searched = list(dict.fromkeys(citation.searched_site_id for citation in cited))
            countries = list(dict.fromkeys(
                self.sites[sid].country for sid in searched if sid in self.sites and self.sites[sid].country
            ))
            rows.append({
                "candidate_id": candidate_id(FOUND_BY_ARBITRATION, *sorted(normalize_url(s.url) or s.url for s in sources)),
                "found_by": FOUND_BY_ARBITRATION,
                "country_iso3": "; ".join(countries),
                "source_urls": _joined(source.url for source in sources),
                "facility_names_in_source": _joined(source.name_in_source for source in sources),
                "facility_names_english": _joined(source.name_english for source in sources),
                "stated_locations": _joined(source.stated_location for source in sources),
                "latitude": f"{latitude:.6f}",
                "longitude": f"{longitude:.6f}",
                "location_basis": _joined(source.basis for source in sources),
                "nearest_site_id": nearest[1].site_id if nearest else "",
                "nearest_site_name": nearest[1].name if nearest else "",
                "nearest_site_km": f"{nearest[0]:.1f}" if nearest else "",
                "searched_site_ids": "; ".join(searched),
                "note": "",
            })
        rows.sort(key=lambda row: (row["country_iso3"], row["source_urls"]))
        return rows


def _joined(values) -> str:
    return "; ".join(dict.fromkeys(text for text in (normalize_scalar(value) for value in values) if text))


def _id_order(site_id: str) -> tuple[int, str]:
    return (int(site_id), "") if str(site_id).isdigit() else (0, str(site_id))


def candidate_id(found_by: str, *parts: str) -> str:
    """A new-facility candidate's id, the same each time the same sources are listed."""
    digest = hashlib.sha256("|".join([found_by, *parts]).encode("utf-8")).hexdigest()[:12]
    return f"WAINEW-{digest}"


def finish_duplicates(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """possible_duplicates.csv rows, their sources, searches and reasons joined."""
    out = []
    for row in rows:
        out.append({
            **{key: value for key, value in row.items() if not key.startswith("_")},
            "source_urls": "; ".join(sorted(row["_urls"])),
            "searched_site_ids": "; ".join(sorted(row["_searched"], key=_id_order)),
            "reason": "; ".join(sorted(row["_reasons"])),
        })
    return out


def match_sources(citations: list[Citation], sites: list[dict[str, Any]], selection: set[str]) -> Matching:
    """File every citation under one of our sites, or none, and list what goes to ingestion."""
    matching = Matcher(sites, selection).match(citations)
    matching.duplicates = finish_duplicates(matching.duplicates)
    return matching
