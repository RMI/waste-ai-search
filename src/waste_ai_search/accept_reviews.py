"""Load what is approved: the auto-approved site-source pairs, and the pairs SMEs approve on Site_Review.

Step 4 of the approve-sources plan (docs/plans/2026-10-21-approve-sources-site-by-site.md). Every
value the search found is in found_values.csv, under the site-source pair (review_id) its status was
decided for. accept-reviews reads the SMEs' decisions back from the reviewed workbook's Site_Review
tab, by review_id, and applies each to its pair's values:

| Decision | What loads |
|---|---|
| none, and the pair is auto-approved | Its values, at the site searched |
| Correct site | Its values, at the site searched |
| Different site, with other_site_id | Its values, at that site, where that site's fields are empty |
| New site | Nothing. The source is listed in new_facility_candidates.csv, once |
| Junk | Nothing |
| none, and the pair is for SME review | Nothing yet |

A pair's values are those that would load at the site searched, auto-approved or for SME review. A
Different site also weighs again, at the other site, the values set aside only because they confirm
or would change one we hold at the site searched, since that says nothing of the other site: the
SME has said the source is about it. Only fills of empty fields load, there as anywhere.

Where the values accepted for one site, attribute and year (for a yearly attribute) disagree, one is
picked (found_values.pick_values): the best tier, then the newest, then the value the most sources
give, then the agent's order.

It writes accepted_values.csv, which ingestion loads; found_values.csv's sme_decision and loaded; and
the SMEs' new sites to new_facility_candidates.csv. It can run before review is finished - a pair not
decided yet loads nothing until it runs again - and running it again on the same decisions writes the
same files. The workbook is only read.
"""
from __future__ import annotations

import csv
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, time
from pathlib import Path
from typing import Any

from .arbitration import baseline_for, site_for_year
from .found_values import AUTO_APPROVED, FOUND_VALUES_FILE, LOADABLE, SME_REVIEW, judged_on_our_value, pick_values
from .input_loader import SEED_NAME, load_sites, write_csv_records
from .lists import FOUND_BY_SME, NEW_FACILITY_FILE, NEW_FACILITY_HEADERS, Site, SiteIndex, candidate_id, id_order, plain
from .locate import stated_location_text
from .payload import normalize_url
from .prompt_builder import needs_contradiction_check
from .schema import (
    ACCEPTED_VALUES_HEADERS,
    CORRECT_SITE,
    DIFFERENT_SITE,
    FOUND_VALUES_HEADERS,
    GAP_FILL_ATTRIBUTES,
    NEW_SITE,
    NOT_A_WASTE_FACILITY,
    SITE_REVIEW_DECISIONS,
    SITE_REVIEW_EDITABLE,
    is_blank,
    normalize_scalar,
)
from .site_review import SITE_REVIEW_SHEET, would_load


ACCEPTED_VALUES_FILE = "accepted_values.csv"

# accepted_values.csv's approved_by for a value from an auto-approved pair, and for an SME's
# decision with no reviewer named.
AUTO = "auto"
SME = "SME"
# What found_values.csv's loaded says of a value accepted to load: written to accepted_values.csv,
# or set aside for another value picked for the same site, attribute and year.
LOADED = "yes"
NOT_LOADED = "no"
# The status pick_values is asked to pick among: values accepted to load, wherever they load.
ACCEPTED = "accepted"

# The Site_Review columns read back: the pair, its site, and what the SME fills in.
READ_COLUMNS = ["review_id", "site_id", *SITE_REVIEW_EDITABLE]


class ReviewError(ValueError):
    """A reviewed workbook whose decisions can't be read back as they stand."""


def reviewed_workbook(run_dir: Path, run_id: str, review_dir: Path | None) -> Path:
    """Where the SMEs' copy of a run's workbook is: the SharePoint folder's, else the run folder's."""
    name = f"{run_id}_review.xlsx"
    if review_dir is not None and (review_dir / run_id / name).is_file():
        return review_dir / run_id / name
    return run_dir / name


# --- reading the decisions -----------------------------------------------------------------------
@dataclass(frozen=True)
class Decision:
    """An SME's decision on one site-source pair, as Site_Review records it."""

    review_id: str
    # The site the pair's search was for.
    site_id: str
    decision: str
    other_site_id: str
    reviewer: str
    reviewed_date: str
    notes: str
    # The row it is on, as Excel numbers them.
    row: int

    @property
    def label(self) -> str:
        """As found_values.csv's sme_decision gives it: a Different site names the site."""
        return f"{self.decision} ({self.other_site_id})" if self.other_site_id else self.decision

    @property
    def approved_by(self) -> str:
        return self.reviewer or SME


def _folded(text: Any) -> str:
    """A decision as typed: any case or spacing, and a hyphen, or the en or em dash Word or Excel may
    turn one into, read as a space."""
    text = normalize_scalar(text)
    for dash in "-–—":
        text = text.replace(dash, " ")
    return " ".join(text.casefold().split())


DECISION_BY_TEXT = {_folded(decision): decision for decision in SITE_REVIEW_DECISIONS}


def _text(value: Any) -> str:
    """A cell as text. Excel keeps a typed site id as a number, which can come back as 13338.0, and a
    typed date as a date and time."""
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    if isinstance(value, datetime) and value.time() == time(0):
        return value.date().isoformat()
    return normalize_scalar(value)


def read_site_review(workbook: Path) -> list[tuple[int, dict[str, str]]]:
    """Site_Review's rows as text, each with its row number as Excel shows it. Opened read-only."""
    path = Path(workbook)
    if not path.is_file():
        raise FileNotFoundError(f"No reviewed workbook at {path}.")
    from openpyxl import load_workbook

    book = load_workbook(path, read_only=True, data_only=True)
    try:
        if SITE_REVIEW_SHEET not in book.sheetnames:
            raise ReviewError(
                f"{path} has no {SITE_REVIEW_SHEET} tab: it was arbitrated before SMEs decided site-source "
                "pairs. Arbitrate the run again to make one."
            )
        rows = book[SITE_REVIEW_SHEET].iter_rows(values_only=True)
        headers = [normalize_scalar(value) for value in next(rows, ())]
        missing = [column for column in READ_COLUMNS if column not in headers]
        if missing:
            raise ReviewError(f"{SITE_REVIEW_SHEET} in {path} has no {', '.join(missing)} column.")
        return [
            (number, dict(zip(headers, (_text(value) for value in row))))
            for number, row in enumerate(rows, start=2)
            if any(value not in (None, "") for value in row)
        ]
    finally:
        book.close()


def read_decisions(workbook: Path, site_ids: set[str]) -> dict[str, Decision]:
    """The decisions on a reviewed workbook's Site_Review tab, by review_id. A row with no decision is
    left out: its pair isn't decided yet.

    Raises ReviewError, naming every row at fault, where a decision can't be read as it stands: one
    that isn't one of the four; a Different site with no other_site_id, with one that isn't a site in
    the run's seed (`site_ids`), or with the row's own site; an other_site_id beside any other
    decision; or two rows deciding one pair two ways. Nothing is guessed, since loading a pair's
    values under the wrong site is worse than stopping.
    """
    decisions: dict[str, Decision] = {}
    problems: list[str] = []
    for number, record in read_site_review(workbook):
        review = record.get("review_id", "")
        typed, other = record.get("decision", ""), record.get("other_site_id", "")
        if not typed and not other:
            continue
        where = f"{SITE_REVIEW_SHEET} row {number}" + (f" ({review})" if review else "")
        decision = DECISION_BY_TEXT.get(_folded(typed), "")
        if not review:
            problems.append(f"{where}: it is filled in but has no review_id, so it can't be matched to a source.")
        elif typed and not decision:
            problems.append(f"{where}: {typed!r} is not a decision. Pick one of {', '.join(SITE_REVIEW_DECISIONS)}.")
        elif decision == DIFFERENT_SITE and not other:
            problems.append(f"{where}: Different site needs the id of the site it is about in other_site_id.")
        elif decision == DIFFERENT_SITE and other not in site_ids:
            problems.append(f"{where}: other_site_id {other} is not a site in the run's seed.csv.")
        elif decision == DIFFERENT_SITE and other == record.get("site_id"):
            problems.append(f"{where}: other_site_id {other} is this row's own site. Pick Correct site instead.")
        elif decision != DIFFERENT_SITE and other:
            problems.append(
                f"{where}: other_site_id is {other}, but only Different site takes one, and the decision is "
                f"{decision or 'blank'}."
            )
        else:
            found = Decision(
                review_id=review,
                site_id=record.get("site_id", ""),
                decision=decision,
                other_site_id=other,
                reviewer=record.get("reviewer", ""),
                reviewed_date=record.get("reviewed_date", ""),
                notes=record.get("notes", ""),
                row=number,
            )
            earlier = decisions.setdefault(review, found)
            if earlier.label != found.label:
                problems.append(f"{where}: row {earlier.row} decides the same pair {earlier.label}, and this row {found.label}.")
    if problems:
        raise ReviewError(
            f"Some decisions in {workbook} can't be read back. Correct these rows and run accept-reviews again:\n"
            + "\n".join(f"  {problem}" for problem in problems)
        )
    return decisions


# --- applying them -------------------------------------------------------------------------------
@dataclass
class Acceptance:
    """What a run's found values and the SMEs' decisions come to."""

    # accepted_values.csv's rows.
    values: list[dict[str, Any]]
    # The SMEs' New site sources, as new_facility_candidates.csv lists them.
    new_sites: list[dict[str, Any]]
    # Decisions on pairs found_values.csv doesn't have: the run was arbitrated again since, and its
    # search no longer cites that page under that name.
    unmatched: list[Decision]
    # Pairs for SME review with no decision yet.
    undecided: int


def accept(
    found: list[dict[str, Any]], decisions: dict[str, Decision], sites_by_id: dict[str, dict[str, Any]]
) -> Acceptance:
    """Apply the decisions to found_values.csv's rows, filling in each row's sme_decision and loaded,
    and pick the one value to load for each site, attribute and year. `found` is changed in place.

    loaded is "yes" for the value written to accepted_values.csv, "no" for a value accepted to load
    that lost to another, and blank for a value not accepted.
    """
    pairs: dict[str, list[dict[str, Any]]] = {}
    accepted: list[dict[str, Any]] = []
    for index, row in enumerate(found):
        row["sme_decision"] = row["loaded"] = ""
        review = row.get("review_id", "")
        if not review:
            continue
        pairs.setdefault(review, []).append(row)
        decision = decisions.get(review)
        if decision is not None:
            row["sme_decision"] = decision.label
        loads_at = destination(row, decision, sites_by_id)
        if loads_at:
            accepted.append({**row, "site_id": loads_at, "status": ACCEPTED, "_found": index, "_decision": decision})

    picked = {id(row) for row in pick_values(accepted, statuses=(ACCEPTED,)).values()}
    values = []
    for row in accepted:
        found[row["_found"]]["loaded"] = LOADED if id(row) in picked else NOT_LOADED
        if id(row) in picked:
            values.append(accepted_row(row, sites_by_id))
    order = {attribute: index for index, attribute in enumerate(GAP_FILL_ATTRIBUTES)}
    values.sort(key=lambda row: (
        id_order(row["site_id"]), order.get(row["attribute_name"], len(order)), -int(row["year"]), row["review_id"],
    ))

    return Acceptance(
        values=values,
        new_sites=new_site_candidates(pairs, decisions, sites_by_id),
        unmatched=[decision for review, decision in decisions.items() if review not in pairs],
        undecided=sum(
            1 for review, rows in pairs.items()
            if review not in decisions and any(row["status"] == SME_REVIEW for row in rows)
        ),
    )


def destination(row: dict[str, Any], decision: Decision | None, sites_by_id: dict[str, dict[str, Any]]) -> str:
    """The site a found value loads at, or "" where it doesn't load."""
    if is_blank(row.get("value")):
        return ""
    if decision is None:
        # An auto-approved pair loads unless an SME decided otherwise; one for review waits.
        return row["site_id"] if row["status"] == AUTO_APPROVED else ""
    if decision.decision == CORRECT_SITE:
        return row["site_id"] if row["status"] in LOADABLE else ""
    if decision.decision == DIFFERENT_SITE and (row["status"] in LOADABLE or judged_on_our_value(row)):
        other = sites_by_id.get(decision.other_site_id, {})
        return decision.other_site_id if fills_empty_field(row, other) else ""
    return ""


def fills_empty_field(row: dict[str, Any], site: dict[str, Any]) -> bool:
    """Whether a found value fills a field that is empty at `site` for the value's year, read as
    found_values.py reads it at the site searched. A Not a Waste Facility verdict counts only at a
    site that would be offered it, one nothing independently confirms (WP-525)."""
    attribute = row["attribute_name"]
    site_then, _note = site_for_year(site, attribute, int(row["year"]))
    if not is_blank(baseline_for(site_then, attribute)):
        return False
    says_not_a_facility = attribute == "facility_type" and normalize_scalar(row.get("value")) == NOT_A_WASTE_FACILITY
    return not says_not_a_facility or needs_contradiction_check(site)


def accepted_row(row: dict[str, Any], sites_by_id: dict[str, dict[str, Any]]) -> dict[str, Any]:
    """One value as accepted_values.csv lists it, at the site it loads at."""
    site = sites_by_id.get(row["site_id"], {})
    decision: Decision | None = row["_decision"]
    return {
        "run_id": row.get("run_id", ""),
        "site_id": row["site_id"],
        "site_name": normalize_scalar(site.get("site_name")),
        "country_iso3": normalize_scalar(site.get("country_iso3")),
        "attribute_name": row["attribute_name"],
        "value": row["value"],
        "unit": row.get("unit", ""),
        "year": row["year"],
        "source_url": row.get("source_url", ""),
        "source_tier": row.get("source_tier", ""),
        "approved_by": decision.approved_by if decision is not None else AUTO,
        "reviewed_date": decision.reviewed_date if decision is not None else "",
        "review_id": row["review_id"],
    }


# --- new sites -----------------------------------------------------------------------------------
def new_site_candidates(
    pairs: dict[str, list[dict[str, Any]]], decisions: dict[str, Decision], sites_by_id: dict[str, dict[str, Any]]
) -> list[dict[str, Any]]:
    """new_facility_candidates.csv's rows for the sources SMEs marked New site: one per source - one
    page under one facility name - whichever of our sites' searches cited it, with what it gives."""
    out: dict[str, dict[str, Any]] = {}
    for review, decision in decisions.items():
        rows = pairs.get(review)
        if decision.decision != NEW_SITE or not rows:
            continue
        first = rows[0]
        name = first.get("facility_name_in_source") or first.get("facility_name_english")
        key = candidate_id(FOUND_BY_SME, normalize_url(first.get("source_url")), plain(name))
        latitude, longitude = _point(first.get("source_coordinates"))
        candidate = out.setdefault(key, {
            "candidate_id": key,
            "found_by": FOUND_BY_SME,
            "country_iso3": first.get("country_iso3", ""),
            "source_urls": first.get("source_url", ""),
            "facility_names_in_source": first.get("facility_name_in_source", ""),
            "facility_names_english": first.get("facility_name_english", ""),
            "stated_locations": stated_location_text(
                {"stated_coordinates": first.get("stated_coordinates"), "stated_address": first.get("source_address")}
            ),
            "latitude": latitude,
            "longitude": longitude,
            "location_basis": first.get("coordinates_from", "") if latitude else "",
            "nearest_site_id": "",
            "nearest_site_name": "",
            "nearest_site_km": "",
            "_sites": [],
            "_notes": [],
        })
        site_id = first["site_id"]
        candidate["_sites"].append(site_id)
        site_name = normalize_scalar(sites_by_id.get(site_id, {}).get("site_name")) or first.get("site_name", "")
        by = " on ".join(part for part in (decision.reviewer, decision.reviewed_date) if part)
        note = f"Marked New site by an SME{f' ({by})' if by else ''} under site {site_id} ({site_name}), whose search cited it."
        gives = would_load([
            row for row in rows
            if not is_blank(row.get("value")) and (row["status"] in LOADABLE or judged_on_our_value(row))
        ])
        if gives:
            note += f" It gives {gives.replace(chr(10), '; ')}."
        if decision.notes:
            note += f" {decision.notes}"
        candidate["_notes"].append(note)

    rows_out = []
    for candidate in out.values():
        rows_out.append({
            **{key: value for key, value in candidate.items() if not key.startswith("_")},
            "searched_site_ids": "; ".join(sorted(dict.fromkeys(candidate["_sites"]), key=id_order)),
            "note": " ".join(candidate["_notes"]),
        })
    return rows_out


def _point(text: Any) -> tuple[str, str]:
    """found_values.csv's source_coordinates, "lat, lon", as its two parts; blank where there are none."""
    parts = [part.strip() for part in normalize_scalar(text).split(",")]
    return (parts[0], parts[1]) if len(parts) == 2 and all(parts) else ("", "")


def list_new_sites(run_dir: Path, candidates: list[dict[str, Any]], sites: list[dict[str, Any]]) -> tuple[int, int]:
    """Add the SMEs' new sites to the run's new_facility_candidates.csv, once each. A source listed
    already gains any other site of ours whose search it was cited by. Returns how many were added,
    and how many are listed in all."""
    path = run_dir / NEW_FACILITY_FILE
    existing: list[dict[str, str]] = []
    if path.exists():
        with path.open(newline="", encoding="utf-8-sig") as handle:
            existing = [dict(row) for row in csv.DictReader(handle)]
    listed = {row.get("candidate_id"): row for row in existing}
    added = []
    for candidate in candidates:
        row = listed.get(candidate["candidate_id"])
        if row is None:
            added.append(candidate)
            continue
        searched = [site_id.strip() for site_id in normalize_scalar(row.get("searched_site_ids")).split(";") if site_id.strip()]
        more = [site_id for site_id in candidate["searched_site_ids"].split("; ") if site_id not in searched]
        if more:
            row["searched_site_ids"] = "; ".join(sorted([*searched, *more], key=id_order))
    add_nearest_sites(added, sites)
    write_csv_records(path, [*existing, *added], NEW_FACILITY_HEADERS)
    return len(added), len(existing) + len(added)


def add_nearest_sites(candidates: list[dict[str, Any]], sites: list[dict[str, Any]]) -> None:
    """Our site nearest each candidate that has a point, measured to its edge as lists.SiteIndex does."""
    located = [row for row in candidates if row["latitude"] and row["longitude"]]
    if not located:
        return
    index = SiteIndex([Site.from_seed(site) for site in sites])
    for row in located:
        try:
            point = (float(row["latitude"]), float(row["longitude"]))
        except ValueError:
            continue
        nearest = index.nearest(point)
        if nearest is not None:
            km, site = nearest
            row["nearest_site_id"], row["nearest_site_name"], row["nearest_site_km"] = site.site_id, site.name, f"{km:.1f}"


# --- the command ---------------------------------------------------------------------------------
def accept_reviews(run_dir: Path, workbook: Path) -> Acceptance:
    """Apply a reviewed workbook's decisions to a run: write accepted_values.csv, found_values.csv's
    sme_decision and loaded, and any new sites to new_facility_candidates.csv."""
    found_path = run_dir / FOUND_VALUES_FILE
    if not found_path.exists():
        raise FileNotFoundError(f"No {FOUND_VALUES_FILE} in {run_dir}: arbitrate the run first.")
    seed_path = run_dir / SEED_NAME
    if not seed_path.exists():
        raise FileNotFoundError(f"No {SEED_NAME} in {run_dir}: arbitrate the run first.")
    sites, _headers = load_sites(seed_path)
    sites_by_id = {normalize_scalar(site.get("site_id")): site for site in sites}
    decisions = read_decisions(workbook, set(sites_by_id))

    with found_path.open(newline="", encoding="utf-8-sig") as handle:
        reader = csv.DictReader(handle)
        found = [dict(row) for row in reader]
        headers = list(reader.fieldnames or FOUND_VALUES_HEADERS)
    acceptance = accept(found, decisions, sites_by_id)

    write_csv_records(run_dir / ACCEPTED_VALUES_FILE, acceptance.values, ACCEPTED_VALUES_HEADERS)
    write_csv_records(found_path, found, [*headers, *(h for h in ("sme_decision", "loaded") if h not in headers)])
    added, listed = list_new_sites(run_dir, acceptance.new_sites, sites)
    print_acceptance(acceptance, decisions, found, Path(workbook), added, listed)
    return acceptance


def print_acceptance(
    acceptance: Acceptance,
    decisions: dict[str, Decision],
    found: list[dict[str, Any]],
    workbook: Path,
    added: int,
    listed: int,
) -> None:
    counts = Counter(decision.decision for decision in decisions.values())
    breakdown = ", ".join(f"{name} {counts[name]}" for name in SITE_REVIEW_DECISIONS if counts[name])
    print(f"Decisions read from {workbook}: {len(decisions)}{f' ({breakdown})' if breakdown else ''}")
    print(f"Pairs for SME review not decided yet: {acceptance.undecided} (nothing of theirs loads until they are)")
    if acceptance.unmatched:
        rows = ", ".join(str(decision.row) for decision in acceptance.unmatched)
        print(
            f"!! {len(acceptance.unmatched)} decision(s) are on pairs {FOUND_VALUES_FILE} no longer has "
            f"(Site_Review rows {rows}): the run was arbitrated again since, and its search no longer cites "
            "that page under that name. They were not applied."
        )
    auto = sum(1 for row in acceptance.values if row["approved_by"] == AUTO)
    sites = len({row["site_id"] for row in acceptance.values})
    print(
        f"Values to load: {len(acceptance.values)} for {sites} site(s), {auto} auto-approved and "
        f"{len(acceptance.values) - auto} approved by an SME, in {ACCEPTED_VALUES_FILE}"
    )
    lost = sum(1 for row in found if row.get("loaded") == NOT_LOADED)
    if lost:
        print(f"Accepted values not loaded, as another was picked for the same site, attribute and year: {lost}")
    print(f"New sites added to {NEW_FACILITY_FILE}: {added} ({listed} listed in all)")
