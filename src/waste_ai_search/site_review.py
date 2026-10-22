"""The Site_Review tab: the site-source pairs an SME decides, under each of our sites.

Step 3 of the approve-sources plan (docs/plans/2026-10-21-approve-sources-site-by-site.md). We trust
the agent to read values off a page; what a person checks is whether the page is about our site. So
the tab has one row per site-source pair (found_values.py) whose values wait on an SME, grouped by
our site, with the pairs that carry a flag first. An auto-approved pair is not listed, and nor is one
none of whose values would load.

Each row shows our site, the source, what the source says of the facility it describes and where it
places it, the flags that sent the pair here or that the source's own page raises, and what
approving it would load. The SME picks a decision:

| Decision | What happens |
|---|---|
| Correct site | Its values load at this site. |
| Different site, with other_site_id | Its values load at that site, where its fields are empty. |
| New site | Nothing loads. The source is listed in new_facility_candidates.csv. |
| Junk | Nothing loads. |
| Blank | Nothing yet. |

review_id names the pair, and is the same each time the run is arbitrated, so a decision is read
back by it (accept_reviews.py).
"""
from __future__ import annotations

from typing import Any

from .found_values import EXCEPTION_KM, SME_REVIEW, Pair, closures
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
NOT_A_FACILITY_FLAG = f"says {NOT_A_WASTE_FACILITY}"


def our_coordinates(site: dict[str, Any]) -> str:
    """Where our database puts the site, as "lat, lon", which Google Maps finds when pasted into its
    search box."""
    point = seed_point(site)
    return format_point(*point) if point is not None else ""


def site_review_rows(
    found: list[dict[str, Any]], pairs: list[Pair], sites_by_id: dict[str, dict[str, Any]]
) -> list[dict[str, Any]]:
    """Site_Review's rows: one per pair with values for SME review. Our sites come in id order, and
    under each, the pairs with flags first, then the best tier, then in the order the search cited
    them.

    `found` is found_values.csv's rows and `pairs` the run's site-source pairs, as sort_values gives
    them.
    """
    of_pair: dict[str, list[dict[str, Any]]] = {}
    for row in found:
        if row.get("review_id"):
            of_pair.setdefault(row["review_id"], []).append(row)
    # As flag_closures reads them: from the sources cited, not the agent's unverified leads.
    closed = closures([row for rows in of_pair.values() for row in rows])
    listed = []
    for order, pair in enumerate(pairs):
        rows = of_pair.get(pair.review_id, [])
        waiting = [row for row in rows if row["status"] == SME_REVIEW]
        if not waiting:
            continue
        flags = pair_flags(pair, rows, waiting, closed.get(pair.site_id, []))
        # Our site and the source as found_values.csv gives them, from the pair's first value.
        record = {column: waiting[0].get(column, "") for column in SITE_REVIEW_HEADERS}
        record.update({
            "review_id": pair.review_id,
            "site_id": pair.site_id,
            "our_coordinates": our_coordinates(sites_by_id.get(pair.site_id, {})),
            "flags": "\n".join(flags),
            "would_load": would_load(waiting),
            **{column: "" for column in SITE_REVIEW_EDITABLE},
        })
        listed.append(((id_order(pair.site_id), not flags, pair.tier, order), record))
    listed.sort(key=lambda item: item[0])
    return [record for _key, record in listed]


def pair_flags(
    pair: Pair, rows: list[dict[str, Any]], waiting: list[dict[str, Any]], closed: list[str]
) -> list[str]:
    """What an SME should look at twice in a pair: the exceptions that sent a Tier 1-2 source here,
    whatever its tier, and what the source's own page doesn't bear out.

    A quote is checked for each value approving would load (`waiting`), and named by its attribute;
    the location the source states is checked on every value it gives (`rows`), since it is the
    pair's. `closed` is what the run found of the site having closed.
    """
    flags = []
    for result, flag in QUOTE_FLAGS.items():
        attributes = list(dict.fromkeys(row["attribute_name"] for row in waiting if row.get("quote_on_page") == result))
        if attributes:
            flags.append(f"{flag} ({', '.join(attributes)})")
    for result, flag in LOCATION_FLAGS.items():
        if any(row.get("location_on_page") == result for row in rows):
            flags.append(flag)
            break
    if pair.shared_with:
        sites = "s" if len(pair.shared_with) > 1 else ""
        flags.append(f"same page and name also cited for site{sites} {', '.join(pair.shared_with)}")
    if pair.distance_km is not None and pair.distance_km > EXCEPTION_KM:
        flags.append(FAR_AWAY_FLAG)
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


def would_load(rows: list[dict[str, Any]]) -> str:
    """What approving a pair would load, one line per value: "attribute = value (year)", the year
    only for a yearly attribute, as one value is loaded per site and attribute otherwise."""
    order = {attribute: index for index, attribute in enumerate(GAP_FILL_ATTRIBUTES)}
    lines = []
    for row in sorted(rows, key=lambda row: (order.get(row["attribute_name"], len(order)), -int(row["year"]))):
        line = f"{row['attribute_name']} = {normalize_scalar(row['value'])}"
        if row["attribute_name"] in TIME_VARYING_ATTRIBUTES:
            line += f" ({row['year']})"
        lines.append(line)
    return "\n".join(dict.fromkeys(lines))
