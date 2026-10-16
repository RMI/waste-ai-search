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

Used by scripts/measure_name_yield.py to see what searching each class found, so the measurement
and any rule that later skips a class sort names the same way.
"""
from __future__ import annotations

import re
from typing import Any

from .pilot_selector import GENERIC_EXACT_NAMES, is_generic_site_name
from .schema import normalize_scalar


NO_NAME = "no name"
NOT_A_NAME = "not a name"
GENERIC = "generic label"
MINING = "mining feature"
NAMED = "named"
NAME_CLASSES = [NO_NAME, NOT_A_NAME, GENERIC, MINING, NAMED]

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
    it differs, which only the mining check reads.
    """
    text = normalize_scalar(name)
    lowered = text.casefold()
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
