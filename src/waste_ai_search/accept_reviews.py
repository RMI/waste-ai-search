"""Load what SMEs approve on Site_Review: the site-source pairs, and the key values under them.

Step 4 of the approve-sources plan (docs/plans/2026-10-21-approve-sources-site-by-site.md), as the
key-values plan (docs/plans/2026-10-26-smes-check-key-values.md) changed it. Every value the search
found is in found_values.csv, under the site-source pair (review_id) its status was decided for.
accept-reviews reads the SMEs' decisions back from the reviewed workbook's Site_Review tab: a source
row's by review_id, and a value row's by review_id and check. Each source decision applies to its
pair's values:

| Decision | What loads |
|---|---|
| Correct site | Its values, at the site searched |
| Different site, with other_site_id | Its values, at that site, where that site's fields are empty |
| New site | Nothing. The source is listed in new_facility_candidates.csv, once |
| Junk | Nothing |
| none | Nothing yet |

A key value of a Tier 3-5 pair (schema.VALUE_CHECKED_ATTRIBUTES), and an operator the agent marked
Inferred or Conflicting from a pair of any tier, also need their own row's decision: Correct loads
the value as found; Wrong with a corrected_value loads that instead; Wrong alone, or no decision,
loads nothing. A corrected_value with no decision is read as Wrong. A pair giving one such value
only has no row for it: the pair's own Correct site or Different site is Correct for the value too,
or Wrong with its corrected_value where one is typed. A Tier 1-2 pair's other values are trusted as
read.

A pair's values are those that would load at the site searched. A Different site also weighs again,
at the other site, the values set aside only because they confirm or would change one we hold at the
site searched, since that says nothing of the other site: the SME has said the source is about it.
A value among those that would need a row of its own has none, so it doesn't load. Only fills of
empty fields load, there as anywhere.

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
from dataclasses import dataclass, field
from datetime import datetime, time
from itertools import combinations
from pathlib import Path
from typing import Any

from .arbitration import baseline_for, site_for_year
from .costs import RUN_LOG_FILE
from .arbitration import decision_key, same_value
from .credibility import TIER_5, TIER_BY_LABEL
from .found_values import (
    AUTO_APPROVED,
    EXCEPTION_KM,
    FOUND_VALUES_FILE,
    LOADABLE,
    SME_REVIEW,
    judged_on_our_value,
    needs_value_check,
    pick_values,
)
from .input_loader import SEED_NAME, load_sites, write_csv_records
from .lists import FOUND_BY_SME, NEW_FACILITY_FILE, NEW_FACILITY_HEADERS, Site, SiteIndex, candidate_id, id_order, plain
from .locate import haversine_km, seed_point, stated_location_text
from .normalize_values import normalize_value
from .payload import normalize_url
from .prompt_builder import needs_contradiction_check
from .schema import (
    ACCEPTED_VALUES_HEADERS,
    CORRECT,
    CORRECT_SITE,
    DIFFERENT_SITE,
    FOUND_VALUES_HEADERS,
    GAP_FILL_ATTRIBUTES,
    JUNK,
    NEW_SITE,
    NOT_A_WASTE_FACILITY,
    SITE_REVIEW_DECISIONS,
    SITE_REVIEW_EDITABLE,
    VALUE_DECISIONS,
    WRONG,
    is_blank,
    normalize_scalar,
)
from .site_review import SITE_REVIEW_SHEET, check_label, checked_on_page, would_load


ACCEPTED_VALUES_FILE = "accepted_values.csv"

# accepted_values.csv's approved_by for a value from a pair auto-approved by a run arbitrated before
# the key-values plan, and for an SME's decision with no reviewer named.
AUTO = "auto"
SME = "SME"
# What found_values.csv's loaded says of a value accepted to load: written to accepted_values.csv,
# or set aside for another value picked for the same site, attribute and year.
LOADED = "yes"
NOT_LOADED = "no"
# The status pick_values is asked to pick among: values accepted to load, wherever they load.
ACCEPTED = "accepted"

# The Site_Review columns read back: the pair, which value a value row checks, its site, and what the
# SME fills in.
READ_COLUMNS = ["review_id", "check", "site_id", *SITE_REVIEW_EDITABLE]


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
        if record.get("check") and not checked_on_page(record["check"]):
            continue  # a value row (read_value_decisions)
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


@dataclass(frozen=True)
class ValueDecision:
    """An SME's decision on one key value, on its own Site_Review row under its pair."""

    review_id: str
    # Which value: check_label's "attribute" or "attribute (year)".
    check: str
    attribute: str
    # Correct or Wrong.
    decision: str
    # What the SME typed in corrected_value, and what it comes to as our database holds it.
    typed: str
    corrected: str
    row: int

    @property
    def key(self) -> tuple[str, str]:
        return self.review_id, self.check


# The years a typed closing_year may be.
YEARS = range(1800, 2101)


def read_value_decisions(workbook: Path) -> dict[tuple[str, str], ValueDecision]:
    """The value rows' decisions on a reviewed workbook's Site_Review tab, by (review_id, check). A
    row with neither a decision nor a corrected_value is left out: it isn't decided yet. A
    corrected_value with no decision is read as Wrong, since a value typed instead says the found one
    is wrong. A source row that checks its pair's one value (site_review.ON_PAGE_ROW) decides it too:
    Correct site or Different site is Correct, or Wrong with the corrected_value typed; any other
    decision, or none, decides nothing of the value.

    Raises ReviewError, naming every row at fault, where a decision isn't Correct or Wrong, or a
    corrected_value isn't a value the attribute can take - converted, mapped and range-checked as the
    search's own values are (normalize_values.py): not a number, a site type that isn't on the list,
    an efficiency above 1, or a closing_year that isn't a year.
    """
    decisions: dict[tuple[str, str], ValueDecision] = {}
    problems: list[str] = []
    by_text = {_folded(decision): decision for decision in VALUE_DECISIONS}
    for number, record in read_site_review(workbook):
        check = record.get("check", "")
        typed, corrected = record.get("decision", ""), record.get("corrected_value", "")
        if checked_on_page(check):
            if DECISION_BY_TEXT.get(_folded(typed)) not in (CORRECT_SITE, DIFFERENT_SITE):
                continue
            check, typed = checked_on_page(check), WRONG if corrected else CORRECT
        if not check or (not typed and not corrected):
            continue
        review = record.get("review_id", "")
        attribute = check.split(" (", 1)[0]
        where = f"{SITE_REVIEW_SHEET} row {number} ({check})"
        decision = by_text.get(_folded(typed), "") if typed else WRONG
        if not review:
            problems.append(f"{where}: it is filled in but has no review_id, so it can't be matched to a value.")
            continue
        if not decision:
            problems.append(f"{where}: {typed!r} is not a decision for a value. Pick {' or '.join(VALUE_DECISIONS)}.")
            continue
        value = ""
        if corrected:
            normal = normalize_value(attribute, corrected, "")
            problem = "; ".join(normal.warnings) if not normal.usable else ""
            if normal.usable and any("dropped" in warning for warning in normal.warnings):
                problem = "; ".join(normal.warnings)
            if normal.usable and attribute == "closing_year" and not (str(normal.value).isdigit() and int(normal.value) in YEARS):
                problem = f"closing_year {corrected!r} is not a year"
            if problem:
                problems.append(f"{where}: corrected_value {corrected!r} can't be used: {problem.rstrip('.')}.")
                continue
            value = normalize_scalar(normal.value)
        decisions[(review, check)] = ValueDecision(review, check, attribute, decision, corrected, value, number)
    if problems:
        raise ReviewError(
            f"Some value decisions in {workbook} can't be read back. Correct these rows and run accept-reviews again:\n"
            + "\n".join(f"  {problem}" for problem in problems)
        )
    return decisions


def read_value_rows(workbook: Path) -> set[tuple[str, str]]:
    """Every value row on a reviewed workbook's Site_Review tab, decided or not, by (review_id, check).
    A workbook made before the run was arbitrated again can lack a row the run now checks a value on."""
    return {
        (record.get("review_id", ""), checked_on_page(record["check"]) or record["check"])
        for _number, record in read_site_review(workbook) if record.get("check")
    }


def checked_value(row: dict[str, Any]) -> bool:
    """Whether a found value is one an SME checks on its own row (found_values.needs_value_check), so
    loads only with an SME's Correct, or corrected value, there: a key value from a Tier 3-5 pair, or
    an operator the agent marked Inferred or Conflicting. Worked out again from the row, rather than
    read from its value_check, so a value weighed again under Different site needs one too."""
    tier = TIER_BY_LABEL.get(normalize_scalar(row.get("source_tier")), TIER_5)
    return needs_value_check(row.get("attribute_name"), tier, row.get("value_basis")) and row.get("status") != AUTO_APPROVED


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
    # Different site decisions applied that are worth a second look (other_site_warnings).
    warnings: list[str] = field(default_factory=list)
    # Value rows' decisions on values found_values.csv doesn't have, as for `unmatched`.
    unmatched_values: list[ValueDecision] = field(default_factory=list)
    # Key values under an approved pair with no decision of their own yet.
    undecided_values: int = 0
    # Of those, the ones whose row the workbook doesn't have, so no SME can decide them there.
    missing_value_rows: int = 0
    # Value decisions worth a second look (value_warnings).
    value_warnings: list[str] = field(default_factory=list)


def accept(
    found: list[dict[str, Any]],
    decisions: dict[str, Decision],
    sites_by_id: dict[str, dict[str, Any]],
    value_decisions: dict[tuple[str, str], ValueDecision] | None = None,
    value_rows: set[tuple[str, str]] | None = None,
) -> Acceptance:
    """Apply the decisions to found_values.csv's rows, filling in each row's sme_decision,
    value_decision, corrected_value and loaded, and pick the one value to load for each site,
    attribute and year. `found` is changed in place. `value_rows` are the workbook's value rows
    (read_value_rows), to tell a checked value not decided yet from one with no row to decide it on.

    loaded is "yes" for the value written to accepted_values.csv, "no" for a value accepted to load
    that lost to another, and blank for a value not accepted.
    """
    value_decisions = value_decisions or {}
    pairs: dict[str, list[dict[str, Any]]] = {}
    accepted: list[dict[str, Any]] = []
    matched: set[tuple[str, str]] = set()
    undecided_values = missing_value_rows = 0
    for index, row in enumerate(found):
        row["sme_decision"] = row["loaded"] = row["value_decision"] = row["corrected_value"] = ""
        review = row.get("review_id", "")
        if not review:
            continue
        pairs.setdefault(review, []).append(row)
        decision = decisions.get(review)
        if decision is not None:
            row["sme_decision"] = decision.label
        checked = checked_value(row)
        # A checked value has a row of its own only where it would load at the site searched.
        on_its_row = value_decisions.get((review, check_label(row))) if checked and row["status"] in LOADABLE else None
        if on_its_row is not None:
            matched.add(on_its_row.key)
            row["value_decision"], row["corrected_value"] = on_its_row.decision, on_its_row.typed
        loads_at = destination(row, decision, sites_by_id)
        if loads_at and checked:
            if on_its_row is None:
                if row["status"] in LOADABLE:
                    undecided_values += 1
                    # A workbook made before the run was arbitrated again can lack the row.
                    missing_value_rows += value_rows is not None and (review, check_label(row)) not in value_rows
                loads_at = ""
            elif on_its_row.decision == WRONG and not on_its_row.corrected:
                loads_at = ""
        if loads_at:
            value = on_its_row.corrected if on_its_row is not None and on_its_row.decision == WRONG else row["value"]
            accepted.append({
                **row, "site_id": loads_at, "value": value, "status": ACCEPTED, "_found": index,
                "_decision": decision, "_value_decision": on_its_row,
            })

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
        unmatched_values=[value for key, value in value_decisions.items() if key not in matched],
        undecided_values=undecided_values,
        missing_value_rows=missing_value_rows,
        value_warnings=value_warnings(value_decisions, matched, decisions, accepted),
    )


def value_warnings(
    value_decisions: dict[tuple[str, str], ValueDecision],
    matched: set[tuple[str, str]],
    decisions: dict[str, Decision],
    accepted: list[dict[str, Any]],
) -> list[str]:
    """The value decisions worth a second look, though none stops accept-reviews: one under a source
    that is Junk, New site or not decided, so it doesn't load (yet); a Correct with a corrected_value,
    which is ignored; and two different values for one field both marked Correct, of which one loads.
    Two spellings of one operator's name are one value (arbitration.same_value)."""
    warnings = []
    for value in sorted(value_decisions.values(), key=lambda value: value.row):
        if value.key not in matched:
            continue
        where = f"{SITE_REVIEW_SHEET} row {value.row} ({value.check})"
        pair = decisions.get(value.review_id)
        if pair is None:
            warnings.append(f"{where}: its source has no decision yet, so it loads nothing until it has one.")
        elif pair.decision in (NEW_SITE, JUNK):
            warnings.append(f"{where}: its source is {pair.decision}, so it loads nothing.")
        if value.decision == CORRECT and value.typed:
            warnings.append(f"{where}: it is marked Correct, so its corrected_value {value.typed!r} is ignored.")
    by_field: dict[tuple[str, str, Any], list[dict[str, Any]]] = {}
    for row in accepted:
        if row["_value_decision"] is not None:
            by_field.setdefault(decision_key(row["site_id"], row["attribute_name"], int(row["year"])), []).append(row)
    for (site_id, attribute, _year), rows in by_field.items():
        if any(not same_value(one["value"], other["value"], attribute) for one, other in combinations(rows, 2)):
            listed = ", ".join(f"row {row['_value_decision'].row} ({row['value']})" for row in rows)
            warnings.append(
                f"{SITE_REVIEW_SHEET}: different values for {attribute} at site {site_id} are all approved "
                f"({listed}); the best tier, then the newest, loads."
            )
    return warnings


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


def other_site_warnings(
    decisions: dict[str, Decision],
    found: list[dict[str, Any]],
    sites_by_id: dict[str, dict[str, Any]],
    searched: set[str],
) -> list[str]:
    """The Different site decisions worth a second look, though each can be right: the other site is
    in another country than the site searched, the run didn't search it, or the source places the
    facility more than EXCEPTION_KM from it. The seed holds our whole database, so a mistyped
    other_site_id is still a site of ours, and its values would load there unremarked. And values
    loaded at a site another run searched reach ingestion from two runs for one facility."""
    points: dict[str, tuple[float, float]] = {}
    for row in found:
        point = _point(row.get("source_coordinates"))
        if row.get("review_id") and point[0] and row["review_id"] not in points:
            points[row["review_id"]] = (float(point[0]), float(point[1]))
    warnings = []
    for decision in sorted(decisions.values(), key=lambda decision: decision.row):
        if decision.decision != DIFFERENT_SITE:
            continue
        searched_site, other = sites_by_id.get(decision.site_id, {}), sites_by_id.get(decision.other_site_id, {})
        named = f"other_site_id {decision.other_site_id} ({normalize_scalar(other.get('site_name'))})"
        where = f"{SITE_REVIEW_SHEET} row {decision.row} ({decision.review_id}): {named}"
        country, theirs = normalize_scalar(searched_site.get("country_iso3")), normalize_scalar(other.get("country_iso3"))
        if country and theirs and country != theirs:
            warnings.append(f"{where} is in {theirs}, but site {decision.site_id}, whose search cited the source, is in {country}.")
        if decision.other_site_id not in searched:
            warnings.append(f"{where} wasn't searched in this run, so ingestion may get its values from another run too.")
        source, ours = points.get(decision.review_id), seed_point(other)
        if source is not None and ours is not None and haversine_km(*source, *ours) > EXCEPTION_KM:
            warnings.append(f"{where} is {haversine_km(*source, *ours):,.0f} km from where the source places the facility.")
    return warnings


def searched_sites(run_dir: Path, found: list[dict[str, Any]]) -> set[str]:
    """The sites the run searched: its run log's, and any site found_values.csv lists."""
    sites = {normalize_scalar(row.get("site_id")) for row in found}
    log = run_dir / RUN_LOG_FILE
    if log.exists():
        with log.open(newline="", encoding="utf-8-sig") as handle:
            sites |= {normalize_scalar(row.get("site_id")) for row in csv.DictReader(handle)}
    return sites - {""}


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
    value_decisions = read_value_decisions(workbook)

    with found_path.open(newline="", encoding="utf-8-sig") as handle:
        reader = csv.DictReader(handle)
        found = [dict(row) for row in reader]
        headers = list(reader.fieldnames or FOUND_VALUES_HEADERS)
    acceptance = accept(found, decisions, sites_by_id, value_decisions, read_value_rows(workbook))
    acceptance.warnings = other_site_warnings(decisions, found, sites_by_id, searched_sites(run_dir, found))

    write_csv_records(run_dir / ACCEPTED_VALUES_FILE, acceptance.values, ACCEPTED_VALUES_HEADERS)
    written = ("sme_decision", "value_decision", "corrected_value", "loaded")
    write_csv_records(found_path, found, [*headers, *(h for h in written if h not in headers)])
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
    if acceptance.warnings:
        print(
            f"!! {len(acceptance.warnings)} thing(s) to check in Different site decisions, which were applied. "
            "Correct other_site_id and run accept-reviews again if one is wrong:"
        )
        for warning in acceptance.warnings:
            print(f"  {warning}")
    checked = Counter(row.get("value_decision") for row in found if row.get("value_decision"))
    corrected = sum(1 for row in found if row.get("value_decision") == WRONG and row.get("corrected_value"))
    print(
        f"Key values decided: {sum(checked.values())} (Correct {checked[CORRECT]}, Wrong {checked[WRONG]}, "
        f"{corrected} of them corrected); waiting under approved sources: {acceptance.undecided_values}"
    )
    if acceptance.missing_value_rows:
        print(
            f"!! {acceptance.missing_value_rows} value(s) waiting under approved sources have no ↳ row in "
            f"{workbook.name}: the run was arbitrated again after the workbook was made. They load nothing "
            "until they are decided on a workbook that has their rows."
        )
    if acceptance.unmatched_values:
        rows = ", ".join(str(value.row) for value in acceptance.unmatched_values)
        print(
            f"!! {len(acceptance.unmatched_values)} value decision(s) are on values {FOUND_VALUES_FILE} no "
            f"longer has (Site_Review rows {rows}). They were not applied."
        )
    if acceptance.value_warnings:
        print(f"!! {len(acceptance.value_warnings)} thing(s) to check in value decisions:")
        for warning in acceptance.value_warnings:
            print(f"  {warning}")
    sites = len({row["site_id"] for row in acceptance.values})
    print(f"Values to load: {len(acceptance.values)} for {sites} site(s), in {ACCEPTED_VALUES_FILE}")
    lost = sum(1 for row in found if row.get("loaded") == NOT_LOADED)
    if lost:
        print(f"Accepted values not loaded, as another was picked for the same site, attribute and year: {lost}")
    print(f"New sites added to {NEW_FACILITY_FILE}: {added} ({listed} listed in all)")
