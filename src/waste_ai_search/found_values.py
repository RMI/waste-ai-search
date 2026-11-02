"""Sort every value the search found: SME review or not used.

Step 2 of the approve-sources plan (docs/plans/2026-10-21-approve-sources-site-by-site.md), as the
key-values plan (docs/plans/2026-10-26-smes-check-key-values.md) changed it. The agent returns only
sources about the site searched, so every source is filed under that site, and an SME checks whether
each source is about our site. So a value's status is decided for its site-source pair: the site
searched, and the source, which is one exact page (payload.normalize_url) under one facility name,
compared without case, accents or punctuation (a blank name is one name). A page with good data on
three of our sites is three pairs, each decided on its own, and each loads only that site's values.

| Status | When |
|---|---|
| SME review | The value fills an empty field. It loads once an SME approves its pair |
| not used | Anything else, and status_reason says which: it confirms our value, would change one we hold, its link is dead or missing, it could not be converted or mapped, or the agent itself marked it an excluded candidate |

Nothing is approved automatically. A Tier 1-2 pair's values are trusted as read, so they load with
its match; a key value (schema.VALUE_CHECKED_ATTRIBUTES) from a Tier 3-5 pair is also checked by an
SME on its own row, and value_check says so ("required").

What once sent a Tier 1-2 pair to SMEs is now a warning on its row (Pair.exceptions):

- the same page and facility name are cited for two or more of our sites (possible_duplicates.csv
  lists them);
- the source places the facility more than EXCEPTION_KM from our site's coordinates. A source
  that gives no coordinates, or a site with none, is not checked, as in batch 1. Coordinates too
  coarse to use, or that disagree with the source's own address, are still checked (Pair.measure);
- the source says the site is "Not a Waste Facility".

An empty field is our value for that site, attribute and year, as arbitration.site_for_year reads
the seed, left blank. A value's year is the year its value_date starts with, else the run's year
(arbitration.value_year). Only fills of empty fields load: a value that would change one we hold is
listed, not loaded. A source's name and coordinates only check the match; they never replace ours.

found_values.csv has one row per site, year, source and attribute. It replaces evidence.csv,
resolved.csv, review_queue.csv and supplementary_leads.csv. sme_decision and loaded are left blank
for accept-reviews to fill, with value_decision and corrected_value.
"""
from __future__ import annotations

import hashlib
from collections import Counter
from dataclasses import dataclass, field
from typing import Any

from .arbitration import baseline_for, comparable, date_key, decision_key, same_value, site_for_year, source_key, value_year
from .credibility import TIER_2, TIER_5, TIER_BY_LABEL, tier_label
from .facility_names import distinctive_words
from .lists import citation_point, id_order, plain
from .locate import coordinates_from, format_point, haversine_km, same_state, seed_point
from .payload import normalize_url
from .prompt_builder import needs_contradiction_check
from .schema import NOT_A_WASTE_FACILITY, VALUE_CHECKED_ATTRIBUTES, is_blank, normalize_scalar


FOUND_VALUES_FILE = "found_values.csv"

SME_REVIEW = "SME review"
NOT_USED = "not used"
# Written by runs arbitrated before the key-values plan, which approved Tier 1-2 fills automatically.
# Nothing writes it now; reading an older found_values.csv still knows it.
AUTO_APPROVED = "auto-approved"
# The statuses whose values load once approved.
LOADABLE = (AUTO_APPROVED, SME_REVIEW)

# Tier 1 is a regulator, permit or operator; Tier 2 government statistics, peer-reviewed or
# intergovernmental data. Their values are trusted as read: they load with their pair's match, with
# no value check (key-values plan, rule 3).
MAX_TRUSTED_TIER = TIER_2
# value_check for a key value from a Tier 3-5 pair: an SME checks it on its own Site_Review row.
CHECK_REQUIRED = "required"
# Batch 1's location check: a source placing the facility further than this from our coordinates
# may be about another facility, so an SME looks, whatever its tier.
EXCEPTION_KM = 5.0

# The warnings a pair can carry, whatever its tier, as Run_Config counts them.
SAME_PAGE = "same page and name as another site"
FAR_AWAY = f"more than {EXCEPTION_KM:g} km away"
OTHER_STATE = "in another state"
SAYS_NOT_A_FACILITY = NOT_A_WASTE_FACILITY
EXCEPTIONS = [SAME_PAGE, FAR_AWAY, OTHER_STATE, SAYS_NOT_A_FACILITY]
# Not a column: a site's or a citation's state as the geocoder names it (locate.canonical_state),
# set by arbitration from the run's state_names.json.
STATE_NAME = "_state"

# Why a value is not used, as Run_Config counts them.
CONFIRMS = "confirms our value"
CHANGES = "would change our value"
DEAD_LINK = "dead link"
NO_LINK = "no link"
UNUSABLE = "could not be converted or mapped"
NOT_OFFERED = f"{NOT_A_WASTE_FACILITY} not offered"
EXCLUDED_BY_AGENT = "excluded by the agent"
UNVERIFIED = "the agent's unverified lead"
NOT_MUNICIPAL = "not municipal waste"
NO_LONGER_ASKED = "no longer asked"
NOT_USED_REASONS = [
    CONFIRMS, CHANGES, DEAD_LINK, NO_LINK, UNUSABLE, EXCLUDED_BY_AGENT, NOT_OFFERED, NOT_MUNICIPAL,
    NO_LONGER_ASKED, UNVERIFIED,
]
# How status_reason begins for a value not used only because of the value we hold at the site
# searched. That says nothing of another site, so an SME's Different site weighs it again there
# (accept_reviews.py).
CONFIRMS_TEXT = "Confirms our value."
CHANGES_TEXT = "Would change our value"

# What the agent writes of a value it found and then judged not to be one: a candidate it
# excluded, or an attribute it reports as not found. "The page gives disposal charges per tonne,
# not waste accepted per year" came back as 299 tonnes a year, so excluded.
AGENT_EXCLUDED_CONFIDENCE = "Excluded candidate"
AGENT_NOT_FOUND_BASIS = "Not found"


def review_id(site_id: str, page: str, name: str) -> str:
    """A site-source pair's id: the same each time the same site cites the same page under the
    same name, so a decision recorded against it can be read back after arbitrating again."""
    digest = hashlib.sha256(f"{page}|{name}".encode("utf-8")).hexdigest()[:12]
    return f"WAIREV-{site_id}-{digest}"


def _km(km: float) -> str:
    return f"{km:.1f} km" if km < 10 else f"{km:,.0f} km"


@dataclass(eq=False)
class Pair:
    """One site-source pair: a site searched, and a source its search cited, perhaps more than
    once (a follow-up pass, or two entries for one page)."""

    site_id: str
    # The exact page, or for a citation with no URL its own key, which no other site shares.
    page: str
    has_page: bool
    # The facility name, compared without case, accents or punctuation.
    name: str
    citations: list[dict[str, Any]] = field(default_factory=list)
    # Our other sites whose searches cited the same page under the same name.
    shared_with: list[str] = field(default_factory=list)
    not_a_waste_facility: bool = False
    # How far the source places the facility from our site, and whether that is further than
    # EXCEPTION_KM, allowing for how rough its point is (measure).
    distance_km: float | None = None
    far_away: bool = False
    # Whether that is measured from points too rough to show as the source's own.
    roughly: bool = False
    # The state the source puts the facility in, where that isn't our site's state (measure).
    other_state: str = ""

    @property
    def review_id(self) -> str:
        return review_id(self.site_id, self.page, self.name)

    @property
    def rated_by(self) -> dict[str, Any]:
        """The citation the source's tier comes from: the best rated. A search can describe a page
        twice - its follow-up pass gave Cowpen Bewley's register entry no publication date, so
        Tier 3 against the first pass's Tier 1 - and an attribute-first response describes it once
        for each value. The page is the same, and the date one citation found is its date."""
        return min(self.citations, key=lambda c: TIER_BY_LABEL.get(normalize_scalar(c.get("source_tier")), TIER_5))

    @property
    def tier(self) -> int:
        return TIER_BY_LABEL.get(normalize_scalar(self.rated_by.get("source_tier")), TIER_5)

    @property
    def location(self) -> dict[str, Any]:
        """The citation whose location the pair goes by: the first that places the facility, else
        the first that places it roughly, else the first."""
        return (
            next((c for c in self.citations if citation_point(c) is not None), None)
            or next((c for c in self.citations if c.get("rough_points")), self.citations[0])
        )

    def measure(self, site: dict[str, Any]) -> None:
        """How far the source places the facility from our site's coordinates, where both are known.

        A source whose point is too rough to show - coordinates with too few decimals, or coordinates
        and an address more than 2 km apart (locate.Located.rough_points) - is measured too: as far as
        the farthest of its points, and far away if any is more than EXCEPTION_KM from ours beyond
        how far off it may be. Coordinates that coarse still say the facility is 100 km away.
        """
        self.distance_km, self.far_away, self.roughly = None, False, False
        # States as the geocoder names them (arbitrate.py), so "N.L." and "Nuevo León" are one.
        ours_state = normalize_scalar(site.get(STATE_NAME))
        theirs_state = next((normalize_scalar(c.get(STATE_NAME)) for c in self.citations if normalize_scalar(c.get(STATE_NAME))), "")
        self.other_state = theirs_state if ours_state and theirs_state and not same_state(ours_state, theirs_state) else ""
        ours = seed_point(site)
        if ours is None:
            return
        location = self.location
        point = citation_point(location)
        if point is not None:
            self.distance_km = haversine_km(*ours, *point)
            self.far_away = self.distance_km > EXCEPTION_KM
            return
        rough = [(haversine_km(*ours, latitude, longitude), off) for latitude, longitude, off in location.get("rough_points") or ()]
        if rough:
            self.distance_km = max(km for km, _off in rough)
            self.far_away = any(km > EXCEPTION_KM + off for km, off in rough)
            self.roughly = True

    def exceptions(self) -> list[str]:
        """Which exceptions apply (EXCEPTIONS)."""
        found = []
        if self.shared_with:
            found.append(SAME_PAGE)
        if self.far_away:
            found.append(FAR_AWAY)
        if self.other_state:
            found.append(OTHER_STATE)
        if self.not_a_waste_facility:
            found.append(SAYS_NOT_A_FACILITY)
        return found

    @property
    def trusted(self) -> bool:
        """Whether the pair's values are trusted as read: a Tier 1-2 source."""
        return self.tier <= MAX_TRUSTED_TIER

    def value_check(self, attribute: str) -> str:
        """CHECK_REQUIRED for a key value of a Tier 3-5 pair, else blank."""
        return CHECK_REQUIRED if attribute in VALUE_CHECKED_ATTRIBUTES and not self.trusted else ""

    def reason(self) -> str:
        """Why the pair's fills go to an SME, in words: its tier, and its warnings."""
        parts = [f"{tier_label(self.tier)} source, filling an empty field"]
        for exception in self.exceptions():
            if exception == SAME_PAGE:
                others = ", ".join(self.shared_with)
                parts.append(f"the same page and name are also cited for site{'s' if len(self.shared_with) > 1 else ''} {others}")
            elif exception == FAR_AWAY:
                parts.append(f"it places the facility {'roughly ' if self.roughly else ''}{_km(self.distance_km)} from our site")
            else:
                parts.append(f"it says the site is {NOT_A_WASTE_FACILITY}")
        return "; ".join(parts) + "."


@dataclass
class Sorted:
    """What sorting found: found_values.csv's rows, every pair, and the pages and names cited for
    two or more of our sites, with their citations, for possible_duplicates.csv."""

    rows: list[dict[str, Any]]
    pairs: list[Pair]
    shared: dict[tuple[str, Any], list[dict[str, Any]]]


def same_name(name: Any) -> Any:
    """A facility name as the same-page check compares it: by its distinctive words, so "The
    Eastview Landfill" and "Eastview Landfill", or "Merrick Landfill" and "Merrick Landfill site", are
    one name; else, for a name of generic words only or in a script those words don't cover, as
    plain() gives it. A blank name is one name."""
    return frozenset(distinctive_words(normalize_scalar(name))) or plain(name)


def pair_up(citations: list[dict[str, Any]]) -> tuple[dict[tuple[str, Any], Pair], dict[tuple[str, Any], list[Pair]]]:
    """Every citation's pair, keyed by (site, source number), and the pages and names cited for
    two or more of our sites.

    A pair is one exact name (plain), which its review_id is built from. Whether two sites' pairs are
    the same page and name goes by same_name, which is looser: in the mini test, Brandon's search
    cited a Manitoba report as "The Eastview Landfill" and Guelph's as "Eastview Landfill"."""
    pairs: dict[tuple[str, str, str], Pair] = {}
    pair_of: dict[tuple[str, Any], Pair] = {}
    for citation in citations:
        site_id = normalize_scalar(citation.get("site_id"))
        url = normalize_url(citation.get("url"))
        page = url or f"#{citation.get('source_index')}"
        name = plain(citation.get("facility_name_in_source") or citation.get("facility_name_english"))
        pair = pairs.setdefault((site_id, page, name), Pair(site_id, page, bool(url), name))
        pair.citations.append(citation)
        pair_of[(site_id, citation.get("source_index"))] = pair

    by_source: dict[tuple[str, Any], list[Pair]] = {}
    for pair in pairs.values():
        if pair.has_page:
            cited = pair.citations[0]
            name = same_name(cited.get("facility_name_in_source") or cited.get("facility_name_english"))
            by_source.setdefault((pair.page, name), []).append(pair)
    # Two spellings cited by one search are two pairs of one site, not two sites.
    shared = {key: group for key, group in by_source.items() if len({pair.site_id for pair in group}) > 1}
    for group in shared.values():
        site_ids = sorted({pair.site_id for pair in group}, key=id_order)
        for pair in group:
            pair.shared_with = [site_id for site_id in site_ids if site_id != pair.site_id]
    return pair_of, shared


def sort_values(
    evidence_rows: list[dict[str, Any]],
    citations: list[dict[str, Any]],
    sites_by_id: dict[str, dict[str, Any]],
    run_year: int,
) -> Sorted:
    """found_values.csv's rows from a run's evidence: each value with its status.

    `evidence_rows` are every value each search's sources gave, as extract_evidence reads them, and
    `citations` every source each search cited, values or not: one per (site_id, source_index),
    with the facility it names and where it places it.
    """
    pair_of, shared = pair_up(citations)
    pairs = list(dict.fromkeys(pair_of.values()))
    citation_of = {(normalize_scalar(c.get("site_id")), c.get("source_index")): c for c in citations}
    for row in evidence_rows:
        if row["attribute_name"] == "facility_type" and normalize_scalar(row.get("mapped_value")) == NOT_A_WASTE_FACILITY:
            pair_of[(row["site_id"], row["source_index"])].not_a_waste_facility = True
    for pair in pairs:
        pair.measure(sites_by_id.get(pair.site_id, {}))

    found = []
    for order, row in enumerate(evidence_rows):
        key = (row["site_id"], row["source_index"])
        site = sites_by_id.get(row["site_id"], {})
        found.append(found_row(row, pair_of[key], citation_of[key], site, run_year, order))
    rows = collapse(found)
    flag_closures(rows)
    return Sorted(rows, pairs, {key: [c for pair in group for c in pair.citations] for key, group in shared.items()})


def found_row(
    row: dict[str, Any], pair: Pair, citation: dict[str, Any], site: dict[str, Any], run_year: int, order: int
) -> dict[str, Any]:
    """One value as found_values.csv lists it, with its status."""
    attribute = row["attribute_name"]
    value = normalize_scalar(row.get("mapped_value"))
    year = value_year(row.get("value_date"), run_year)
    site_then, note = site_for_year(site, attribute, year)
    ours = baseline_for(site_then, attribute)
    status, reason, why = status_of(row, attribute, value, ours, pair, site)
    check = pair.value_check(attribute) if status == SME_REVIEW else ""
    if note:
        reason = f"{reason} {note}"
    location = pair.location
    point = citation_point(location)
    as_written = " ".join(
        part for part in (normalize_scalar(row.get("claimed_value")), normalize_scalar(row.get("claimed_unit"))) if part
    )
    return {
        "run_id": row.get("run_id", ""),
        "site_id": row["site_id"],
        "site_name": normalize_scalar(site.get("site_name")),
        "country_iso3": normalize_scalar(site.get("country_iso3")),
        "year": year,
        "source_id": row.get("source_id", ""),
        "source_url": row.get("url", ""),
        "source_title": normalize_scalar(citation.get("source_title")),
        "publisher": normalize_scalar(citation.get("publisher")),
        # The pair's, which its status goes by.
        "source_tier": tier_label(pair.tier),
        "tier_rule_applied": normalize_scalar(pair.rated_by.get("tier_rule_applied")),
        "link_status": row.get("link_status", ""),
        "facility_name_in_source": normalize_scalar(location.get("facility_name_in_source")),
        "facility_name_english": normalize_scalar(location.get("facility_name_english")),
        "source_address": normalize_scalar(location.get("stated_address")),
        "source_place": ", ".join(dict.fromkeys(
            part for part in (normalize_scalar(location.get("stated_municipality")), normalize_scalar(location.get("stated_state"))) if part
        )),
        "stated_coordinates": normalize_scalar(location.get("stated_coordinates")),
        "source_coordinates": format_point(*point) if point is not None else "",
        "coordinates_from": coordinates_from(location.get("location_basis")) if point is not None else "",
        "distance_km": f"{pair.distance_km:.2f}" if pair.distance_km is not None else "",
        "location_note": normalize_scalar(location.get("location_reason")),
        "attribute_name": attribute,
        "value": value,
        "unit": normalize_scalar(row.get("normalized_unit")) if value else "",
        "value_as_written": as_written,
        "value_date": normalize_scalar(row.get("value_date")),
        "our_value": ours,
        "quote": normalize_scalar(row.get("quoted_evidence_short")),
        "quote_on_page": row.get("quote_on_page", ""),
        "location_on_page": row.get("location_on_page", ""),
        "agent_note": normalize_scalar(row.get("evidence_summary")),
        "value_basis": normalize_scalar(row.get("value_basis")),
        "agent_confidence": normalize_scalar(row.get("agent_confidence")),
        "planned": normalize_scalar(row.get("planned")),
        "not_municipal": normalize_scalar(row.get("not_municipal")),
        "review_id": pair.review_id,
        "status": status,
        "status_reason": reason,
        "value_check": check,
        "sme_decision": "",
        "value_decision": "",
        "corrected_value": "",
        "loaded": "",
        "_why": why,
        "_order": order,
    }


def status_of(
    row: dict[str, Any], attribute: str, value: str, ours: str, pair: Pair, site: dict[str, Any]
) -> tuple[str, str, str]:
    """(status, why in words, why as Run_Config counts it) for one value."""
    excluded = row.get("_excluded", "")
    if excluded:
        reason = normalize_scalar(row.get("exclusion_reason"))
        if not value and row.get("mapping_note"):
            reason = f"{reason.rstrip('.')}: {row['mapping_note']}"
        return NOT_USED, reason, excluded
    if (
        normalize_scalar(row.get("agent_confidence")) == AGENT_EXCLUDED_CONFIDENCE
        or normalize_scalar(row.get("value_basis")) == AGENT_NOT_FOUND_BASIS
    ):
        # Under review an SME would reject it; approved automatically, it would load unseen.
        return NOT_USED, "The agent marked it an excluded candidate, not a finding.", EXCLUDED_BY_AGENT
    if attribute == "facility_type" and value == NOT_A_WASTE_FACILITY and not needs_contradiction_check(site):
        # The verdict is offered only to sites nothing independently confirms (WP-525).
        return NOT_USED, f"{NOT_A_WASTE_FACILITY} is used only where the search offered it, and this site was not offered it.", NOT_OFFERED
    if attribute == "annual_incoming_waste_metric_tonnes" and row.get("not_municipal"):
        # Incoming waste counts municipal waste only, as the standardized spec asks of every
        # source (Hugh, 30 October): an amount of rubble, soil or tailings stays out of the table.
        return NOT_USED, (
            "Not municipal waste: the source says this amount is mainly other waste, such as rubble, "
            "soil or tailings, and incoming waste counts municipal waste only."
        ), NOT_MUNICIPAL
    if attribute == "bulk_waste_type":
        # Retired 30 October 2026 (schema.UNSEARCHED_ATTRIBUTES); an older response still gives it.
        return NOT_USED, (
            "No longer asked: whether incoming waste is municipal is marked on the amount itself, "
            "and a site taking mainly other waste is a Non-Municipal Waste Landfill."
        ), NO_LONGER_ASKED
    if not is_blank(ours):
        if comparable(ours, attribute) == comparable(value, attribute):
            return NOT_USED, CONFIRMS_TEXT, CONFIRMS
        return NOT_USED, f"{CHANGES_TEXT}, {ours}: only fills of empty fields load.", CHANGES
    return SME_REVIEW, pair.reason(), ""


def collapse(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """One row per site, year, source and attribute.

    A source can give one attribute twice for one year, or be cited again by a follow-up pass. The
    row kept is a value that could load if there is one, then the newest, then the first the agent
    gave; a different value it set aside is named in the kept row's status_reason.
    """
    groups: dict[tuple[str, int, str, str], list[dict[str, Any]]] = {}
    for row in rows:
        groups.setdefault((row["site_id"], row["year"], row["review_id"], row["attribute_name"]), []).append(row)
    out = []
    for group in groups.values():
        group.sort(key=lambda row: (
            row["status"] == NOT_USED, tuple(-part for part in date_key(row["value_date"])), row["_order"],
        ))
        kept = group[0]
        others = list(dict.fromkeys(
            f"{row['value']} ({row['value_date'] or 'undated'})" for row in group[1:]
            if row["status"] != NOT_USED and comparable(row["value"], row["attribute_name"]) != comparable(kept["value"], kept["attribute_name"])
        ))
        if others:
            kept["status_reason"] = f"{kept['status_reason']} The source also gives {'; '.join(others)}."
        out.append(kept)
    return out


def closures(rows: list[dict[str, Any]]) -> dict[str, list[str]]:
    """What the run found of each site having closed: "facility_status = Inactive", or its
    closing_year, from any source. A planned closure says nothing of the site having closed."""
    closure: dict[str, list[str]] = {}
    for row in rows:
        if row["attribute_name"] == "facility_status" and row["value"] == "Inactive":
            closure.setdefault(row["site_id"], []).append("facility_status = Inactive")
        elif row["attribute_name"] == "closing_year" and row["value"] and not row.get("planned"):
            closure.setdefault(row["site_id"], []).append(f"closing_year = {row['value']}")
    return {site_id: list(dict.fromkeys(reported)) for site_id, reported in closure.items()}


def flag_closures(rows: list[dict[str, Any]]) -> None:
    """Say on a "Not a Waste Facility" verdict for review when the run also found the site closed.

    The likeliest false verdict is a closed landfill misread as "not a landfill" - "it's a park
    now" - and a site given the verdict AND reported closed is exactly that shape.
    """
    closure = closures(rows)
    for row in rows:
        reported = closure.get(row["site_id"], [])
        if row["attribute_name"] == "facility_type" and row["value"] == NOT_A_WASTE_FACILITY and row["status"] == SME_REVIEW and reported:
            row["status_reason"] += (
                f" CHECK: this run also found {'; '.join(reported)} - is this a closed landfill misread as "
                "never a waste site?"
            )


def unverified_rows(leads: list[dict[str, Any]], sites_by_id: dict[str, dict[str, Any]], run_year: int) -> list[dict[str, Any]]:
    """The agent's own unverified_leads as found_values.csv rows, not used: it gave no source it
    would stand behind. `leads` are payload_leads'."""
    rows = []
    for lead in leads:
        site = sites_by_id.get(lead["site_id"], {})
        rows.append({
            "run_id": lead.get("run_id", ""),
            "site_id": lead["site_id"],
            "site_name": normalize_scalar(site.get("site_name")),
            "country_iso3": normalize_scalar(site.get("country_iso3")),
            "year": run_year,
            "source_url": lead.get("url", ""),
            "attribute_name": lead.get("attribute_name", ""),
            "value": lead.get("value", ""),
            "value_as_written": lead.get("value", ""),
            "agent_note": lead.get("lead_summary", ""),
            "status": NOT_USED,
            "status_reason": "The agent reported it as unverified.",
            "_why": UNVERIFIED,
        })
    return rows


def judged_on_our_value(row: dict[str, Any]) -> bool:
    """Whether a found_values.csv row is not used only because it confirms or would change the value
    we hold at the site searched."""
    reason = normalize_scalar(row.get("status_reason"))
    return row.get("status") == NOT_USED and (reason.startswith(CONFIRMS_TEXT) or reason.startswith(CHANGES_TEXT))


# --- one value per site, attribute and year ------------------------------------------------------
def pick_values(rows: list[dict[str, Any]], statuses: tuple[str, ...] = LOADABLE) -> dict[tuple[str, str, Any], dict[str, Any]]:
    """The one value to load for each site, attribute and year (for a yearly attribute; otherwise
    each site and attribute), among the rows with one of `statuses`, where they disagree: the best
    tier, then the newest value_date, then the value the most sources give (a page giving it twice
    counts once, and an operator's name counts every spelling of it, arbitration.same_value), then
    the agent's order (decision 6).

    Tier always comes first. arbitration.resolve, which this once went through, let a status from a
    lower tier win when it was dated years later, which within one year's key only happens where a
    date was misread: one after the run's year, which value_year folds into the run's, or one before
    1950.

    Returns the chosen found_values.csv row by key (arbitration.decision_key).
    """
    by_key: dict[tuple[str, str, Any], list[tuple[int, dict[str, Any]]]] = {}
    for order, row in enumerate(rows):
        if row.get("status") not in statuses or is_blank(row.get("value")):
            continue
        key = decision_key(normalize_scalar(row["site_id"]), row["attribute_name"], int(row["year"]))
        by_key.setdefault(key, []).append((order, row))
    picked = {}
    for key, items in by_key.items():
        attribute = key[1]
        rows_here = [row for _order, row in items]
        # How many sources give each row's value, counted as arbitration.source_counts counts them.
        agreeing = {
            id(row): len({
                source_key(other, index) for index, other in enumerate(rows_here)
                if same_value(other.get("value"), row.get("value"), attribute)
            })
            for row in rows_here
        }
        _order, best = min(items, key=lambda item: (
            TIER_BY_LABEL.get(normalize_scalar(item[1].get("source_tier")), TIER_5),
            tuple(-part for part in date_key(item[1].get("value_date"))),
            -agreeing[id(item[1])],
            item[0],
        ))
        picked[key] = best
    return picked


# --- what Run_Config says ------------------------------------------------------------------------
def describe_sorting(rows: list[dict[str, Any]], pairs: list[Pair]) -> list[dict[str, Any]]:
    """How the run's values and pairs were sorted, for Run_Config. A pair is counted where any of its
    values would load; a pair none of whose values would load is not counted, nor listed."""
    review_ids = {row["review_id"] for row in rows if row["status"] == SME_REVIEW}
    review = [pair for pair in pairs if pair.review_id in review_ids]
    tiers = Counter("Tier 1-2" if pair.trusted else "Tier 3-5" for pair in review)
    warned = Counter(exception for pair in review for exception in pair.exceptions())
    statuses = Counter(row["status"] for row in rows)
    checks = sum(1 for row in rows if row.get("value_check") == CHECK_REQUIRED)
    not_used = Counter(row["_why"] for row in rows if row["status"] == NOT_USED)
    return [
        {
            "setting": "site_source_pairs",
            "value": f"{len(review)}{_breakdown(tiers, ['Tier 1-2', 'Tier 3-5'])} ({statuses[SME_REVIEW]} values)",
        },
        {"setting": "pairs_with_warnings", "value": f"{sum(1 for pair in review if pair.exceptions())}{_breakdown(warned, EXCEPTIONS)}"},
        {"setting": "values_to_check", "value": checks},
        {"setting": "values_not_used", "value": f"{statuses[NOT_USED]}{_breakdown(not_used, NOT_USED_REASONS)}"},
    ]


def _breakdown(counts: Counter, order: list[str]) -> str:
    parts = [f"{name} {counts[name]}" for name in order if counts[name]]
    return f": {', '.join(parts)}" if parts else ""
