"""The Site_Review tab: every site-source pair an SME decides, under each of our sites, and under a
pair the values an SME checks against the page.

Step 3 of the approve-sources plan (docs/plans/2026-10-21-approve-sources-site-by-site.md), as the
key-values plan (docs/plans/2026-10-26-smes-check-key-values.md) changed it. A person checks whether
each source is about our site, whatever its tier: the tab has one row per site-source pair
(found_values.py) with values that would load, grouped by our site, with the pairs that carry a flag
first. A Tier 1-2 source's values are trusted as read. Under a Tier 3-5 pair comes one row per key
value (schema.VALUE_CHECKED_ATTRIBUTES) it would load, for the SME to check against the page, and
under a pair of any tier one for an operator the agent marked Inferred or Conflicting
(found_values.needs_value_check).

A source row shows our site, the source, what the source says of the facility it describes and
where it places it, the flags, and what approving it would load. Its decision:

| Decision | What happens |
|---|---|
| Correct site | Its values load at this site; a key value only once marked Correct. |
| Different site, with other_site_id | Its values load at that site, where its fields are empty; a key value only once marked Correct. |
| New site | Nothing loads. The source is listed in new_facility_candidates.csv. |
| Junk | Nothing loads. |
| Blank | Nothing yet. |

A value row shows which value (check), the value as it would load, the evidence (the quote, the value
as the page writes it, the agent's note) and its flags. Its decision is Correct or Wrong; with Wrong,
an SME who knows the right value types it in corrected_value, and that loads instead.

review_id names the pair, and is the same each time the run is arbitrated, so a decision is read
back by it (accept_reviews.py); a value row by its review_id and check.
"""
from __future__ import annotations

import re
from typing import Any

from .arbitration import decision_key, same_value
from .found_values import CHECK_REQUIRED, EXCEPTION_KM, SME_REVIEW, Pair, closures, pick_values
from .geocoder import place_text
from .lists import id_order
from .locate import format_point, seed_point
from .on_page import NOT_FOUND, PARTLY
from .schema import (
    GAP_FILL_ATTRIBUTES,
    NOT_A_WASTE_FACILITY,
    SITE_REVIEW_EDITABLE,
    SITE_REVIEW_HEADERS,
    TIME_VARYING_ATTRIBUTES,
    normalize_scalar,
)


SITE_REVIEW_SHEET = "Site_Review"

# What a row is, for the workbook's layout (workbook_io.py): never written as a column.
KIND = "_kind"
SOURCE_ROW = "source"
VALUE_ROW = "value"

# Begins a value row's source_title, so it reads as under the source above it.
VALUE_ROW_MARK = "↳"

# The flags, one line each in the flags cell. A page check is flagged where our check couldn't find
# the quote, or the location the source states, on the page as the link check kept it (on_page.py).
QUOTE_FLAGS = {
    NOT_FOUND: "quote not found on the page",
    PARTLY: "quote only partly found on the page",
}
LOCATION_FLAGS = {
    NOT_FOUND: "address or coordinates not found on the page",
    PARTLY: "address or coordinates only partly found on the page",
}
FAR_AWAY_FLAG = f"more than {EXCEPTION_KM:g} km from our site"
OTHER_STATE_FLAG = "in a different state from our site"
NO_NAME_FLAG = "the source names no facility"
NOT_A_FACILITY_FLAG = f"says {NOT_A_WASTE_FACILITY}"
PLANNED_FLAG = "planned: a closure the page expects, not one that has happened"
# Said on a source row, which lists every value the page gives, of a key value an SME checks on
# its own ↳ row below (Hugh, 30 October: "(check below)" said too little).
CHECK_INDEPENDENTLY = "(check independently)"


def our_coordinates(site: dict[str, Any]) -> str:
    """Where our database puts the site, as "lat, lon", which Google Maps finds when pasted into its
    search box."""
    point = seed_point(site)
    return format_point(*point) if point is not None else ""


def say_link_status(records: list[dict[str, Any]], words: dict[str, str]) -> list[dict[str, Any]]:
    """Each source row's link_status in words, by its URL (on_page.PageChecker.link_words): where
    our check couldn't read the page, why, so the SME knows its quotes weren't checked. A value row
    has none."""
    for record in records:
        if record.get(KIND) == SOURCE_ROW and words.get(record.get("source_url", "")):
            record["link_status"] = words[record["source_url"]]
    return records


def check_label(row: dict[str, Any]) -> str:
    """Which value a value row checks: the attribute, with the year for a yearly attribute, since
    one value is loaded per site and attribute otherwise. With review_id, it names the row."""
    attribute = row["attribute_name"]
    return f"{attribute} ({row['year']})" if attribute in TIME_VARYING_ATTRIBUTES else attribute


def needs_check(row: dict[str, Any]) -> bool:
    return row.get("value_check") == CHECK_REQUIRED


def site_review_rows(
    found: list[dict[str, Any]], pairs: list[Pair], sites_by_id: dict[str, dict[str, Any]]
) -> list[dict[str, Any]]:
    """Site_Review's rows: one per pair with values that would load, and under it one per key value
    an SME checks. Our sites come in id order, and under each, the pairs with flags first, then the
    best tier, then in the order the search cited them.

    `found` is found_values.csv's rows and `pairs` the run's site-source pairs, as sort_values gives
    them.
    """
    of_pair: dict[str, list[dict[str, Any]]] = {}
    for row in found:
        if row.get("review_id"):
            of_pair.setdefault(row["review_id"], []).append(row)
    # As flag_closures reads them: from the sources cited, not the agent's unverified leads.
    closed = closures([row for rows in of_pair.values() for row in rows])
    # Every value that would load, by what it loads for, to say where two sources disagree.
    loading: dict[tuple[str, str, Any], list[dict[str, Any]]] = {}
    for rows in of_pair.values():
        for row in rows:
            if row["status"] == SME_REVIEW and normalize_scalar(row.get("value")):
                loading.setdefault(decision_key(row["site_id"], row["attribute_name"], int(row["year"])), []).append(row)
    listed = []
    for order, pair in enumerate(pairs):
        rows = of_pair.get(pair.review_id, [])
        waiting = [row for row in rows if row["status"] == SME_REVIEW]
        if not waiting:
            continue
        checked = sorted((row for row in waiting if needs_check(row)), key=value_order)
        unchecked = [row for row in waiting if not needs_check(row)]
        flags = pair_flags(pair, rows, unchecked, closed.get(pair.site_id, []))
        flags += [flag for row in unchecked if (flag := rival_flag(row, loading))]
        # Our site and the source as found_values.csv gives them, from the pair's first value.
        record = {column: waiting[0].get(column, "") for column in SITE_REVIEW_HEADERS}
        record.update({
            KIND: SOURCE_ROW,
            "review_id": pair.review_id,
            "check": "",
            "site_id": pair.site_id,
            "our_coordinates": our_coordinates(sites_by_id.get(pair.site_id, {})),
            "our_place": place_text(sites_by_id.get(pair.site_id, {})),
            "flags": "\n".join(dict.fromkeys(flags)),
            "would_load": would_load(waiting),
            "evidence": "",
            "quote": "",
            **{column: "" for column in SITE_REVIEW_EDITABLE},
        })
        group = [record, *(value_record(row, loading) for row in checked)]
        listed.append(((id_order(pair.site_id), not flags, pair.tier, order), group))
    listed.sort(key=lambda item: item[0])
    return [record for _key, group in listed for record in group]


def value_order(row: dict[str, Any]) -> tuple[int, int]:
    """Values in the order the database lists its attributes, the newest year first."""
    order = {attribute: index for index, attribute in enumerate(GAP_FILL_ATTRIBUTES)}
    return order.get(row["attribute_name"], len(order)), -int(row["year"])


def value_record(row: dict[str, Any], loading: dict[tuple[str, str, Any], list[dict[str, Any]]]) -> dict[str, Any]:
    """A key value's own row under its pair: what it is, the evidence, and its flags."""
    flags = []
    if row.get("quote_on_page") in QUOTE_FLAGS:
        flags.append(QUOTE_FLAGS[row["quote_on_page"]])
    if row.get("planned"):
        flags.append(PLANNED_FLAG)
    rival = rival_flag(row, loading)
    if rival:
        flags.append(rival)
    record = {column: "" for column in SITE_REVIEW_HEADERS}
    record.update({
        KIND: VALUE_ROW,
        "review_id": row["review_id"],
        "check": check_label(row),
        "site_id": row["site_id"],
        "source_url": row.get("source_url", ""),
        "source_title": f"{VALUE_ROW_MARK} check {check_label(row)}",
        "flags": "\n".join(flags),
        "would_load": value_line(row),
        "evidence": evidence(row),
        # Not columns: the workbook links the source to the quote (a text fragment), and offers
        # corrected_value the attribute's own allowed values.
        "quote": normalize_scalar(row.get("quote")),
        "_attribute": row["attribute_name"],
    })
    return record


def evidence(row: dict[str, Any]) -> str:
    """What the SME checks a value against: the quote, the value as the page writes it where that
    differs from what loads, and the agent's note."""
    lines = []
    quote = normalize_scalar(row.get("quote"))
    if quote:
        lines.append(f"“{quote}”")
    written = normalize_scalar(row.get("value_as_written"))
    # "2019 year" says no more than 2019.
    if written and re.sub(r"\s+years?$", "", written) != normalize_scalar(row.get("value")):
        lines.append(f"As written: {written}")
    note = normalize_scalar(row.get("agent_note"))
    if note:
        lines.append(f"Agent: {note}")
    return "\n".join(lines)


def pair_flags(
    pair: Pair, rows: list[dict[str, Any]], unchecked: list[dict[str, Any]], closed: list[str]
) -> list[str]:
    """What an SME should look at twice in a pair: what once sent a Tier 1-2 source to an SME, where
    the source places the facility apart from our site, whether it names one, and what the source's
    own page doesn't bear out.

    A quote is checked for each value approving would load with no row of its own (`unchecked`), and
    named by its attribute; a key value's is flagged on its own row. The location the source states
    is checked on every value it gives (`rows`), since it is the pair's. `closed` is what the run found
    of the site having closed.
    """
    flags = []
    for result, flag in QUOTE_FLAGS.items():
        attributes = list(dict.fromkeys(row["attribute_name"] for row in unchecked if row.get("quote_on_page") == result))
        if attributes:
            flags.append(f"{flag} ({', '.join(attributes)})")
    for result, flag in LOCATION_FLAGS.items():
        if any(row.get("location_on_page") == result for row in rows):
            flags.append(flag)
            break
    if pair.shared_with:
        sites = "s" if len(pair.shared_with) > 1 else ""
        flags.append(f"same page and name also cited for site{sites} {', '.join(pair.shared_with)}")
    if pair.far_away:
        flags.append(FAR_AWAY_FLAG)
    if pair.other_state:
        flags.append(f"{OTHER_STATE_FLAG}: the source says {pair.other_state}")
    if pair.other_municipality:
        # A prompt to look, not a reason to reject: a landfill can serve one town from the next.
        flags.append(
            f"the source puts the site in {pair.other_municipality}; ours is in {pair.our_municipality} - "
            "check it is our site"
        )
    if not pair.name:
        flags.append(NO_NAME_FLAG)
    if pair.not_a_waste_facility:
        # The likeliest false verdict is a closed landfill misread as never a waste site.
        if closed:
            flags.append(
                f"{NOT_A_FACILITY_FLAG}, but this run also found {'; '.join(closed)}: is it a closed "
                "landfill misread as never a waste site?"
            )
        else:
            flags.append(NOT_A_FACILITY_FLAG)
    return flags


def rival_flag(row: dict[str, Any], loading: dict[tuple[str, str, Any], list[dict[str, Any]]]) -> str:
    """Where another source on the tab gives a different value for what this one would fill. One
    value loads per site, attribute and year, picked by tier first, so the SME is told which loads if
    both sources are approved. Blank where no other source disagrees; two spellings of one operator's
    name don't (arbitration.same_value).

    `loading` is every value that would load, by arbitration.decision_key.
    """
    attribute, value = row["attribute_name"], normalize_scalar(row.get("value"))
    key = decision_key(row["site_id"], attribute, int(row["year"]))
    others = [
        other for other in loading.get(key, [])
        if other.get("review_id") != row.get("review_id") and not same_value(other["value"], value, attribute)
    ]
    if not value or not others:
        return ""
    winner = pick_values([*others, row], statuses=(SME_REVIEW,))[key]
    if winner is row:
        best = pick_values(others, statuses=(SME_REVIEW,))[key]
        return f"a {best['source_tier']} source here gives {value_line(best)}; if both are approved, this one loads"
    return f"a {winner['source_tier']} source here gives {value_line(winner)}; if both are approved, that one loads"


def value_line(row: dict[str, Any]) -> str:
    """One value as it loads: "attribute = value (year)", the year only for a yearly attribute, as one
    value is loaded per site and attribute otherwise."""
    line = f"{row['attribute_name']} = {normalize_scalar(row['value'])}"
    if row["attribute_name"] in TIME_VARYING_ATTRIBUTES:
        line += f" ({row['year']})"
    return line


def would_load(rows: list[dict[str, Any]]) -> str:
    """What approving a pair would load, one line per value (value_line), a key value an SME checks
    on its own row marked so."""
    ordered = sorted(rows, key=value_order)
    return "\n".join(dict.fromkeys(
        f"{value_line(row)} {CHECK_INDEPENDENTLY}" if needs_check(row) else value_line(row) for row in ordered
    ))
