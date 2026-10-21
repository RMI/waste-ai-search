"""Whether a site's name gives a web search anything to look for.

The agent finds a facility by its name and location. A name that is filler, stray text from
another map tag, or only a kind of site gives it nothing to search for. Five classes:

- no name: empty, or the filler the ingestion pipeline gives an OpenStreetMap polygon with no name
  tag (`transform_osm.py` in RMI/waste_data_ingestion_pipeline);
- not a name: a URL, a bare number, or a survey note, which reach the name when a polygon has no
  name tag and the pipeline falls back to its note or description tags;
- generic label: says what kind of site it is, not which one (`is_generic_site_name`);
- mining feature: a mining spoil heap or similar, not a waste site, so there is nothing to find
  about it as one;
- named: everything else.

Site selection skips the first four classes (`select_sites` in search.py), and
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
from .schema import normalize_scalar


NO_NAME = "no name"
NOT_A_NAME = "not a name"
GENERIC = "generic label"
MINING = "mining feature"
NAMED = "named"
NAME_CLASSES = [NO_NAME, NOT_A_NAME, GENERIC, MINING, NAMED]
# Not searched by default (Hugh, 20 October 2026): none of them gives a search anything to look
# for. --site-ids and --include-unsearchable-names search them anyway.
SKIPPED_NAME_CLASSES = [NO_NAME, NOT_A_NAME, GENERIC, MINING]

# The run's list of sites skipped by name, in the run folder. Every search that doesn't list its
# site ids adds to it (record_skips), so arbitration can record in Run_Config what the run skipped.
SKIPPED_SITES_FILE = "skipped_sites.csv"
SKIPPED_SITES_HEADERS = ["site_id", "site_name", "original_site_name", "country_iso3", "name_class", "reason"]

PLACEHOLDER_NAMES = {"openstreetmap landfill"}
# Text from OSM's fixme-style tags, e.g. "update the tag landuse after survey".
SURVEY_NOTE_PHRASES = ("update the tag", "after survey", "fixme")
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
MINING_ORIGINAL_WORDS = re.compile(r"(отвал|террикон|терикон|відвал|хвостохранилищ|хвостосховищ)", re.IGNORECASE)


def name_reason(name: Any, original_name: Any = "") -> tuple[str, str]:
    """(class, the rule that put it there): one of NAME_CLASSES, and "" for a named site.

    `name` is the English name the search sees; `original_name` the source's own spelling, where
    it differs. The mining check reads both. Where the English name is empty or filler, a source
    spelling that is itself named keeps the site named: the translation lost a name the source
    recorded, and the agent searches that spelling first. The generic check reads only the English
    name: it knows only English words, so it would pass a generic original such as `Полигон ТБО`
    ("MSW landfill") as named.
    """
    text = normalize_scalar(name)
    lowered = text.casefold()
    if not text or lowered in PLACEHOLDER_NAMES:
        original = normalize_scalar(original_name)
        if original and name_class(original, original) == NAMED:
            return NAMED, ""
    if not text:
        return NO_NAME, "empty name"
    if lowered in PLACEHOLDER_NAMES:
        return NO_NAME, "ingestion's filler for an unnamed OSM polygon"
    if URL.search(text):
        return NOT_A_NAME, "a URL"
    if NUMBER.match(text):
        return NOT_A_NAME, "a bare number"
    if any(phrase in lowered for phrase in SURVEY_NOTE_PHRASES):
        return NOT_A_NAME, "a survey note"
    if MINING_WORDS.search(text):
        return MINING, "a mining word (blade, terrikon, overburden, spoil, slag, tailings pond)"
    if MINING_ORIGINAL_WORDS.search(normalize_scalar(original_name)):
        return MINING, "a mining word in the original name (отвал, террикон, відвал)"
    if is_generic_site_name(text):
        if text.lower().strip() in GENERIC_EXACT_NAMES:
            return GENERIC, "a listed generic phrase"
        return GENERIC, "only words like landfill, dump, waste, garbage, rubbish, tip"
    return NAMED, ""


def name_class(name: Any, original_name: Any = "") -> str:
    """Which of NAME_CLASSES a site's name falls in."""
    return name_reason(name, original_name)[0]


def site_name_class(site: dict[str, Any]) -> str:
    """Which of NAME_CLASSES a seed row's name falls in."""
    return name_class(site.get("site_name"), site.get("original_site_name"))


def skip_unsearchable_names(sites: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """(the sites to search, a skipped_sites.csv row for each site whose name is not searched)."""
    kept, skipped = [], []
    for site in sites:
        cls, reason = name_reason(site.get("site_name"), site.get("original_site_name"))
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
