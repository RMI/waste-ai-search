"""Whether a site's name gives a web search anything to look for.

The agent finds a facility by its name and location. A name that is filler, stray text from
another map tag, or only a kind of site gives it nothing to search for. Six classes:

- no name: empty, or the filler the ingestion pipeline gives an OpenStreetMap polygon with no name
  tag (`transform_osm.py` in RMI/waste_data_ingestion_pipeline);
- not a name: a URL, a bare number, or a survey note, which reach the name when a polygon has no
  name tag and the pipeline falls back to its note or description tags;
- generic label: says what kind of site it is, not which one (`is_generic_site_name`);
- mining feature: a mining spoil heap or similar, not a waste site, so there is nothing to find
  about it as one;
- address only: a site only Global Plastic Watch records. It maps sites from satellite images and
  names none of them; the name we hold is an address worked out from the point ("Nông Cống
  District, Thanh Hoa province, Vietnam"), so the search would have only a place to go on;
- named: everything else.

Site selection skips the first five classes (`select_sites` in search.py), and
scripts/measure_name_yield.py measures what searching each class found, so the rule and the
measurement sort names the same way.
"""
from __future__ import annotations

import csv
import re
from collections import Counter
from pathlib import Path
from typing import Any

from .input_loader import write_csv_records
from .pilot_selector import GENERIC_EXACT_NAMES, is_generic_site_name
from .run_context import tool_root
from .schema import NON_MUNICIPAL_LANDFILL, NOT_A_WASTE_FACILITY, normalize_scalar


NO_NAME = "no name"
NOT_A_NAME = "not a name"
GENERIC = "generic label"
MINING = "mining feature"
ADDRESS = "address only"
NAMED = "named"
# Not a name class: a site we already know isn't a municipal waste site, whatever it's called.
# Skipped by skip_not_municipal and listed in skipped_sites.csv beside the name classes.
NOT_MUNICIPAL = "not municipal"
NAME_CLASSES = [NO_NAME, NOT_A_NAME, GENERIC, MINING, ADDRESS, NOT_MUNICIPAL, NAMED]
# Not searched by default (Hugh, 20 October 2026; address only, 4 November 2026): none of them
# gives a search anything to look for. --site-ids and --include-unsearchable-names search them anyway.
SKIPPED_NAME_CLASSES = [NO_NAME, NOT_A_NAME, GENERIC, MINING, ADDRESS]

# Global Plastic Watch's dataset name in contributing_data_sources. 1,716 of the 19,492 seed sites
# come from it alone; the 4 it shares with another source take that source's name.
ADDRESS_ONLY_SOURCES = {"gpw_2021"}

# The run's list of sites skipped by name, in the run folder. Every search that doesn't list its
# site ids adds to it (record_skips), so arbitration can record in Run_Config what the run skipped.
SKIPPED_SITES_FILE = "skipped_sites.csv"
SKIPPED_SITES_HEADERS = ["site_id", "site_name", "original_site_name", "country_iso3", "name_class", "reason"]

PLACEHOLDER_NAMES = {"openstreetmap landfill"}
# A name that says the site has none, as 4 Mexican sites are named.
NO_NAME_PHRASES = {"no name", "noname", "unnamed", "sin nombre"}
# Text from OSM's fixme-style tags, e.g. "update the tag landuse after survey", and a mapper's notes
# to themselves, as Russian OSM names hold ("clarify what exactly it is", "Not valid").
SURVEY_NOTE_PHRASES = (
    "update the tag", "after survey", "fixme",
    "clarify", "looks like", "not valid", "incomprehensible", "no longer in use", "what is being",
)
# One letter ("L", from л).
SINGLE_CHARACTER = re.compile(r"^\w$")
# A numbered part of some site, never which site: "Map 4" (4-я карта), "Section No1" (секция №1).
NUMBERED_PART = re.compile(r"^(?:map|section|cell|card)\s*(?:no\.?|№|#)?\s*\d+$", re.IGNORECASE)
# A street, which says where a point is and names no site: "проспект Тракторостроителей".
STREET_WORDS = re.compile(r"(проспект|шоссе|улица|переулок|\bул\.)", re.IGNORECASE)
URL = re.compile(r"(https?://|www\.)", re.IGNORECASE)
# Digits and separators only, e.g. "4000" or "12/3".
NUMBER = re.compile(r"^[\d\s.,;:/#№-]+$")
# Mining spoil rather than waste. Machine translation turns Russian отвал (spoil heap) into "Blade"
# and leaves террикон (spoil tip) as "Terrikon", so the source's own spelling is checked too.
# "Quarry" and "mine" are left out on purpose: many real landfills sit in old quarries and are
# named after them. So is "tailings" alone: Brazil's rejeitos, the household waste left after
# sorting, is translated "tailings", and "Tailings Landfill" there is a municipal landfill.
MINING_WORDS = re.compile(
    r"\b(blades?|terrikons?|overburden|spoil|slag|gangue|waste rock|tailings (?:ponds?|dams?|storage))\b",
    re.IGNORECASE,
)
# German Halde (a mining heap: "Halde 311", "Eduardschachthalde", but not a Müllhalde, a rubbish
# heap) and its potash tips ("Kaliberg",
# "Kalimandscharo"), and Turkish pasa (mine waste, which machine translation makes "Pasha").
MINING_ORIGINAL_WORDS = re.compile(
    r"(отвал|террикон|терикон|відвал|хвостохранилищ|хвостосховищ|(?<!müll)halde\b|^kaliberg$|^kalima[n]?d?scharo$|^pasa$|erzhalde)",
    re.IGNORECASE,
)


def name_reason(name: Any, original_name: Any = "") -> tuple[str, str]:
    """(class, the rule that put it there): one of NAME_CLASSES, and "" for a named site.

    `name` is the English name the search sees; `original_name` the source's own spelling, where
    it differs. The mining check reads both. Where the English name is empty or filler, a source
    spelling that is itself named keeps the site named: the translation lost a name the source
    recorded, and the agent searches that spelling first. The generic check reads only the English
    name. It knows English and Spanish words, and the German and Russian ones a name that reached us
    untranslated uses ("ehem. Deponie", "Бывшая свалка"); an original's own words, such as Turkish
    ones, are left to its translation.
    """
    text = normalize_scalar(name)
    lowered = text.casefold()
    if not text or lowered in PLACEHOLDER_NAMES or lowered in NO_NAME_PHRASES:
        original = normalize_scalar(original_name)
        # The original is checked as a name of its own; one the same as the name adds nothing.
        if original and original.casefold() != lowered and name_class(original, original) == NAMED:
            return NAMED, ""
    if not text:
        return NO_NAME, "empty name"
    if lowered in PLACEHOLDER_NAMES:
        return NO_NAME, "ingestion's filler for an unnamed OSM polygon"
    if lowered in NO_NAME_PHRASES:
        return NO_NAME, "says it has no name"
    if URL.search(text):
        return NOT_A_NAME, "a URL"
    if NUMBER.match(text):
        return NOT_A_NAME, "a bare number"
    if any(phrase in lowered for phrase in SURVEY_NOTE_PHRASES) or "?" in text:
        return NOT_A_NAME, "a survey note"
    if SINGLE_CHARACTER.match(text):
        return NOT_A_NAME, "a single letter"
    if NUMBERED_PART.match(text):
        return NOT_A_NAME, "a numbered part of a site, such as Map 4"
    if STREET_WORDS.search(text):
        return NOT_A_NAME, "a street"
    if MINING_WORDS.search(text):
        return MINING, "a mining word (blade, terrikon, overburden, spoil, slag, tailings pond)"
    if MINING_ORIGINAL_WORDS.search(normalize_scalar(original_name)) or MINING_ORIGINAL_WORDS.search(text):
        return MINING, "a mining word in the original name (отвал, террикон, відвал, Halde, pasa)"
    if is_generic_site_name(text):
        if text.lower().strip() in GENERIC_EXACT_NAMES:
            return GENERIC, "a listed generic phrase"
        return GENERIC, "only words for a kind of site, such as landfill, dump, basurero, relleno sanitario, municipal"
    return NAMED, ""


def name_class(name: Any, original_name: Any = "") -> str:
    """Which of NAME_CLASSES a site's name falls in."""
    return name_reason(name, original_name)[0]


def site_name_reason(site: dict[str, Any]) -> tuple[str, str]:
    """`name_reason` for a seed row, which also knows the datasets behind it: a site only Global
    Plastic Watch records is address only, whatever its address says."""
    sources = {part.strip() for part in normalize_scalar(site.get("contributing_data_sources")).split("+")}
    if sources and sources <= ADDRESS_ONLY_SOURCES:
        return ADDRESS, "only Global Plastic Watch records it; its name is an address worked out from the point"
    return name_reason(site.get("site_name"), site.get("original_site_name"))


def site_name_class(site: dict[str, Any]) -> str:
    """Which of NAME_CLASSES a seed row's name falls in."""
    return site_name_reason(site)[0]


def skip_unsearchable_names(sites: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """(the sites to search, a skipped_sites.csv row for each site whose name is not searched)."""
    kept, skipped = [], []
    for site in sites:
        cls, reason = site_name_reason(site)
        if cls not in SKIPPED_NAME_CLASSES:
            kept.append(site)
            continue
        skipped.append({
            **{
                key: normalize_scalar(site.get(key))
                for key in ("site_id", "site_name", "original_site_name", "country_iso3")
            },
            "name_class": cls,
            "reason": reason,
        })
    return kept, skipped


# Sites a person has checked and found aren't municipal waste sites (mine and slag dumps, sludge
# fields, coal yards), by WasteMAP ID. Read from ingestion's manual entries
# (manual_entry_facilities.csv on RMI/waste_data_ingestion_pipeline#379) before the database is
# rebuilt with them; once it is, the seed's own facility_type says the same.
NOT_MUNICIPAL_SITES_FILE = tool_root() / "data" / "not_municipal_sites.csv"
NOT_MUNICIPAL_TYPES = {NON_MUNICIPAL_LANDFILL, NOT_A_WASTE_FACILITY}


def reviewed_not_municipal(path: Path = NOT_MUNICIPAL_SITES_FILE) -> dict[str, str]:
    """{WasteMAP ID: the type a reviewer gave it} for each site in the hand-reviewed list."""
    if not path.exists():
        return {}
    with path.open(newline="", encoding="utf-8-sig") as handle:
        return {normalize_scalar(row["wastemap_id"]): normalize_scalar(row["facility_type"]) for row in csv.DictReader(handle)}


def skip_not_municipal(
    sites: list[dict[str, Any]], reviewed: dict[str, str] | None = None
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """(the sites to search, a skipped_sites.csv row for each site that isn't a municipal waste
    site): our seed types it a Non-Municipal Waste Landfill or Not a Waste Facility, or a reviewer
    did (data/not_municipal_sites.csv). A municipal-waste model leaves these sites out, so nothing
    the search finds about them is used, and searching a mine dump can only load a municipal value
    onto it."""
    reviewed = reviewed_not_municipal() if reviewed is None else reviewed
    kept, skipped = [], []
    for site in sites:
        site_id = normalize_scalar(site.get("site_id"))
        ours = normalize_scalar(site.get("facility_type"))
        if ours in NOT_MUNICIPAL_TYPES:
            reason = f"our seed types it {ours}"
        elif site_id in reviewed:
            reason = f"reviewed as {reviewed[site_id]} (data/not_municipal_sites.csv)"
        else:
            kept.append(site)
            continue
        skipped.append({
            **{
                key: normalize_scalar(site.get(key))
                for key in ("site_id", "site_name", "original_site_name", "country_iso3")
            },
            "name_class": NOT_MUNICIPAL,
            "reason": reason,
        })
    return kept, skipped


def class_breakdown(classes: list[str]) -> str:
    """How many of each class, in NAME_CLASSES order: "no name 300, generic label 112"."""
    counts = Counter(classes)
    return ", ".join(f"{cls} {counts[cls]:,}" for cls in NAME_CLASSES if counts[cls])


def describe_counts(classes: list[str]) -> str:
    """A count with its classes, for Run_Config: "412 (no name 300, generic label 112)", or "0"."""
    return f"{len(classes):,} ({class_breakdown(classes)})" if classes else "0"


def read_skips(run_dir: Path) -> list[dict[str, str]]:
    """The run's skipped_sites.csv rows, or none if it has no list yet."""
    path = run_dir / SKIPPED_SITES_FILE
    if not path.exists():
        return []
    with path.open(newline="", encoding="utf-8-sig") as handle:
        return list(csv.DictReader(handle))


def record_skips(run_dir: Path, skipped: list[dict[str, Any]], searched: set[str]) -> None:
    """Add one search's skipped sites to the run's skipped_sites.csv.

    The list is the run's, not the last search's: a later search of fewer sites under the same run
    id (one country, a resume) adds to it rather than replacing it. `searched` is every site this
    search selected or the run already has a response for; those leave the list, so a site skipped
    once and searched later (by id, or with --include-unsearchable-names) is never counted as both.
    """
    rows = {normalize_scalar(row.get("site_id")): row for row in read_skips(run_dir)}
    rows.update({normalize_scalar(row.get("site_id")): row for row in skipped})
    write_csv_records(
        run_dir / SKIPPED_SITES_FILE,
        [row for site_id, row in rows.items() if site_id not in searched],
        SKIPPED_SITES_HEADERS,
    )


def recorded_skips(run_dir: Path, searched: set[str]) -> str:
    """What the run's searches skipped by name and never searched, for Run_Config."""
    if not (run_dir / SKIPPED_SITES_FILE).exists():
        return "not recorded (the run predates the rule, or searched only --site-ids)"
    return describe_counts([
        normalize_scalar(row.get("name_class"))
        for row in read_skips(run_dir)
        if normalize_scalar(row.get("site_id")) not in searched
    ])
