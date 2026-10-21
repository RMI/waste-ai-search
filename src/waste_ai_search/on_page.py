"""Whether a source's quote, and the location it states, are really on its page.

The agent quotes each source (quoted_evidence_short) and copies where it puts the facility
(stated_location), but both are its own report. The link check keeps the text of the page itself
(page_text.py), so arbitration can check them, offline. One that isn't on the page is flagged for the
SME, who decides: nothing is dropped or downgraded for it, since a page can say something in a way
this check can't follow, in an image or a map, say.

Both sides are compared in plain form: lower case, accents taken off, and every run of anything but
letters and digits one space, so punctuation, line breaks and a hyphen at a line's end don't matter.

| Result | Means |
|---|---|
| found | Every part of it, split at an ellipsis, is on the page as written. A stated coordinate counts where the page gives it to more decimals. |
| partly found | Not as written, but 80% of its words, or one of its two coordinates, are on the page. |
| not found | Neither. |
| no page text | The link check kept no text for the page: it didn't load, refuses automated reading, is a scan, or wasn't checked. |
"""
from __future__ import annotations

import re
import unicodedata
from collections import Counter
from functools import lru_cache
from pathlib import Path
from typing import Any

from .locate import COORDINATES, GRID_REFERENCE, PART_SEPARATOR
from .page_text import PAGE_TEXT_DIR, read_page_text


FOUND = "found"
PARTLY = "partly found"
NOT_FOUND = "not found"
NO_PAGE_TEXT = "no page text"
RESULTS = [FOUND, PARTLY, NOT_FOUND, NO_PAGE_TEXT]

# How many of a phrase's words must be on the page for it to be partly found.
PARTLY_SHARE = 0.8

_ELLIPSIS = re.compile(r"\.{3,}|…")
_NOT_WORD = re.compile(r"[\W_]+")
# A coordinate as a source writes it, sign aside: the digits before and after its decimal mark.
_COORDINATE = re.compile(r"(\d{1,3})[.,](\d{2,})")
# A grid reference's letters and digits.
_GRID = re.compile(r"[A-Za-z]{1,2}\s*\d[\d\s]*")


def plain(text: Any) -> str:
    """Text in the plain form both sides are compared in."""
    decomposed = unicodedata.normalize("NFKD", str(text or ""))
    folded = "".join(character for character in decomposed if not unicodedata.combining(character)).casefold()
    return _NOT_WORD.sub(" ", folded).strip()


def words(text: Any) -> list[str]:
    """The words of a phrase that say something: three letters or more, or any number."""
    return [word for word in plain(text).split() if len(word) > 2 or word.isdigit()]


class Page:
    """A page's kept text, in plain form, with its words and its letters and digits run together."""

    def __init__(self, text: str) -> None:
        self.plain = f" {plain(text)} "
        self.words = set(self.plain.split())
        self.compact = self.plain.replace(" ", "")


def phrase_on_page(phrase: Any, page: Page | None) -> str:
    """Whether a quote or an address is on the page. "" where there is nothing to check."""
    fragments = [plain(part) for part in _ELLIPSIS.split(str(phrase or ""))]
    fragments = [fragment for fragment in fragments if fragment]
    if not fragments:
        return ""
    if page is None:
        return NO_PAGE_TEXT
    if all(f" {fragment} " in page.plain for fragment in fragments):
        return FOUND
    counted = words(phrase)
    if counted and sum(1 for word in counted if word in page.words) / len(counted) >= PARTLY_SHARE:
        return PARTLY
    return NOT_FOUND


def coordinates_on_page(text: str, page: Page) -> str:
    """Whether stated coordinates are on the page: each number, to at least the decimals stated.
    A sign or hemisphere is not compared, since pages write them every which way."""
    numbers = _COORDINATE.findall(text)
    if not numbers:
        return phrase_on_page(text, page)
    # Trailing zeros say nothing: 175.02770 is the page's 175.0277.
    found = sum(
        1 for whole, decimals in numbers
        if re.search(rf"(?<![0-9]){whole} {decimals.rstrip('0') or '0'}[0-9]*(?![0-9])", page.plain)
    )
    if found == len(numbers):
        return FOUND
    return PARTLY if found else NOT_FOUND


def grid_reference_on_page(text: str, page: Page) -> str:
    """Whether a grid reference is on the page, its spaces aside: "TQ 310 806" is "TQ310806"."""
    references = [plain(match).replace(" ", "") for match in _GRID.findall(text)]
    references = [reference for reference in references if reference]
    if not references:
        return phrase_on_page(text, page)
    return FOUND if all(reference in page.compact for reference in references) else NOT_FOUND


def location_on_page(stated: Any, kind: Any, page: Page | None) -> str:
    """Whether a stated location is on the page. Where a source gives two, coordinates and an
    address, the better of the two counts. "" where nothing is stated."""
    text = str(stated or "").strip()
    if not text:
        return ""
    if page is None:
        return NO_PAGE_TEXT
    parts = [part.strip() for part in text.split(PART_SEPARATOR)]
    kinds = [part.strip() for part in str(kind or "").split(PART_SEPARATOR)]
    results = []
    for index, part in enumerate(parts):
        if not part:
            continue
        part_kind = kinds[index] if index < len(kinds) else ""
        if part_kind == COORDINATES or (not part_kind and _COORDINATE.search(part)):
            results.append(coordinates_on_page(part, page))
        elif part_kind == GRID_REFERENCE:
            results.append(grid_reference_on_page(part, page))
        else:
            results.append(phrase_on_page(part, page))
    for result in (FOUND, PARTLY, NOT_FOUND):
        if result in results:
            return result
    return ""


class PageChecker:
    """Reads a run's kept page texts, by source_id, and checks quotes and locations against them."""

    def __init__(self, run_dir: Path) -> None:
        self.directory = run_dir / PAGE_TEXT_DIR
        self.load = lru_cache(maxsize=256)(self._load)

    def _load(self, source_id: str) -> Page | None:
        text = read_page_text(self.directory, f"{source_id}.txt.gz") if source_id else None
        return Page(text) if text else None

    def check(self, row: dict[str, Any]) -> None:
        """Fill an evidence row's quote_on_page and location_on_page."""
        page = self.load(str(row.get("source_id") or ""))
        row["quote_on_page"] = phrase_on_page(row.get("quoted_evidence_short"), page)
        row["location_on_page"] = location_on_page(row.get("stated_location"), row.get("stated_location_kind"), page)


def describe_results(results: list[str]) -> str:
    """Counts of each result, for Run_Config: "found 12, partly found 3, not found 2"."""
    counts = Counter(result for result in results if result)
    return ", ".join(f"{name} {counts[name]}" for name in RESULTS if counts[name]) or "none checked"
