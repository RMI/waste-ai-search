"""How often a source's page states a location the agent didn't copy.

Measures whether a second pass is worth building: a model reading each cited page for the
facility's address or coordinates, which the search agent leaves out when Bing gave it only part of
the page, or it didn't look. For each page whose text the link check kept (page_text/), it looks for
a location in the text, and compares that with what the agent stated for the page:

| Kind | What counts |
|---|---|
| coordinates | A latitude and longitude, decimal or degrees-minutes-seconds, within MAX_KM of a site whose search cited the page |
| grid reference | A UK or Irish grid reference, the same way, for a UK or Irish site |
| street address | A civic number and up to four words before a word for a road ("12 Otaihanga Road"), or an Ontario lot and concession |
| postcode | Only a postcode of the site's country: UK, Canada, Australia (state and four digits) or Ireland's Eircode |

It is a heuristic and counts an upper bound: an address may be the operator's office, and a pair of
numbers in a table may not be coordinates, though the distance filter rules most such pairs out.
Spot-check the pages it lists before deciding on anything.
"""
from __future__ import annotations

import csv
import json
import re
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .link_check import LINK_CHECK_FILE
from .locate import COORDINATES, GRID_REFERENCE, MATCHING_BASES, haversine_km, parse_point, seed_point
from .page_text import PAGE_TEXT_DIR, read_page_text
from .schema import normalize_scalar


STREET_ADDRESS = "street address"
POSTCODE = "postcode"
# Strongest first: a page is counted under the strongest kind it states.
KINDS = [COORDINATES, GRID_REFERENCE, STREET_ADDRESS, POSTCODE]

# Coordinates on a page count only within this distance of a site whose search cited it, so a pair
# of figures in a table, or another country's facility, isn't counted.
MAX_KM = 300.0
# How many candidate numbers are tried on one page: a register can hold thousands of rows.
MAX_CANDIDATES = 5000
# How much of the page around a finding is shown, for a spot check.
SNIPPET_CHARS = 140

_NUMBER = re.compile(r"-?\d{1,3}(?:[.,]\d{3,}|\s*°)")
_OS_GRID = re.compile(r"(?<![A-Za-z])[HJNOST][A-HJ-Z]\s?\d{3,5}\s?\d{3,5}(?!\d)")
_IRISH_GRID = re.compile(r"(?<![A-Za-z])[A-HJ-Z]\s?\d{3,5}\s?\d{3,5}(?!\d)")
_STREET_WORDS = (
    "road|rd|street|st|avenue|ave|highway|hwy|drive|dr|lane|ln|way|boulevard|blvd|crescent|cres|"
    "court|ct|place|pl|parkway|pkwy|terrace|tce|close|line|sideroad|trail|route|rte|chemin|rue|rang"
)
_STREET = re.compile(
    rf"(?<![\w.,])\d{{1,5}}[A-Za-z]?(?:-\d{{1,5}})?\s+(?:[^\W\d_][\w'’.-]*\s+){{0,4}}(?:{_STREET_WORDS})\.?(?!\w)",
    re.IGNORECASE,
)
_LOT_CONCESSION = re.compile(r"\blot\s+\d+[A-Za-z]?\s*,?\s*(?:of\s+)?concession\s+\d+", re.IGNORECASE)
POSTCODES = {
    "GBR": re.compile(r"\b[A-Z]{1,2}\d[A-Z\d]?\s?\d[ABD-HJLNP-UW-Z]{2}\b"),
    "CAN": re.compile(r"\b[ABCEGHJ-NPRSTVXY]\d[ABCEGHJ-NPRSTV-Z]\s?\d[ABCEGHJ-NPRSTV-Z]\d\b"),
    "AUS": re.compile(r"\b(?:NSW|VIC|QLD|SA|WA|TAS|NT|ACT)\s+\d{4}\b"),
    "IRL": re.compile(r"\b[AC-FHKNPRTV-Y]\d{2}\s?[AC-FHKNPRTV-Y0-9]{4}\b"),
}

MISSED_HEADERS = [
    "source_id", "url", "source_type", "searched_site_ids", "found_kind", "found_text", "latitude",
    "longitude", "km_from_site", "agent_stated",
]


@dataclass
class Site:
    point: tuple[float, float] | None
    iso3: str


@dataclass
class Finding:
    kind: str
    text: str
    point: tuple[float, float] | None = None
    km: float | None = None


@dataclass
class PageReport:
    """One page whose text was kept: what the agent stated for it, and what the page itself states."""

    source_id: str
    url: str
    source_type: str
    searched: list[str]
    agent_stated: bool
    agent_usable: bool
    finding: Finding | None = None

    @property
    def missed(self) -> bool:
        """The page states coordinates, a grid reference or an address, and the agent stated none."""
        return not self.agent_stated and self.finding is not None and self.finding.kind != POSTCODE


@dataclass
class Measurement:
    pages_cited: int = 0
    verdicts: Counter = field(default_factory=Counter)
    text_kinds: Counter = field(default_factory=Counter)
    reports: list[PageReport] = field(default_factory=list)


def _snippet(text: str, start: int, end: int) -> str:
    left = max(0, start - SNIPPET_CHARS // 3)
    return re.sub(r"\s+", " ", text[left:left + SNIPPET_CHARS]).strip()


def _nearest_km(point: tuple[float, float], sites: list[Site]) -> float | None:
    distances = [haversine_km(*point, *site.point) for site in sites if site.point]
    return min(distances) if distances else None


def find_point(text: str, sites: list[Site], pattern: re.Pattern, kind: str) -> Finding | None:
    """The first point on the page, read the way the locate step reads one, near a site that cited
    it."""
    iso3 = next((site.iso3 for site in sites if site.iso3), "")
    for index, match in enumerate(pattern.finditer(text)):
        if index >= MAX_CANDIDATES:
            break
        # Coordinates are read from around the number, where their labels and hemispheres are; a
        # grid reference from itself, since its parser takes a stated location, not a sentence.
        window = match.group(0) if kind == GRID_REFERENCE else text[max(0, match.start() - 40):match.end() + 80]
        located = parse_point(window, iso3)
        if located is None or located.point is None:
            continue
        # Only what the locate step would use: a coarse pair is a hint, and a grid reference is
        # counted as one.
        if located.basis != kind:
            continue
        km = _nearest_km(located.point, sites)
        if km is not None and km <= MAX_KM:
            return Finding(kind, _snippet(text, match.start(), match.end()), located.point, km)
    return None


def find_location(text: str, sites: list[Site]) -> Finding | None:
    """The strongest kind of location the page states, if any."""
    finding = find_point(text, sites, _NUMBER, COORDINATES)
    if finding:
        return finding
    countries = {site.iso3 for site in sites}
    if countries & {"GBR", "IRL"}:
        finding = find_point(text, sites, _OS_GRID if "GBR" in countries else _IRISH_GRID, GRID_REFERENCE)
        if finding:
            return finding
    for pattern in (_STREET, _LOT_CONCESSION):
        match = pattern.search(text)
        if match:
            return Finding(STREET_ADDRESS, _snippet(text, match.start(), match.end()))
    for iso3 in sorted(countries):
        pattern = POSTCODES.get(iso3)
        match = pattern.search(text) if pattern else None
        if match:
            return Finding(POSTCODE, _snippet(text, match.start(), match.end()))
    return None


def _read_csv(path: Path) -> list[dict[str, str]]:
    if not path.exists():
        return []
    with path.open(newline="", encoding="utf-8-sig") as handle:
        return list(csv.DictReader(handle))


def measure(run_dir: Path) -> Measurement:
    """Measure one arbitrated run whose link check kept page text."""
    evidence = _read_csv(run_dir / "evidence.csv")
    if evidence and "stated_location" not in evidence[0]:
        raise SystemExit(
            f"{run_dir / 'evidence.csv'} predates stated locations: arbitrate the run again with this version first."
        )
    sources = {row["source_id"]: row for row in _read_csv(run_dir / "sources.csv")}
    sites: dict[str, Site] = {}
    for row in _read_csv(run_dir / "seed.csv"):
        site_id = normalize_scalar(row.get("site_id") or row.get("facility_id"))
        sites.setdefault(site_id, Site(seed_point(row), normalize_scalar(row.get("country_iso3") or row.get("iso3c_plus"))[:3].upper()))

    checked = {}
    link_path = run_dir / LINK_CHECK_FILE
    if link_path.exists():
        checked = json.loads(link_path.read_text(encoding="utf-8"))
    measurement = Measurement(pages_cited=len(checked))
    measurement.verdicts.update(record.get("verdict", "") for record in checked.values())
    text_files = {}
    for record in checked.values():
        if record.get("text_file"):
            text_files[record["text_file"].removesuffix(".txt.gz")] = record["text_file"]
            measurement.text_kinds[record.get("text_kind") or "other"] += 1

    by_source: dict[str, list[dict[str, str]]] = {}
    for row in evidence:
        by_source.setdefault(row.get("source_id", ""), []).append(row)
    text_dir = run_dir / PAGE_TEXT_DIR
    for source_id, rows in sorted(by_source.items()):
        name = text_files.get(source_id)
        text = read_page_text(text_dir, name) if name else None
        if not text:
            continue
        searched = sorted({row.get("searched_site_id") or row.get("site_id", "") for row in rows} - {""})
        stated = [row for row in rows if normalize_scalar(row.get("stated_location"))]
        source = sources.get(source_id, {})
        measurement.reports.append(PageReport(
            source_id=source_id,
            url=source.get("url", ""),
            source_type=source.get("source_type", ""),
            searched=searched,
            agent_stated=bool(stated),
            agent_usable=any(row.get("location_basis") in MATCHING_BASES for row in stated),
            finding=find_location(text, [sites[site_id] for site_id in searched if site_id in sites]),
        ))
    return measurement


def _share(part: int, whole: int) -> str:
    return f"{part:,} ({part / whole:.0%})" if whole else f"{part:,}"


def summary(measurement: Measurement) -> str:
    """The measurement in plain words, as the script prints it."""
    reports = measurement.reports
    lines = [
        f"Pages cited: {measurement.pages_cited:,} "
        f"({', '.join(f'{verdict} {count:,}' for verdict, count in measurement.verdicts.most_common())})",
        f"Pages with text kept: {sum(measurement.text_kinds.values()):,} "
        f"({', '.join(f'{kind} {count:,}' for kind, count in measurement.text_kinds.most_common())})",
        f"Pages measured (text kept, cited in evidence.csv): {len(reports):,}",
        f"  The agent stated a location for: {_share(sum(1 for r in reports if r.agent_stated), len(reports))}, "
        f"usable for matching: {sum(1 for r in reports if r.agent_usable):,}",
    ]
    for kind in KINDS:
        stating = [r for r in reports if r.finding and r.finding.kind == kind]
        silent = sum(1 for r in stating if not r.agent_stated)
        lines.append(f"  The page states {kind} (strongest kind): {len(stating):,}; the agent stated nothing for {silent:,}")
    missed = [r for r in reports if r.missed]
    lines.append(
        f"Missed: the page states coordinates, a grid reference or an address, and the agent stated "
        f"no location: {_share(len(missed), len(reports))}"
    )
    by_type = Counter(r.source_type or "unknown" for r in missed)
    totals = Counter(r.source_type or "unknown" for r in reports)
    if by_type:
        lines.append("  By source type (missed / measured):")
        lines.extend(f"    {kind}: {count:,} / {totals[kind]:,}" for kind, count in by_type.most_common())
    return "\n".join(lines)


def missed_rows(measurement: Measurement) -> list[dict[str, Any]]:
    """The missed pages, for a spot check."""
    rows = []
    for report in measurement.reports:
        if not report.missed:
            continue
        finding = report.finding
        rows.append({
            "source_id": report.source_id,
            "url": report.url,
            "source_type": report.source_type,
            "searched_site_ids": "; ".join(report.searched),
            "found_kind": finding.kind,
            "found_text": finding.text,
            "latitude": f"{finding.point[0]:.6f}" if finding.point else "",
            "longitude": f"{finding.point[1]:.6f}" if finding.point else "",
            "km_from_site": f"{finding.km:.1f}" if finding.km is not None else "",
            "agent_stated": "no",
        })
    return rows


def write_missed(path: Path, measurement: Measurement) -> int:
    rows = missed_rows(measurement)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=MISSED_HEADERS)
        writer.writeheader()
        writer.writerows(rows)
    return len(rows)


def measure_run(run_dir: Path) -> Measurement:
    """Measure a run, after checking it has what the measurement needs."""
    if not (run_dir / PAGE_TEXT_DIR).is_dir() or not any((run_dir / PAGE_TEXT_DIR).glob("*.txt.gz")):
        raise SystemExit(
            f"No page text in {run_dir}: run `uv run waste-ai-search check-links --run-id <run id>` "
            "to keep it (network), then arbitrate."
        )
    return measure(run_dir)
