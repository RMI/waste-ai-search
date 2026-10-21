"""Turn the location each source states into a point (build step 3 of the source-matching plan).

The agent copies where each source puts the facility, as the source writes it: coordinates, a grid
reference or the site's street address. This module turns that text into a latitude and longitude,
and says how far the point can be trusted, as its `basis`:

| Basis | From | Used for |
|---|---|---|
| coordinates | Coordinates with 3 or more decimals (or seconds) | Matching, and a location value |
| grid reference | A UK or Irish grid reference, 100 m or finer | Matching, and a location value |
| rooftop | An address geocoded to the building or address point | Matching, and a location value |
| street | An address geocoded only to its street | Matching only |
| hint | Coordinates with fewer decimals, or a coarser grid reference | Nothing yet: approximate |
| none | Anything else, or a location a guard throws out | - |

Coordinates and grid references are parsed here, with no network, so arbitration can do it too.
Addresses go to a geocoder, which only the locate step calls: it runs in the search phase, beside
the link check, and keeps every answer in the run's `source_locations.json`, so arbitration reads
geocoded points from that file and stays offline. No geocoder is configured by default, and then
an address stays unlocated.

A facility name is never geocoded: geocoders find landfills by name from OpenStreetMap, which is
where most of our sites came from, so it would hand us our own point back. Only text the agent
labelled a street address is sent, with any leading facility name taken off.
"""
from __future__ import annotations

import json
import math
import os
import re
import threading
import time
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import urlsplit

from .schema import normalize_scalar


SOURCE_LOCATIONS_FILE = "source_locations.json"

# What a stated location became. The first three are a location value; street is good enough to
# match a source to a site but not to load; a hint is too coarse for either.
COORDINATES = "coordinates"
GRID_REFERENCE = "grid reference"
ROOFTOP = "rooftop"
STREET = "street"
HINT = "hint"
NONE = "none"
VALUE_BASES = (COORDINATES, GRID_REFERENCE, ROOFTOP)
MATCHING_BASES = (*VALUE_BASES, STREET)
BASES = (*MATCHING_BASES, HINT, NONE)

# The kinds of location the agent is asked to label each stated location with.
STREET_ADDRESS = "street address"
LOCATION_KINDS = [COORDINATES, GRID_REFERENCE, STREET_ADDRESS, "place description", "none"]
# A source that gives both coordinates and an address writes them as two parts, "a | b", with the
# kinds written the same way.
PART_SEPARATOR = "|"

# 0.001 degrees is about 110 m. Two decimals (about 1 km) is what the prompt itself gives, so it
# can only ever be a hint.
MIN_DECIMALS = 3
# The prompt's coordinates are rounded to this many decimals: enough to search with, and nothing
# worth copying.
PROMPT_DECIMALS = 2
# In batch 1 the agent copied our own point, to the metre, and credited it to a registry. A stated
# point this close to ours is treated as copied, as is one equal to the prompt's rounded point.
COPIED_WITHIN_METRES = 10.0
# Coordinates win over an address in the same source, but an address this far from them makes
# the pair doubtful. A landfill's address marks its gate, which can be well away from its middle.
DOUBTFUL_ADDRESS_KM = 2.0

# wastebook.co.uk republishes the Environment Agency register, but its coordinates are postcode
# centroids.
POSTCODE_CENTROID_SITES = ("wastebook.co.uk",)

# Stated text that means no location at all.
NO_LOCATION_TEXT = {"", "none", "n/a", "na", "not stated", "not given", "unknown", "-", "blank"}

NO_GEOCODER = "no geocoder configured"
NOT_LOCATED = "not located yet: run `locate --run-id` for this run"

# A PO box, private bag or the like is where post goes, not where the site is.
PO_BOX = re.compile(
    r"\b(?:p\.?\s*o\.?\s*box|post\s*office\s*box|g\.?p\.?o\.?\s*box|private\s+bag|locked\s+bag|"
    r"postfach|apartado(?:\s+postal)?|caixa\s+postal|casilla(?:\s+postal)?|bo[iî]te\s+postale|"
    r"b\.p\.\s*\d|postbus|box\s+\d+\s*$)",
    re.IGNORECASE,
)


@dataclass
class Located:
    """Where one stated location puts the facility, and how far that can be trusted."""

    latitude: float | None = None
    longitude: float | None = None
    basis: str = NONE
    reason: str = ""
    geocoder: str = ""
    # For a hint, how far the true point may be from this one: the half-diagonal of the square its
    # last digit names. An address can agree with a hint only within this, and DOUBTFUL_ADDRESS_KM.
    uncertainty_km: float = 0.0

    @property
    def point(self) -> tuple[float, float] | None:
        if self.latitude is None or self.longitude is None:
            return None
        return self.latitude, self.longitude

    def value(self) -> str:
        """The point as "lat, lon", which Google Maps finds when pasted into its search box."""
        return format_point(self.latitude, self.longitude) if self.point else ""


def format_point(latitude: float | None, longitude: float | None) -> str:
    if latitude is None or longitude is None:
        return ""
    return f"{format_coordinate(latitude)}, {format_coordinate(longitude)}"


def format_coordinate(number: float | None) -> str:
    """Six decimals, about 0.1 m, without trailing zeros."""
    if number is None:
        return ""
    text = f"{number:.6f}".rstrip("0").rstrip(".")
    return "0" if text in {"-0", ""} else text


def states_location(source: dict[str, Any]) -> bool:
    """Whether a source states a location at all."""
    return normalize_scalar(source.get("stated_location")).lower() not in NO_LOCATION_TEXT


def haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    radius = 6371.0088
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = phi2 - phi1
    dlambda = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlambda / 2) ** 2
    return 2 * radius * math.asin(math.sqrt(a))


def _float(value: Any) -> float | None:
    try:
        return float(normalize_scalar(value).replace(",", ""))
    except ValueError:
        return None


def seed_point(site: dict[str, Any]) -> tuple[float, float] | None:
    lat, lon = _float(site.get("latitude")), _float(site.get("longitude"))
    return None if lat is None or lon is None else (lat, lon)


def prompt_point(site: dict[str, Any]) -> tuple[float, float] | None:
    """Our coordinates as the prompt gives them: rounded to PROMPT_DECIMALS, about 1 km."""
    point = seed_point(site)
    if point is None:
        return None
    return round(point[0], PROMPT_DECIMALS), round(point[1], PROMPT_DECIMALS)


# --- coordinates -----------------------------------------------------------------------------
_HEMISPHERE_SIGN = {"N": 1, "S": -1, "E": 1, "W": -1}
_MINUS = "-−–"
# A hemisphere written as a word, "49.1234 North, 97.1234 West", is read as its letter.
_HEMISPHERE_WORDS = re.compile(r"(?<![A-Za-z])(north|south|east|west)(?![A-Za-z])", re.IGNORECASE)
# A number followed by a unit is a distance, an area or the like, never a coordinate: "12.345 ha
# at 48.8434, -87.4296".
_UNIT = re.compile(
    r"\s*(?:km|kms|kilomet(?:re|er)s?|met(?:re|er)s?|m|ha|hectares?|%|t|tonnes?|tons?|mi|miles?|ft|"
    r"feet|acres?|m2|m²|m3|m³|sq)(?![A-Za-z])",
    re.IGNORECASE,
)
# A label belongs to the number in its own part of the text: in "45.42 (lat), -75.70 (long)" the
# "lat" before the comma is the first number's.
_SEPARATOR = re.compile(r"[,;]")
# One degree of latitude, in km; a degree of longitude is never longer.
_KM_PER_DEGREE = 111.32

# Degrees, minutes and seconds: 63° 43' 58.15" N, or 51°30.25'N. The degree mark is required, so a
# bare number in an address is never read as a coordinate. A hemisphere letter goes before or
# after, never both: in "N 48.8 W 87.4" the W belongs to the second number.
_DMS = re.compile(
    r"(?<![A-Za-z])(?P<pre>[NSEW](?![A-Za-z]))?\s*(?P<sign>[-+−–])?\s*"
    r"(?P<deg>\d{1,3}(?:\.\d+)?)\s*[°º˚]\s*"
    r"(?:(?P<min>\d{1,2}(?:[.,]\d+)?)\s*['′’`]\s*"
    r"(?:(?P<sec>\d{1,2}(?:[.,]\d+)?)\s*(?:\"|″|”|''|’’)?\s*)?)?"
    r"(?(pre)|(?P<post>[NSEW](?![A-Za-z]))?)"
)
# Decimal degrees: 48.8434, -87.4296, or 48.8434°N 87.4296°W. A decimal point is required.
_DECIMAL = re.compile(
    r"(?<![A-Za-z\d.])(?P<pre>[NSEW](?![A-Za-z]))?\s*(?P<sign>[-+−–])?\s*"
    r"(?P<deg>\d{1,3}\.\d+)(?!\d|\.\d)\s*[°º˚]?\s*(?(pre)|(?P<post>[NSEW](?![A-Za-z]))?)"
)
_LAT_LABEL = re.compile(r"\blat(?:itude)?\b", re.IGNORECASE)
_LON_LABEL = re.compile(r"\b(?:lon|long|lng|longitude)\b", re.IGNORECASE)
# Words that may sit around a location that is nothing but coordinates or a grid reference.
_LABELS = re.compile(
    r"\b(?:lat|latitude|lon|long|lng|longitude|coordinates?|coords?|gps|wgs\s*-?\s*84|decimal|"
    r"degrees|dd|dms|location|position|centroid|site|ngr|os|ordnance|survey|national|grid|ref|"
    r"reference|irish|itm|transverse|mercator|easting|northing|e|n|x|y|at)\b",
    re.IGNORECASE,
)


@dataclass
class _Component:
    value: float
    hemisphere: str
    fine: bool
    label: str
    span: tuple[int, int]
    # The step its last digit names, in degrees: 0.01 for "48.84", 1/60 for whole minutes.
    resolution: float
    # Followed by a unit, so not a coordinate at all.
    unit: bool


def _decimals(number: str) -> int:
    parts = re.split(r"[.,]", number)
    return len(parts[1]) if len(parts) > 1 else 0


def _axis_label(text: str) -> str:
    """"lat" or "lon" if the text names one axis. Where it names both, as a header does ("Lat/Long:",
    "Coordinates (lat, long):"), the first named is the first number's."""
    lat, lon = _LAT_LABEL.search(text), _LON_LABEL.search(text)
    if lat and lon:
        return "lat" if lat.start() < lon.start() else "lon"
    return "lon" if lon else "lat" if lat else ""


def _component(match: re.Match, text: str, previous_end: int, dms: bool) -> _Component:
    degrees = float(match["deg"])
    minute_text = match.groupdict().get("min") or ""
    second_text = match.groupdict().get("sec") or ""
    value = degrees + float(minute_text.replace(",", ".") or 0) / 60 + float(second_text.replace(",", ".") or 0) / 3600
    if match["sign"] and match["sign"] in _MINUS:
        value = -value
    hemisphere = (match["pre"] or match["post"] or "").upper()
    if hemisphere and _HEMISPHERE_SIGN[hemisphere] < 0:
        value = -abs(value)
    if dms and minute_text:
        # A second is about 30 m; a hundredth of a minute about 18 m. Whole minutes are 1.8 km.
        fine = bool(second_text) or _decimals(minute_text) >= 2
        if second_text:
            resolution = 10 ** -_decimals(second_text) / 3600
        else:
            resolution = 10 ** -_decimals(minute_text) / 60
    else:
        fine = _decimals(match["deg"]) >= MIN_DECIMALS
        resolution = 10 ** -_decimals(match["deg"])
    # A label is read from the text before the number, back to the last comma: "Lat: 48.84, Long:
    # -87.43". So in "48.84 lat, -87.43 long" the second number takes no label, rather than the
    # first's. The first number takes a header naming both.
    before = text[previous_end:match.start()]
    label = _axis_label(before) if previous_end == 0 else _axis_label(_SEPARATOR.split(before)[-1])
    unit = bool(_UNIT.match(text, match.end()))
    return _Component(value, hemisphere, fine, label, match.span(), resolution, unit)


def _components(text: str) -> list[_Component]:
    found: list[tuple[re.Match, bool]] = [(match, True) for match in _DMS.finditer(text)]
    taken = [match.span() for match, _dms in found]
    for match in _DECIMAL.finditer(text):
        if not any(start < match.end() and match.start() < end for start, end in taken):
            found.append((match, False))
    found.sort(key=lambda item: item[0].start())
    out: list[_Component] = []
    previous_end = 0
    for match, dms in found:
        out.append(_component(match, text, previous_end, dms))
        previous_end = match.end()
    return out


def _only_labels(text: str, spans: list[tuple[int, int]]) -> bool:
    """Whether nothing but labels and punctuation is left once `spans` are taken out of `text`."""
    rest = text
    for start, end in sorted(spans, reverse=True):
        rest = rest[:start] + " " + rest[end:]
    rest = _LABELS.sub(" ", rest)
    return not re.search(r"[A-Za-z\d]", rest)


def _axes(first: _Component, second: _Component) -> tuple[_Component, _Component]:
    """A pair as (latitude, longitude): "lat, lon" unless a hemisphere, a label or a value beyond 90
    says otherwise."""
    if first.hemisphere in {"E", "W"} or second.hemisphere in {"N", "S"}:
        return second, first
    if first.hemisphere in {"N", "S"} or second.hemisphere in {"E", "W"}:
        return first, second
    if first.label == "lon" or second.label == "lat":
        return second, first
    if first.label == "lat" or second.label == "lon":
        return first, second
    if abs(first.value) > 90 >= abs(second.value):
        return second, first
    return first, second


def _in_range(latitude: float, longitude: float) -> bool:
    return -90 <= latitude <= 90 and -180 <= longitude <= 180 and not (latitude == 0 and longitude == 0)


def parse_coordinates(text: str, strict: bool = False) -> Located | None:
    """Read a latitude and longitude from text, or None if it holds no coordinate pair.

    "lat, lon" is assumed. "lon, lat" is read only when it is unambiguous: a hemisphere letter or
    word, or a label, says so, or the first number is beyond 90. `strict` asks for text that is
    nothing but the coordinates and their labels, for text the agent labelled something else.
    """
    text = normalize_scalar(text)
    if "." not in text and ";" in text:
        text = re.sub(r"(\d),(\d)", r"\1.\2", text)  # decimal commas: "48,8434; 2,3488"
    text = _HEMISPHERE_WORDS.sub(lambda match: match[1][0].upper(), text)
    components = [component for component in _components(text) if not component.unit]
    if len(components) < 2:
        return None
    if strict and (len(components) > 2 or not _only_labels(text, [c.span for c in components])):
        return None
    # In prose ("1.5 km north of 48.8434, -87.4296"), the first neighbouring pair that is in range
    # and precise enough to be a location is the one meant; failing that, the first in range.
    pairs = [_axes(one, two) for one, two in zip(components, components[1:])]
    in_range = [pair for pair in pairs if _in_range(pair[0].value, pair[1].value)]
    precise = [pair for pair in in_range if pair[0].fine and pair[1].fine]
    latitude, longitude = (precise or in_range or pairs)[0]

    lat, lon = latitude.value, longitude.value
    if not _in_range(lat, lon):
        return None if strict else Located(reason=f"coordinates out of range ({lat:g}, {lon:g})")
    if latitude.fine and longitude.fine:
        return Located(lat, lon, COORDINATES, "")
    uncertainty = max(latitude.resolution, longitude.resolution) / 2 * _KM_PER_DEGREE * math.sqrt(2)
    return Located(
        lat, lon, HINT, f"coordinates with fewer than {MIN_DECIMALS} decimals are approximate",
        uncertainty_km=uncertainty,
    )


# --- grid references ---------------------------------------------------------------------------
# The OS National Grid: two letters naming a 100 km square, then an easting and northing of equal
# length. The first letter is H, J, N, O, S or T for Great Britain; no letter I is used.
_OS_GRID = re.compile(r"(?<![A-Za-z])([HJNOST][A-HJ-Z])\s*(\d{2,10})(?:\s+(\d{1,5}))?(?![\d.A-Za-z])")
# The Irish Grid: one letter naming a 100 km square.
_IRISH_GRID = re.compile(r"(?<![A-Za-z])([A-HJ-Z])\s*(\d{2,10})(?:\s+(\d{1,5}))?(?![\d.A-Za-z])")
# Irish Transverse Mercator: plain metres, read only where the text says ITM.
_ITM_LABEL = re.compile(r"\bITM\b|irish\s+transverse\s+mercator", re.IGNORECASE)
_METRES = re.compile(r"(?<![\d.])(\d{6}(?:\.\d+)?)(?![\d])")
# An easting and a northing in metres, each with its label: "E 531090 N 180600", "Easting 531090,
# Northing 180600", "X 531090 Y 180600". The letters are labels here, never a grid square.
_EASTING = re.compile(r"(?<![A-Za-z])(?:(?i:eastings?)|E|X)(?![A-Za-z])\s*[:=]?\s*(\d{5,7}(?:\.\d+)?)(?![\d.])")
_NORTHING = re.compile(r"(?<![A-Za-z])(?:(?i:northings?)|N|Y)(?![A-Za-z])\s*[:=]?\s*(\d{5,7}(?:\.\d+)?)(?![\d.])")
_IRISH_LABEL = re.compile(r"\birish\b", re.IGNORECASE)
# Irish Grid letters apply to Ireland and Northern Ireland only.
IRISH_GRID_ISO3 = {"IRL", "GBR", ""}
# A grid reference this fine (3 digits a side, 100 m) or finer is a location; coarser is a hint.
MIN_GRID_DIGITS = 3

_transformers: dict[str, Any] = {}


def _to_wgs84(crs: str, easting: float, northing: float) -> tuple[float, float]:
    """Convert a projected point to latitude and longitude with pyproj."""
    if crs not in _transformers:
        from pyproj import Transformer

        _transformers[crs] = Transformer.from_crs(crs, "EPSG:4326", always_xy=True)
    lon, lat = _transformers[crs].transform(easting, northing)
    return lat, lon


def _split_digits(first: str, second: str | None) -> tuple[str, str] | None:
    if second is not None:
        return (first, second) if len(first) == len(second) and len(first) <= 5 else None
    if len(first) % 2:
        return None
    half = len(first) // 2
    return first[:half], first[half:]


def _grid_point(square_east: int, square_north: int, digits: tuple[str, str], crs: str, label: str) -> Located:
    precision = len(digits[0])
    resolution = 10 ** (5 - precision)
    # The reference names the square's south-west corner; its middle is the better point.
    easting = square_east * 100_000 + int(digits[0]) * resolution + resolution / 2
    northing = square_north * 100_000 + int(digits[1]) * resolution + resolution / 2
    lat, lon = _to_wgs84(crs, easting, northing)
    if precision >= MIN_GRID_DIGITS:
        return Located(lat, lon, GRID_REFERENCE, "")
    return Located(
        lat, lon, HINT, f"{label} coarser than 100 m is approximate",
        uncertainty_km=resolution * math.sqrt(2) / 2 / 1000,
    )


def _letter_index(letter: str) -> int:
    index = ord(letter) - ord("A")
    return index - 1 if index > 8 else index  # the grids skip I


def parse_os_grid(match: re.Match) -> Located | None:
    letters = match[1].upper()
    digits = _split_digits(match[2], match[3])
    if digits is None:
        return None
    first, second = _letter_index(letters[0]), _letter_index(letters[1])
    square_east = ((first - 2) % 5) * 5 + (second % 5)
    square_north = (19 - (first // 5) * 5) - (second // 5)
    if not (0 <= square_east <= 6 and 0 <= square_north <= 12):
        return None
    return _grid_point(square_east, square_north, digits, "EPSG:27700", "an OS grid reference")


def parse_irish_grid(match: re.Match) -> Located | None:
    digits = _split_digits(match[2], match[3])
    if digits is None:
        return None
    index = _letter_index(match[1].upper())
    return _grid_point(index % 5, 4 - index // 5, digits, "EPSG:29903", "an Irish Grid reference")


def parse_itm(text: str) -> Located | None:
    if not _ITM_LABEL.search(text):
        return None
    numbers = _METRES.findall(text)
    if len(numbers) < 2:
        return None
    easting, northing = float(numbers[0]), float(numbers[1])
    first_label = text[: text.find(numbers[0])]
    if re.search(r"\bN(?:orthing)?\b", first_label, re.IGNORECASE) and not re.search(
        r"\bE(?:asting)?\b", first_label, re.IGNORECASE
    ):
        easting, northing = northing, easting
    if not (300_000 <= easting <= 900_000 and 400_000 <= northing <= 1_000_000):
        return None
    lat, lon = _to_wgs84("EPSG:2157", easting, northing)
    return Located(lat, lon, GRID_REFERENCE, "")


def _labelled_metres(text: str) -> tuple[float, float, list[tuple[int, int]]] | None:
    """An easting and a northing, each labelled, and where they are in the text."""
    east, north = _EASTING.search(text), _NORTHING.search(text)
    if east is None or north is None:
        return None
    return float(east[1]), float(north[1]), [east.span(), north.span()]


def parse_labelled_metres(easting: float, northing: float, iso3: str, text: str) -> Located | None:
    """Labelled metres in the grid the country uses: the British National Grid in the UK, and in
    Ireland the Irish Grid or ITM, which never overlap. A UK text that says Irish is in Ireland's
    grids, as Northern Ireland's sources are. Anywhere else the grid is unknown, so None."""
    iso3 = iso3.upper()
    if iso3 == "GBR" and not _IRISH_LABEL.search(text):
        if 0 <= easting <= 700_000 and 0 <= northing <= 1_300_000:
            lat, lon = _to_wgs84("EPSG:27700", easting, northing)
            return Located(lat, lon, GRID_REFERENCE, "")
        return None
    if iso3 not in {"GBR", "IRL"}:
        return None
    if easting >= 400_000 and northing >= 500_000:
        crs_ok = 400_000 <= easting <= 900_000 and northing <= 1_000_000
        crs = "EPSG:2157"  # ITM
    else:
        crs_ok = 0 <= easting < 400_000 and 0 <= northing < 500_000
        crs = "EPSG:29903"  # the Irish Grid
    if not crs_ok:
        return None
    lat, lon = _to_wgs84(crs, easting, northing)
    return Located(lat, lon, GRID_REFERENCE, "")


def parse_grid_reference(text: str, iso3: str = "", strict: bool = False) -> Located | None:
    """Read a UK or Irish grid reference, ITM metres labelled as such, or a labelled easting and
    northing; None if there is none.

    Where the text holds several references, as in "Licence W0123, grid ref T 285 935", the first
    one 100 m or finer wins over a coarser one before it.
    """
    text = normalize_scalar(text)
    itm = parse_itm(text)
    if itm is not None:
        return itm
    # "E 531090 N 180600" is metres. Read as the Irish Grid's square E, it would land in Scotland.
    labels: list[tuple[int, int]] = []
    metres = _labelled_metres(text)
    if metres is not None:
        easting, northing, labels = metres
        if not strict or _only_labels(text, labels):
            located = parse_labelled_metres(easting, northing, iso3, text)
            if located is not None:
                return located
    candidates = [(_OS_GRID, parse_os_grid)]
    if iso3.upper() in IRISH_GRID_ISO3:
        candidates.append((_IRISH_GRID, parse_irish_grid))
    found: list[Located] = []
    for pattern, parse in candidates:
        for match in pattern.finditer(text):
            if any(start < match.end() and match.start() < end for start, end in labels):
                continue
            if strict and not _only_labels(text, [match.span()]):
                continue
            located = parse(match)
            if located is not None:
                found.append(located)
    return next((item for item in found if item.basis == GRID_REFERENCE), found[0] if found else None)


def parse_point(text: str, iso3: str = "", strict: bool = False) -> Located | None:
    """Coordinates or a grid reference, read locally; None if the text holds neither."""
    return parse_coordinates(text, strict=strict) or parse_grid_reference(text, iso3, strict=strict)


# --- addresses ---------------------------------------------------------------------------------
@dataclass
class GeocodeMatch:
    latitude: float
    longitude: float
    # ROOFTOP or STREET, or "" for anything coarser, which is not used.
    precision: str
    # What the geocoder said about the match, kept in source_locations.json.
    match: str


class Geocoder(Protocol):
    name: str

    def geocode(self, address: str, iso2: str) -> GeocodeMatch | None: ...


def _session(session: Any) -> Any:
    if session is not None:
        return session
    import requests

    return requests.Session()


# https://learn.microsoft.com/en-us/rest/api/maps/search/get-geocoding?view=rest-maps-2026-01-01
# GET {endpoint}/geocode, api-version 2026-01-01. A free-text address goes in addressLine, since
# countryRegion (ISO 3166-1 alpha-2) may not be combined with `query`. Auth is a `subscription-key`
# header, or a Microsoft Entra ID token for https://atlas.microsoft.com/.default with the account's
# client id in `x-ms-client-id`. Each feature's properties carry `type` (Address, RoadBlock,
# RoadIntersection, Postcode1, PopulatedPlace...), `confidence` (High, Medium, Low), `matchCodes`
# (Good, Ambiguous, UpHierarchy) and `geocodePoints`, each with a `calculationMethod` (Rooftop,
# Parcel, Interpolation, InterpolationOffset). Coordinates are [longitude, latitude].
AZURE_MAPS_ENDPOINT = "https://atlas.microsoft.com"
AZURE_MAPS_API_VERSION = "2026-01-01"
AZURE_MAPS_SCOPE = "https://atlas.microsoft.com/.default"


class AzureMapsGeocoder:
    """Azure Maps geocoding, by subscription key or by Entra ID (`az login`)."""

    name = "azure-maps"

    def __init__(
        self,
        subscription_key: str = "",
        client_id: str = "",
        credential: Any = None,
        session: Any = None,
        endpoint: str = AZURE_MAPS_ENDPOINT,
        timeout: float = 15.0,
    ) -> None:
        if not subscription_key and not client_id:
            raise ValueError(
                "Azure Maps needs AZURE_MAPS_SUBSCRIPTION_KEY, or AZURE_MAPS_CLIENT_ID to sign in with "
                "Entra ID."
            )
        self.subscription_key = subscription_key
        self.client_id = client_id
        self.credential = credential
        self.session = _session(session)
        self.endpoint = endpoint.rstrip("/")
        self.timeout = timeout

    @classmethod
    def from_env(cls, session: Any = None) -> "AzureMapsGeocoder":
        return cls(
            subscription_key=_env("AZURE_MAPS_SUBSCRIPTION_KEY"),
            client_id=_env("AZURE_MAPS_CLIENT_ID"),
            session=session,
        )

    def _headers(self) -> dict[str, str]:
        if self.subscription_key:
            return {"subscription-key": self.subscription_key}
        if self.credential is None:
            from azure.identity import DefaultAzureCredential

            self.credential = DefaultAzureCredential()
        token = self.credential.get_token(AZURE_MAPS_SCOPE).token
        return {"Authorization": f"Bearer {token}", "x-ms-client-id": self.client_id}

    def geocode(self, address: str, iso2: str) -> GeocodeMatch | None:
        params: dict[str, Any] = {"api-version": AZURE_MAPS_API_VERSION, "addressLine": address, "top": 1}
        if iso2:
            params["countryRegion"] = iso2
        response = self.session.get(
            f"{self.endpoint}/geocode", params=params, headers=self._headers(), timeout=self.timeout
        )
        response.raise_for_status()
        features = (response.json() or {}).get("features") or []
        return azure_match(features[0]) if features else None


def azure_match(feature: dict[str, Any]) -> GeocodeMatch | None:
    """Read one Azure Maps feature: an exact address point is rooftop, a street or block is street.

    An interpolated house number is placed along its street, so it counts as street. A match the
    service had to broaden (UpHierarchy), one of several candidates (Ambiguous), or Low confidence
    is not used.
    """
    properties = feature.get("properties") or {}
    points = properties.get("geocodePoints") or []
    point = next((p for p in points if "Display" in (p.get("usageTypes") or [])), points[0] if points else {})
    coordinates = ((point or {}).get("geometry") or feature.get("geometry") or {}).get("coordinates") or []
    if len(coordinates) < 2:
        return None
    kind = normalize_scalar(properties.get("type"))
    method = normalize_scalar((point or {}).get("calculationMethod"))
    confidence = normalize_scalar(properties.get("confidence"))
    codes = [normalize_scalar(code) for code in properties.get("matchCodes") or []]
    described = f"{kind or '?'}, {method or 'no method'}, {confidence or 'no confidence'}, {'/'.join(codes) or 'no match code'}"
    precision = ""
    if confidence != "Low" and "UpHierarchy" not in codes and "Ambiguous" not in codes:
        if kind == "Address" and method in {"Rooftop", "Parcel"}:
            precision = ROOFTOP
        elif kind in {"Address", "RoadBlock", "RoadIntersection"}:
            precision = STREET
    return GeocodeMatch(float(coordinates[1]), float(coordinates[0]), precision, described)


NOMINATIM_URL = "https://nominatim.openstreetmap.org"
# Nominatim's usage policy asks for a User-Agent naming the application. It comes from
# configuration, never from whoever is running the search.
DEFAULT_NOMINATIM_USER_AGENT = "waste-ai-search (RMI)"
# The public instance allows at most one request a second.
NOMINATIM_MIN_INTERVAL_SECONDS = 1.0


class NominatimGeocoder:
    """OpenStreetMap's Nominatim: the public instance or a configured one, one request a second."""

    name = "nominatim"

    def __init__(
        self,
        url: str = NOMINATIM_URL,
        user_agent: str = DEFAULT_NOMINATIM_USER_AGENT,
        session: Any = None,
        min_interval: float = NOMINATIM_MIN_INTERVAL_SECONDS,
        clock: Any = time.monotonic,
        sleep: Any = time.sleep,
        timeout: float = 15.0,
    ) -> None:
        self.url = url.rstrip("/")
        self.user_agent = user_agent or DEFAULT_NOMINATIM_USER_AGENT
        self.session = _session(session)
        self.min_interval = min_interval
        self.clock = clock
        self.sleep = sleep
        self.timeout = timeout
        self._lock = threading.Lock()
        self._last: float | None = None

    @classmethod
    def from_env(cls, session: Any = None) -> "NominatimGeocoder":
        return cls(
            url=_env("NOMINATIM_URL") or NOMINATIM_URL,
            user_agent=_env("NOMINATIM_USER_AGENT") or DEFAULT_NOMINATIM_USER_AGENT,
            session=session,
        )

    def _wait(self) -> None:
        with self._lock:
            if self._last is not None:
                delay = self._last + self.min_interval - self.clock()
                if delay > 0:
                    self.sleep(delay)
            self._last = self.clock()

    def geocode(self, address: str, iso2: str) -> GeocodeMatch | None:
        params: dict[str, Any] = {"q": address, "format": "jsonv2", "addressdetails": 1, "limit": 1}
        if iso2:
            params["countrycodes"] = iso2.lower()
        self._wait()
        response = self.session.get(
            f"{self.url}/search", params=params, headers={"User-Agent": self.user_agent}, timeout=self.timeout
        )
        response.raise_for_status()
        results = response.json() or []
        return nominatim_match(results[0]) if results else None


def nominatim_match(result: dict[str, Any]) -> GeocodeMatch | None:
    """Read one Nominatim result: an address point or building is rooftop, a road is street.

    Anything else - a town, a postcode, or a mapped feature such as a landfill that happens to carry
    the address - is not used: the last would be OpenStreetMap's own point for the site.
    """
    lat, lon = _float(result.get("lat")), _float(result.get("lon"))
    if lat is None or lon is None:
        return None
    category = normalize_scalar(result.get("category") or result.get("class"))
    kind = normalize_scalar(result.get("type"))
    described = f"{category or '?'}={kind or '?'}, rank {normalize_scalar(result.get('place_rank')) or '?'}"
    precision = ""
    if category == "building" or (category == "place" and kind in {"house", "building"}):
        precision = ROOFTOP
    elif category == "highway":
        precision = STREET
    return GeocodeMatch(lat, lon, precision, described)


def _env(name: str) -> str:
    return (os.environ.get(name) or "").strip()


def _load_env() -> None:
    try:
        from dotenv import load_dotenv  # type: ignore

        load_dotenv()
    except ImportError:
        pass


GEOCODERS = {"azure-maps": AzureMapsGeocoder, "nominatim": NominatimGeocoder}


def make_geocoder(name: str = "") -> Geocoder | None:
    """The configured geocoder: `name`, else WASTE_AI_SEARCH_GEOCODER. None when neither names one."""
    _load_env()
    choice = (name or _env("WASTE_AI_SEARCH_GEOCODER")).strip().lower()
    if choice in {"", "none"}:
        return None
    if choice not in GEOCODERS:
        raise ValueError(f"Unknown geocoder {choice!r}: use one of {', '.join(sorted(GEOCODERS))}, or none.")
    return GEOCODERS[choice].from_env()


# ISO 3166-1 alpha-3 to alpha-2, which both geocoders take to keep an address in its country.
_ISO_PAIRS = """
AFG AF ALA AX ALB AL DZA DZ ASM AS AND AD AGO AO AIA AI ATA AQ ATG AG ARG AR ARM AM ABW AW AUS AU
AUT AT AZE AZ BHS BS BHR BH BGD BD BRB BB BLR BY BEL BE BLZ BZ BEN BJ BMU BM BTN BT BOL BO BES BQ
BIH BA BWA BW BVT BV BRA BR IOT IO BRN BN BGR BG BFA BF BDI BI CPV CV KHM KH CMR CM CAN CA CYM KY
CAF CF TCD TD CHL CL CHN CN CXR CX CCK CC COL CO COM KM COG CG COD CD COK CK CRI CR CIV CI HRV HR
CUB CU CUW CW CYP CY CZE CZ DNK DK DJI DJ DMA DM DOM DO ECU EC EGY EG SLV SV GNQ GQ ERI ER EST EE
SWZ SZ ETH ET FLK FK FRO FO FJI FJ FIN FI FRA FR GUF GF PYF PF ATF TF GAB GA GMB GM GEO GE DEU DE
GHA GH GIB GI GRC GR GRL GL GRD GD GLP GP GUM GU GTM GT GGY GG GIN GN GNB GW GUY GY HTI HT HMD HM
VAT VA HND HN HKG HK HUN HU ISL IS IND IN IDN ID IRN IR IRQ IQ IRL IE IMN IM ISR IL ITA IT JAM JM
JPN JP JEY JE JOR JO KAZ KZ KEN KE KIR KI PRK KP KOR KR KWT KW KGZ KG LAO LA LVA LV LBN LB LSO LS
LBR LR LBY LY LIE LI LTU LT LUX LU MAC MO MDG MG MWI MW MYS MY MDV MV MLI ML MLT MT MHL MH MTQ MQ
MRT MR MUS MU MYT YT MEX MX FSM FM MDA MD MCO MC MNG MN MNE ME MSR MS MAR MA MOZ MZ MMR MM NAM NA
NRU NR NPL NP NLD NL NCL NC NZL NZ NIC NI NER NE NGA NG NIU NU NFK NF MKD MK MNP MP NOR NO OMN OM
PAK PK PLW PW PSE PS PAN PA PNG PG PRY PY PER PE PHL PH PCN PN POL PL PRT PT PRI PR QAT QA REU RE
ROU RO RUS RU RWA RW BLM BL SHN SH KNA KN LCA LC MAF MF SPM PM VCT VC WSM WS SMR SM STP ST SAU SA
SEN SN SRB RS SYC SC SLE SL SGP SG SXM SX SVK SK SVN SI SLB SB SOM SO ZAF ZA SGS GS SSD SS ESP ES
LKA LK SDN SD SUR SR SJM SJ SWE SE CHE CH SYR SY TWN TW TJK TJ TZA TZ THA TH TLS TL TGO TG TKL TK
TON TO TTO TT TUN TN TUR TR TKM TM TCA TC TUV TV UGA UG UKR UA ARE AE GBR GB USA US UMI UM URY UY
UZB UZ VUT VU VEN VE VNM VN VGB VG VIR VI WLF WF ESH EH YEM YE ZMB ZM ZWE ZW XKX XK
""".split()
ISO3_TO_ISO2 = dict(zip(_ISO_PAIRS[::2], _ISO_PAIRS[1::2]))


def iso2(iso3: Any) -> str:
    return ISO3_TO_ISO2.get(normalize_scalar(iso3).upper(), "")


def _plain(text: Any) -> str:
    return re.sub(r"[\W_]+", " ", normalize_scalar(text).casefold()).strip()


def address_to_send(text: str, names: list[str]) -> str:
    """The address text a geocoder may see: never a facility name, and never one leading it.

    "Brady Road Landfill, 1777 Brady Road, Winnipeg" is sent as "1777 Brady Road, Winnipeg". Text
    that is only the name gives nothing to send.
    """
    address = normalize_scalar(text)
    for name in sorted({normalize_scalar(n) for n in names if normalize_scalar(n)}, key=len, reverse=True):
        if _plain(address) == _plain(name):
            return ""
        leading = re.match(re.escape(name), address, re.IGNORECASE)
        if leading:
            rest = address[leading.end():].lstrip(" ,;:-–")
            if rest:
                address = rest
    return address


# --- the cache ---------------------------------------------------------------------------------
def cache_key(text: str, iso3: Any) -> str:
    """Located text is keyed by itself and its country, never by site, so one answer serves all."""
    return f"{normalize_scalar(iso3).upper()}|{normalize_scalar(text)}"


def load_locations(path: Path) -> dict[str, dict[str, Any]]:
    """A run's source_locations.json. Missing or unreadable means nothing located, never an error."""
    if not path.exists():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}
    return payload if isinstance(payload, dict) else {}


def save_locations(path: Path, entries: dict[str, dict[str, Any]]) -> None:
    """Write under a hidden temporary name, then rename: a hard stop never leaves half a file, which
    would read as nothing located and send every address again."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    try:
        temporary.write_text(json.dumps(dict(sorted(entries.items())), indent=2, ensure_ascii=False), encoding="utf-8")
        os.replace(temporary, path)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise


def _record(cache: dict[str, dict[str, Any]], text: str, iso3: Any, located: Located, match: str = "") -> None:
    """Keep one answer in the cache. An unchanged one keeps its date, so the file only changes
    when an answer does."""
    key = cache_key(text, iso3)
    entry = {
        "text": text,
        "country": normalize_scalar(iso3).upper(),
        "latitude": located.latitude,
        "longitude": located.longitude,
        "basis": located.basis,
        "reason": located.reason,
        "geocoder": located.geocoder,
        "match": match,
    }
    earlier = cache.get(key) or {}
    same = all(earlier.get(field) == value for field, value in entry.items())
    entry["located_at"] = earlier.get("located_at") if same and earlier.get("located_at") else date.today().isoformat()
    cache[key] = entry


def _from_entry(entry: dict[str, Any]) -> Located:
    return Located(
        _float(entry.get("latitude")) if entry.get("latitude") is not None else None,
        _float(entry.get("longitude")) if entry.get("longitude") is not None else None,
        normalize_scalar(entry.get("basis")) or NONE,
        normalize_scalar(entry.get("reason")),
        normalize_scalar(entry.get("geocoder")),
    )


class GeocoderError(RuntimeError):
    """A geocoder could not answer; the address is tried again next time."""


def geocode_address(
    address: str,
    iso3: Any,
    cache: dict[str, dict[str, Any]] | None,
    geocoder: Geocoder | None,
    live: bool,
) -> Located:
    """Where an address is, from the cache first. Only a live locate step asks the geocoder.

    An answer is cached whatever it is, so an address is never sent twice. One cached before a
    geocoder was configured is asked again once there is one. A failed request is not cached.
    """
    key = cache_key(address, iso3)
    entry = (cache or {}).get(key)
    if entry is not None and not (live and geocoder is not None and entry.get("reason") == NO_GEOCODER):
        return _from_entry(entry)
    if not live:
        return Located(reason=NOT_LOCATED)
    if geocoder is None:
        located, match = Located(reason=NO_GEOCODER), ""
    else:
        try:
            found = geocoder.geocode(address, iso2(iso3))
        except Exception as exc:  # noqa: BLE001 - one bad lookup must not end the locate step
            raise GeocoderError(f"{geocoder.name}: {type(exc).__name__}: {exc}") from exc
        if found is None:
            located, match = Located(reason="the geocoder found no match", geocoder=geocoder.name), ""
        elif found.precision in (ROOFTOP, STREET):
            located, match = Located(found.latitude, found.longitude, found.precision, "", geocoder.name), found.match
        else:
            located = Located(
                found.latitude, found.longitude, NONE,
                "geocoded only coarser than a street, so not used", geocoder.name,
            )
            match = found.match
    if cache is not None:
        _record(cache, address, iso3, located, match)
    return located


# --- one source --------------------------------------------------------------------------------
def _host_is(url: Any, sites: tuple[str, ...]) -> bool:
    try:
        host = (urlsplit(normalize_scalar(url)).hostname or "").rstrip(".")
    except ValueError:
        return False
    return any(host == site or host.endswith("." + site) for site in sites)


def _copied(located: Located, site: dict[str, Any]) -> bool:
    """Whether stated coordinates are the prompt's rounded point, or within 10 m of our seed point."""
    point = located.point
    if point is None:
        return False
    rounded = prompt_point(site)
    if rounded is not None and all(abs(a - b) < 1e-9 for a, b in zip(point, rounded)):
        return True
    seed = seed_point(site)
    return seed is not None and haversine_km(*seed, *point) * 1000 <= COPIED_WITHIN_METRES


def _parts(text: str, kind: str) -> list[tuple[str, str]]:
    texts = [part.strip() for part in text.split(PART_SEPARATOR)]
    kinds = [part.strip().lower() for part in kind.split(PART_SEPARATOR)]
    out = []
    for index, part in enumerate(texts):
        if part:
            out.append((part, kinds[index] if index < len(kinds) else ""))
    return out


def locate_source(
    source: dict[str, Any],
    site: dict[str, Any],
    cache: dict[str, dict[str, Any]] | None = None,
    geocoder: Geocoder | None = None,
    live: bool = False,
) -> Located:
    """Where one source puts the facility, after the guards. `site` is the site searched.

    Coordinates and grid references are read here. An address is looked up in `cache`, and only a
    `live` call (the locate step) asks `geocoder` for one that isn't there, and records every part
    it reads in `cache`.
    """
    text = normalize_scalar(source.get("stated_location"))
    kind = normalize_scalar(source.get("stated_location_kind"))
    if not states_location(source):
        return Located(reason="no location stated")
    iso3 = normalize_scalar(site.get("country_iso3")).upper()
    names = [source.get("facility_name_in_source"), source.get("facility_name_english")]

    point: Located | None = None
    address: Located | None = None
    unread: list[str] = []
    for part, part_kind in _parts(text, kind):
        if part_kind in {COORDINATES, GRID_REFERENCE}:
            found = parse_point(part, iso3) or Located(reason=f"could not read the {part_kind}")
        else:
            # The agent's labels are unreliable, so coordinates labelled anything else still count.
            found = parse_point(part, iso3, strict=True)
        if found is not None:
            if live and cache is not None:
                _record(cache, part, iso3, found)
            if point is None or (point.point is None and found.point is not None):
                point = found
            continue
        if part_kind != STREET_ADDRESS:
            unread.append(
                "no location stated" if part_kind == "none"
                else f"a {part_kind} is not located" if part_kind
                else "not coordinates, a grid reference or a street address"
            )
            continue
        if address is not None:
            continue
        if PO_BOX.search(part):
            address = Located(reason="a PO box is not where the site is")
            continue
        sent = address_to_send(part, names)
        if not sent:
            address = Located(reason="the stated location is the facility's name, which is never geocoded")
            continue
        address = geocode_address(sent, iso3, cache, geocoder, live)

    if point is not None and point.point is not None:
        if point.basis in {COORDINATES, GRID_REFERENCE, HINT} and _host_is(source.get("url"), POSTCODE_CENTROID_SITES):
            point = Located(reason="wastebook.co.uk's coordinates are postcode centroids")
        elif point.basis in {COORDINATES, HINT} and _copied(point, site):
            point = Located(reason="copied from the prompt")
    located_address = address is not None and address.basis in MATCHING_BASES
    if point is not None and point.point is not None:
        if located_address and point.basis == HINT:
            # Coarse coordinates say less than an address placed on its street or building.
            return address
        if located_address:
            # The coordinates win, unless the address puts the site somewhere else entirely.
            km = haversine_km(*point.point, *address.point)
            if km > DOUBTFUL_ADDRESS_KM:
                return Located(
                    reason=f"doubtful: its address lands {km:.1f} km from its coordinates",
                    geocoder=address.geocoder,
                )
        return point
    if address is not None and (address.point is not None or point is None):
        return address
    if point is not None:
        return point
    return Located(reason="; ".join(dict.fromkeys(unread)) or "no location stated")


def describe_bases(located: list[Located]) -> str:
    """Counts by basis, most useful first: "coordinates 3, street 1, hint 2, none 4", or "0"."""
    return count_bases([item.basis for item in located])


def count_bases(bases: list[str]) -> str:
    counts = {basis: sum(1 for item in bases if item == basis) for basis in BASES}
    parts = [f"{basis} {count:,}" for basis, count in counts.items() if count]
    return ", ".join(parts) if parts else "0"


def describe_geocoders(entries: dict[str, dict[str, Any]], located: bool) -> str:
    """Which geocoder answered a run's addresses, for Run_Config. `located` says the locate step
    wrote source_locations.json at all."""
    names = sorted({normalize_scalar(entry.get("geocoder")) for entry in entries.values()} - {""})
    if names:
        return ", ".join(names)
    if any(entry.get("reason") == NO_GEOCODER for entry in entries.values()):
        return "none configured"
    return "none needed (no address stated)" if located else "locate step not run"
