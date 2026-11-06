"""Whether a source's quote, and the location it states, are really on its page.

The agent quotes each source (quoted_evidence_short) and copies where it puts the facility
(stated_address and stated_coordinates), but both are its own report. The link check keeps the
text of the page itself (page_text.py), so arbitration can check them, offline. One that isn't on
the page is flagged for the SME, who decides: nothing is dropped or downgraded for it, since a page
can say something in a way this check can't follow, in an image or a map, say.

Both sides are compared in plain form: lower case, accents and invisible formatting characters
taken off, and every run of anything but letters and digits one space. A word a PDF hyphenated
across a line break is read whole. Then they are compared again with the spaces inside a number
taken out ("45,000" is "45000") and a number split from a unit run into it ("10ha" is "10 ha"), "%"
read as "percent", and the usual road abbreviations written out ("Rd" is "road").

| Result | Means |
|---|---|
| found | Every part of it, split at an ellipsis, is on the page as written. A stated coordinate counts where the page gives it to as many decimals or more, or puts a point within 250 m of it in any form. |
| partly found | Not as written, but 80% of its words, and every number in it, are on the page; or one of its two coordinates is. |
| not found | Neither. A quote with a number the page doesn't have is not found, however many of its words are there. |
| not in the part read | Not found, but the link check read only part of the page, so it may be in the rest. Not flagged. |
| no page text | The link check kept no text for the page: it didn't load, refuses automated reading, is a scan, is built by JavaScript, or wasn't checked. Or the text it kept of an HTML page is only the site's menu, which the quote or location isn't in. Not flagged. |

A quote of fewer than three words ("Open", "Status Operational") is not checked: finding it says
nothing.
"""
from __future__ import annotations

import re
import unicodedata
from collections import Counter
from functools import lru_cache
from pathlib import Path
from typing import Any

from .link_check import LinkResult, describe_link
from .locate import COORDINATES, GRID_REFERENCE, ROOFTOP, STREET, haversine_km, parse_coordinates
from .page_text import HTML, PAGE_TEXT_DIR, little_text, read_in_part, read_page_text
from .payload import PART_SEPARATOR


FOUND = "found"
PARTLY = "partly found"
NOT_FOUND = "not found"
NOT_IN_PART_READ = "not in the part read"
NO_PAGE_TEXT = "no page text"
RESULTS = [FOUND, PARTLY, NOT_FOUND, NOT_IN_PART_READ, NO_PAGE_TEXT]

# How many of a phrase's words must be on the page for it to be partly found.
PARTLY_SHARE = 0.8
# A quote shorter than this, in words, is not checked.
MIN_QUOTE_WORDS = 3
# A point the page gives in another form, degrees-minutes-seconds or rounded, counts within this.
NEARBY_KM = 0.25
# How many numbers on one page are tried as coordinates: a register can hold thousands.
MAX_CANDIDATES = 5000
# The text kept of an HTML page this short, with no line longer than MENU_LINE_CHARS, is the site's
# menu, not the article, which was built after the page loaded or held back from automated readers.
# xeu.mx's page for site 9233 kept 593 characters of menu, its longest line 80, so its three quotes
# read "not found". A quote or location missing from it says nothing.
MENU_PAGE_CHARS = 1000
MENU_LINE_CHARS = 100

_ELLIPSIS = re.compile(r"\.\s?\.\s?\.|…|\[\s*\.\.\.\s*\]")
_NOT_WORD = re.compile(r"[\W_]+")
# A word hyphenated across a line break, as PDFs set them: "land-\nfill".
_LINE_BREAK_HYPHEN = re.compile(r"(\w)[-‐‑­]\s*\n\s*(\w)")
_DIGIT_SPACE = re.compile(r"(?<=\d) (?=\d)")
_DIGIT_LETTER = re.compile(r"(?<=\d)(?=[^\W\d_])|(?<=[^\W\d_])(?=\d)")
# A coordinate as a source writes it, sign aside: the digits before and after its decimal mark.
_COORDINATE = re.compile(r"(\d{1,3})[.,](\d{2,})")
_COORDINATE_CANDIDATE = re.compile(r"-?\d{1,3}(?:[.,]\d{2,}|\s*°)")
# A grid reference's letters and digits.
_GRID = re.compile(r"[A-Za-z]{1,2}\s*\d[\d\s]*")
# Scripts written without spaces between words, where a phrase is found anywhere in the text.
_UNSPACED = re.compile(r"[฀-໿က-႟ក-៿぀-ヿ㐀-鿿가-힯豈-﫿]")
ABBREVIATIONS = {
    "rd": "road", "st": "street", "ave": "avenue", "av": "avenue", "hwy": "highway", "dr": "drive",
    "ln": "lane", "blvd": "boulevard", "cres": "crescent", "ct": "court", "pl": "place", "tce": "terrace",
    "pkwy": "parkway", "rte": "route", "mt": "mount", "nth": "north", "sth": "south",
}
# Words an address may add or leave out without being another address.
COUNTRY_WORDS = {
    "united", "kingdom", "uk", "england", "scotland", "wales", "northern", "ireland", "canada",
    "australia", "zealand", "nz", "aotearoa",
}


def plain(text: Any) -> str:
    """Text in the plain form both sides are compared in."""
    decomposed = unicodedata.normalize("NFKD", str(text or ""))
    kept = "".join(
        character for character in decomposed
        if not unicodedata.combining(character) and unicodedata.category(character) != "Cf"
    )
    folded = kept.casefold().replace("%", " percent ").replace("per cent", "percent")
    return _NOT_WORD.sub(" ", folded).strip()


def joined(plain_text: str) -> str:
    """Plain text compared a second way: numbers whole, units apart, abbreviations written out."""
    text = _DIGIT_LETTER.sub(" ", _DIGIT_SPACE.sub("", plain_text))
    return " ".join(ABBREVIATIONS.get(word, word) for word in text.split())


def _numbers(joined_text: str) -> list[str]:
    return [word for word in joined_text.split() if any(character.isdigit() for character in word)]


class Page:
    """A page's kept text, in each form it is compared in."""

    def __init__(self, text: str) -> None:
        self.raw = text
        self.plain = f" {plain(text)} "
        dehyphenated = _LINE_BREAK_HYPHEN.sub(r"\1\2", text)
        self.whole_words = f" {plain(dehyphenated)} "
        self.joined = f" {joined(self.whole_words)} "
        self.compact = self.whole_words.replace(" ", "")
        self.words = set(self.plain.split()) | set(self.whole_words.split()) | set(self.joined.split())


def _on_page(fragment: str, page: Page) -> bool:
    if _UNSPACED.search(fragment):
        return fragment.replace(" ", "") in page.compact
    padded = f" {fragment} "
    return padded in page.plain or padded in page.whole_words or f" {joined(fragment)} " in page.joined


def _mostly_on_page(phrase: Any, page: Page) -> bool:
    """80% of the phrase's words on the page, country words aside, and every number in it."""
    phrase_plain = plain(phrase)
    if _UNSPACED.search(phrase_plain):
        # No words to count: count pairs of characters instead.
        compact = phrase_plain.replace(" ", "")
        pairs = [compact[index:index + 2] for index in range(len(compact) - 1)]
        return bool(pairs) and sum(1 for pair in pairs if pair in page.compact) / len(pairs) >= PARTLY_SHARE
    if any(number not in page.words for number in _numbers(joined(phrase_plain))):
        return False
    counted = [word for word in joined(phrase_plain).split() if (len(word) > 2 or word.isdigit()) and word not in COUNTRY_WORDS]
    return bool(counted) and sum(1 for word in counted if word in page.words) / len(counted) >= PARTLY_SHARE


def phrase_on_page(phrase: Any, page: Page | None, min_words: int = 1) -> str:
    """Whether a quote or an address is on the page. "" where there is nothing to check, or too
    little: fewer than `min_words` words."""
    fragments = [plain(part) for part in _ELLIPSIS.split(str(phrase or ""))]
    fragments = [fragment for fragment in fragments if fragment]
    if not fragments:
        return ""
    if not _UNSPACED.search(" ".join(fragments)) and len(joined(" ".join(fragments)).split()) < min_words:
        return ""
    if page is None:
        return NO_PAGE_TEXT
    if all(_on_page(fragment, page) for fragment in fragments):
        return FOUND
    return PARTLY if _mostly_on_page(phrase, page) else NOT_FOUND


def quote_on_page(quote: Any, page: Page | None) -> str:
    """Whether a quote is on the page; one of fewer than MIN_QUOTE_WORDS words is not checked."""
    return phrase_on_page(quote, page, MIN_QUOTE_WORDS)


def _point_nearby(page: Page, point: tuple[float, float]) -> bool:
    """Whether the page gives a point within NEARBY_KM of this one, in any form the locate step reads."""
    for index, match in enumerate(_COORDINATE_CANDIDATE.finditer(page.raw)):
        if index >= MAX_CANDIDATES:
            return False
        located = parse_coordinates(page.raw[match.start():match.end() + 80]) or parse_coordinates(
            page.raw[max(0, match.start() - 40):match.end() + 80]
        )
        if located is not None and located.point and haversine_km(*located.point, *point) <= NEARBY_KM:
            return True
    return False


def coordinates_on_page(text: str, page: Page, point: tuple[float, float] | None = None) -> str:
    """Whether stated coordinates are on the page: each number, to at least the decimals stated
    (trailing zeros aside), or else a point within NEARBY_KM in any form. A sign or hemisphere is not
    compared, since pages write them every which way."""
    numbers = _COORDINATE.findall(text)
    if not numbers:
        result = phrase_on_page(text, page)
    else:
        found = sum(
            1 for whole, decimals in numbers
            if re.search(rf"(?<![0-9]){whole} {decimals.rstrip('0') or '0'}[0-9]*(?![0-9])", page.plain)
        )
        result = FOUND if found == len(numbers) else PARTLY if found else NOT_FOUND
    if result != FOUND and point is not None and _point_nearby(page, point):
        return FOUND
    return result


def grid_reference_on_page(text: str, page: Page) -> str:
    """Whether a grid reference is on the page, its spaces aside: "TQ 310 806" is "TQ310806"."""
    references = [plain(match).replace(" ", "") for match in _GRID.findall(text)]
    references = [reference for reference in references if reference]
    if not references:
        return phrase_on_page(text, page)
    return FOUND if all(reference in page.compact for reference in references) else NOT_FOUND


def _parts(text: Any) -> list[str]:
    return [part.strip() for part in str(text or "").split(PART_SEPARATOR) if part.strip()]


def location_on_page(
    address: Any, coordinates: Any, page: Page | None, basis: str = "", point: tuple[float, float] | None = None
) -> str:
    """Whether the location a source states is on the page: its address, its coordinates or grid
    reference, or both. "" where it states nothing.

    Where a source gives both, the one its point was read from counts (`basis`): an address on the
    page says nothing of coordinates beside it that aren't. With no usable point, the better of the
    two counts."""
    addresses, points = _parts(address), _parts(coordinates)
    if not addresses and not points:
        return ""
    if page is None:
        return NO_PAGE_TEXT
    checked = []
    near = point if basis == COORDINATES else None
    for part in points:
        # Coordinates have decimals or degrees; anything else stated as coordinates is a grid
        # reference, or an easting and northing.
        if parse_coordinates(part) is not None:
            checked.append((COORDINATES, coordinates_on_page(part, page, near)))
        else:
            checked.append((GRID_REFERENCE, grid_reference_on_page(part, page)))
    for part in addresses:
        # An address that is nothing but coordinates, which the locate step reads as them.
        if parse_coordinates(part, strict=True) is not None:
            checked.append((COORDINATES, coordinates_on_page(part, page, near)))
        else:
            checked.append(("address", phrase_on_page(part, page)))
    used = {COORDINATES: COORDINATES, GRID_REFERENCE: GRID_REFERENCE, ROOFTOP: "address", STREET: "address"}.get(basis)
    results = [result for part_kind, result in checked if used is None or part_kind == used] or [result for _kind, result in checked]
    for result in (FOUND, PARTLY, NOT_FOUND):
        if result in results:
            return result
    return ""


def menu_only(text: str) -> bool:
    """Whether the text kept of a page is only the site's menu: short, with no line of prose."""
    return len(text) < MENU_PAGE_CHARS and all(len(line) <= MENU_LINE_CHARS for line in text.split("\n"))


def _point(row: dict[str, Any]) -> tuple[float, float] | None:
    try:
        return float(row["located_latitude"]), float(row["located_longitude"])
    except (KeyError, TypeError, ValueError):
        return None


class PageChecker:
    """Reads a run's kept page texts, by source_id, and checks quotes and locations against them.

    `notes` maps each kept text file to the link check's note on it: a page that gave so little text
    it is probably built by JavaScript counts as no text, and on a page read only in part, something
    not found may be in the rest. `kinds` maps it to the kind of page it was: on an HTML page whose
    text is only the site's menu (menu_only), something not found counts as no page text."""

    def __init__(self, run_dir: Path, notes: dict[str, str] | None = None, kinds: dict[str, str] | None = None) -> None:
        self.directory = run_dir / PAGE_TEXT_DIR
        self.notes = notes or {}
        self.kinds = kinds or {}
        self.load = lru_cache(maxsize=256)(self._load)
        self.menu_only = lru_cache(maxsize=None)(self._menu_only)

    def _load(self, source_id: str) -> Page | None:
        name = f"{source_id}.txt.gz" if source_id else ""
        if not name or little_text(self.notes.get(name, "")):
            return None
        text = read_page_text(self.directory, name)
        return Page(text) if text else None

    def check(self, row: dict[str, Any]) -> None:
        """Fill an evidence row's quote_on_page and location_on_page."""
        source_id = str(row.get("source_id") or "")
        page = self.load(source_id)
        quote = quote_on_page(row.get("quoted_evidence_short"), page)
        location = location_on_page(
            row.get("stated_address"), row.get("stated_coordinates"), page, row.get("location_basis", ""), _point(row)
        )
        if read_in_part(self.notes.get(f"{source_id}.txt.gz", "")):
            quote = NOT_IN_PART_READ if quote in (NOT_FOUND, PARTLY) else quote
            location = NOT_IN_PART_READ if location in (NOT_FOUND, PARTLY) else location
        elif self.menu_only(source_id):
            quote = NO_PAGE_TEXT if quote in (NOT_FOUND, PARTLY) else quote
            location = NO_PAGE_TEXT if location in (NOT_FOUND, PARTLY) else location
        row["quote_on_page"], row["location_on_page"] = quote, location

    def _menu_only(self, source_id: str) -> bool:
        """Whether a page is HTML whose kept text is only the site's menu (menu_only). link_words asks
        it of every page, so only an HTML page's text is read, and no Page is built from it."""
        name = f"{source_id}.txt.gz"
        if self.kinds.get(name) != HTML or little_text(self.notes.get(name, "")):
            return False
        text = read_page_text(self.directory, name)
        return bool(text) and menu_only(text)

    def link_words(self, results: dict[str, LinkResult]) -> dict[str, str]:
        """Site_Review's link_status for each page the link check saw, by the URL its rows carry,
        the one that loaded: its verdict, and why its quotes weren't checked where they weren't
        (link_check.describe_link)."""
        return {
            result.resolved_url: describe_link(
                result, bool(result.text_file) and self.menu_only(result.text_file.removesuffix(".txt.gz"))
            )
            for result in results.values()
        }


def describe_results(results: list[str]) -> str:
    """Counts of each result, for Run_Config: "found 12, partly found 3, not found 2"."""
    counts = Counter(result for result in results if result)
    return ", ".join(f"{name} {counts[name]}" for name in RESULTS if counts[name]) or "none checked"
