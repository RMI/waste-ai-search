"""The Site_Review tab lists the site-source pairs an SME decides, and under a Tier 3-5 pair the key
values an SME checks (site_review.py).

Step 3 of the approve-sources plan, as the key-values plan changed it. A person checks whether every
source is about our site, so the tab has one source row per site-source pair with values that would
load, grouped by our site with the flagged pairs first. Each shows our site, the source, what it says
of the facility it describes, the flags, and what approving it would load. Under a Tier 3-5 pair, a
value row per key value shows the value, the evidence and its flags. The workbook's own affordances (columns,
dropdown, links, shading) are in test_workbook.py. These run end to end through run_arbitration
where they can, on hand-built source-first responses.
"""
from __future__ import annotations

import csv
import json

import pytest
from openpyxl import load_workbook

from waste_ai_search.arbitrate import run_arbitration
from waste_ai_search.found_values import SME_REVIEW, Pair
from waste_ai_search.input_loader import write_csv_records
from waste_ai_search.on_page import FOUND, NO_PAGE_TEXT, NOT_FOUND, PARTLY
from waste_ai_search.run_context import PipelineConfig
from waste_ai_search.schema import NOT_A_WASTE_FACILITY, SITE_REVIEW_HEADERS
from waste_ai_search.seed_source import seed_headers
from waste_ai_search.site_review import our_coordinates, pair_flags, would_load


def seed(site_id, name, lat, lon, iso3="CAN", **extra):
    return {
        "facility_id": site_id, "site_id": site_id, "internal_facility_id": site_id, "year": "2022",
        "site_name": name, "facility_name": name, "country_iso3": iso3, "iso3c_plus": iso3,
        "latitude": str(lat), "longitude": str(lon), "contributing_data_sources": "canada_ghgrp_2021",
        "reference_year": "2022", **extra,
    }


def source(url, name="", location="", values=(), source_type="Regulator", address=""):
    """A source as the agent reports one; `location` is the coordinates it states."""
    return {
        "url": url, "source_title": "Page", "publisher": "Publisher", "source_type": source_type,
        "publication_date": "2024-03-01", "facility_name_in_source": name, "facility_name_english": "",
        "stated_address": address, "stated_coordinates": location, "values": list(values),
    }


def value(attribute, found, value_date="2024", unit=""):
    return {"attribute_name": attribute, "value": found, "unit": unit, "value_basis": "Direct",
            "confidence_score": "High", "value_date": value_date, "evidence_summary": "Says so.",
            "quoted_evidence_short": "It says so."}


def arbitrate(tmp_path, seeds, responses):
    """Arbitrate one run, `responses` mapping each site searched to the sources its search cited, and
    return the run folder."""
    run_dir = tmp_path / "run"
    write_csv_records(run_dir / "seed.csv", seeds, seed_headers())
    raw = run_dir / "raw_foundry_responses"
    raw.mkdir(parents=True, exist_ok=True)
    for site_id, sources in responses.items():
        (raw / f"site_{site_id}.json").write_text(json.dumps({"site_id": site_id, "sources": sources}))
    run_arbitration(PipelineConfig(input_csv=None, run_dir=run_dir, run_id="run", dataset_version="v"))
    return run_dir


def all_rows(run_dir):
    ws = load_workbook(run_dir / "run_review.xlsx")["Site_Review"]
    header = [cell.value for cell in ws[1]]
    assert header == SITE_REVIEW_HEADERS
    return [{h: ("" if v is None else v) for h, v in zip(header, row)} for row in ws.iter_rows(min_row=2, values_only=True)]


def site_review(run_dir):
    """The source rows."""
    return [row for row in all_rows(run_dir) if not row["check"]]


def found_values(run_dir):
    return list(csv.DictReader((run_dir / "found_values.csv").open(encoding="utf-8-sig")))


# Brady Road, in Winnipeg, and a site of ours 38 km north of it.
BRADY = seed("13413", "Brady Road Resource Management Facility", 49.7583, -97.2004)
NEARBY = seed("900", "domestic landfill", 50.10, -97.20)


# --- which pairs are listed ------------------------------------------------------------------------
def test_every_pair_with_values_that_would_load_is_listed_one_source_row_each(tmp_path):
    """A Tier 1 pair is listed too (key-values plan); a pair none of whose values would load isn't; a
    pair giving several values is one source row, under the review_id its values have in
    found_values.csv."""
    site = {**BRADY, "facility_type": "Sanitary Landfill"}
    run_dir = arbitrate(tmp_path, [site], {"13413": [
        source("https://manitoba.example/permit", "Brady Road Landfill", values=[value("operator", "City of Winnipeg")]),
        source("https://news.example/brady", "Brady Road", values=[value("closing_year", "2045"), value("facility_status", "Active")],
               source_type="News report"),
        # Tier 3, but it only confirms our facility type.
        source("https://blog.example/brady", "Brady Road", values=[value("facility_type", "Sanitary Landfill")], source_type="News report"),
    ]})
    listed = site_review(run_dir)
    assert [(row["source_url"], row["source_tier"]) for row in listed] == [
        ("https://manitoba.example/permit", "Tier 1"), ("https://news.example/brady", "Tier 3"),
    ]
    waiting = {r["review_id"] for r in found_values(run_dir) if r["status"] == SME_REVIEW}
    assert waiting == {row["review_id"] for row in listed}
    assert {(row["decision"], row["other_site_id"], row["corrected_value"], row["reviewer"], row["reviewed_date"], row["notes"]) for row in listed} == {("", "", "", "", "", "")}


def test_a_tier_3_to_5_pair_has_a_row_for_each_key_value_and_a_tier_1_2_pair_none(tmp_path):
    """The key values of a Tier 3-5 source are checked against the page on rows of their own, under
    it; a Tier 1-2 source's are trusted as read."""
    run_dir = arbitrate(tmp_path, [BRADY], {"13413": [
        source("https://manitoba.example/permit", "Brady Road Landfill", values=[value("closing_year", "2019"), value("operator", "City")]),
        source("https://news.example/brady", "Brady Road", source_type="News report", values=[
            value("operator", "City of Winnipeg"),
            {**value("area_square_meters", "12", unit="ha"), "quoted_evidence_short": "The 12 ha site", "evidence_summary": "Footprint."},
            {**value("closing_year", "2031"), "planned": True},
            value("annual_incoming_waste_metric_tonnes", "400000", "2023", unit="t"),
        ]),
    ]})
    rows = all_rows(run_dir)
    assert [(row["source_url"], row["check"]) for row in rows] == [
        ("https://manitoba.example/permit", ""),
        ("https://news.example/brady", ""),
        ("open the page at the quote", "closing_year"),
        ("open the page at the quote", "annual_incoming_waste_metric_tonnes (2023)"),
        ("open the page at the quote", "area_square_meters"),
    ]
    news, closing, incoming, area = rows[1:]
    assert news["would_load"] == (
        "operator = City of Winnipeg\nclosing_year = 2031 (check below)\n"
        "annual_incoming_waste_metric_tonnes = 400000 (2023) (check below)\narea_square_meters = 120000 (check below)"
    )
    assert {row["review_id"] for row in (closing, incoming, area)} == {news["review_id"]}
    assert (area["source_title"], area["would_load"], area["site_id"], area["site_name"]) == (
        "↳ check area_square_meters", "area_square_meters = 120000", "13413", "",
    )
    assert area["evidence"] == "“The 12 ha site”\nAs written: 12 ha\nAgent: Footprint."
    # The permit gives a closing year too: the better tier loads if both are approved.
    assert closing["flags"] == (
        "a planned closure, not an actual one: the source says the site is expected to close then\n"
        "a Tier 1 source here gives closing_year = 2019; if both are approved, that one loads"
    )


def test_our_sites_come_in_id_order_with_flagged_pairs_first(tmp_path):
    """Under each of our sites, a pair with a flag comes before one without, whatever order the
    search cited them in; sites are in number order, not text order."""
    run_dir = arbitrate(tmp_path, [BRADY, NEARBY], {
        "13413": [
            source("https://news.example/brady", "Brady Road", values=[value("closing_year", "2045")], source_type="News report"),
            # A permit placing the facility at our other site, 38 km away.
            source("https://manitoba.example/permit", "Brady Road Landfill", "50.1000, -97.2000", values=[value("operator", "City of Winnipeg")]),
        ],
        "900": [source("https://wiki.example/landfill", "Landfill", values=[value("opening_year", "1960")], source_type="Wiki or aggregator")],
    })
    listed = [(row["site_id"], row["source_url"], row["flags"]) for row in all_rows(run_dir)]
    assert listed == [
        ("900", "https://wiki.example/landfill", ""),
        ("13413", "https://manitoba.example/permit", "more than 5 km from our site"),
        ("13413", "https://news.example/brady", ""),
        ("13413", "open the page at the quote", ""),
    ]


# --- what a row shows --------------------------------------------------------------------------------
def test_a_row_shows_our_site_the_source_and_where_the_source_places_the_facility(tmp_path):
    site = {**BRADY, "original_site_name": "Brady Road RMF", "source_language": "en"}
    run_dir = arbitrate(tmp_path, [site], {"13413": [
        source("https://news.example/brady", "Brady Road Landfill", "49.7590, -97.2010", address="1777 Brady Road, Winnipeg",
               values=[value("closing_year", "2045"), value("facility_status", "Active", "2023")], source_type="News report"),
    ]})
    [row] = site_review(run_dir)
    assert (row["site_id"], row["site_name"], row["original_site_name"], row["source_language"], row["country_iso3"]) == (
        "13413", "Brady Road Resource Management Facility", "Brady Road RMF", "en", "CAN",
    )
    assert row["our_coordinates"] == "49.7583, -97.2004"
    assert (row["source_title"], row["publisher"], row["source_tier"]) == ("Page", "Publisher", "Tier 3")
    assert (row["facility_name_in_source"], row["source_address"]) == ("Brady Road Landfill", "1777 Brady Road, Winnipeg")
    assert (row["stated_coordinates"], row["source_coordinates"], row["coordinates_from"], row["distance_km"]) == (
        "49.7590, -97.2010", "49.759, -97.201", "stated", "0.09",
    )
    assert row["location_note"] == ""
    assert row["flags"] == ""
    assert row["would_load"] == "facility_status = Active (2023)\nclosing_year = 2045 (check below)"


def test_a_location_too_coarse_to_place_is_shown_as_written_with_why(tmp_path):
    """An SME sees the coordinates the source gives even where we couldn't place them, and why, rather
    than blank columns that read as no location given."""
    run_dir = arbitrate(tmp_path, [BRADY], {"13413": [
        source("https://news.example/brady", "Brady Road Landfill", "50.66, -97.20",
               values=[value("closing_year", "2045")], source_type="News report"),
    ]})
    [row] = site_review(run_dir)
    assert (row["stated_coordinates"], row["source_coordinates"], row["distance_km"]) == ("50.66, -97.20", "", "100.26")
    assert row["location_note"] == "coordinates with fewer than 3 decimals are approximate"
    assert row["flags"] == "more than 5 km from our site"


def test_a_value_another_source_contradicts_is_flagged_with_which_loads(tmp_path):
    """One value loads per site, attribute and year, by tier first. Without the flag, an SME approving
    Hutt City Council as the operator might not notice that a Tier 1 page elsewhere on the tab loads WM
    New Zealand instead."""
    run_dir = arbitrate(tmp_path, [BRADY], {"13413": [
        source("https://manitoba.example/permit", "Brady Road Landfill", values=[value("operator", "City of Winnipeg"), value("opening_year", "1973")]),
        source("https://stats.example/table", "Brady Road", values=[value("facility_status", "Active", "2024")], source_type="Government statistics"),
        # Tier 3, so for review: it loses the operator to the permit, agrees on the opening year, and
        # gives a flare nothing else does (without one, it would not be reviewed at all).
        source("https://news.example/brady", "Brady Road", values=[value("operator", "Waste Connections"), value("opening_year", "1973"), value("has_flare", "TRUE")], source_type="News report"),
        # Tier 1 but 6 km off, so for review: approved, its status would replace the statistics'.
        source("https://manitoba.example/notice", "Brady Road", "49.8120, -97.2004", values=[value("facility_status", "Inactive", "2024")]),
    ]})
    flags = {row["source_url"]: row["flags"] for row in site_review(run_dir)}
    assert flags["https://news.example/brady"] == "a Tier 1 source here gives operator = City of Winnipeg; if both are approved, that one loads"
    assert flags["https://manitoba.example/notice"] == (
        "more than 5 km from our site\na Tier 2 source here gives facility_status = Active (2024); if both are approved, this one loads"
    )
    assert flags["https://stats.example/table"] == (
        "a Tier 1 source here gives facility_status = Inactive (2024); if both are approved, that one loads"
    )


def test_would_load_gives_the_year_only_for_a_yearly_attribute():
    """One value loads per site, attribute and year for a yearly attribute, and per site and attribute
    otherwise; lines come in the order the attributes are searched, the newest year first."""
    rows = [
        {"attribute_name": "operator", "value": "Ops Ltd", "year": 2026},
        {"attribute_name": "waste_in_place_metric_tonnes", "value": "1200000", "year": 2019},
        {"attribute_name": "waste_in_place_metric_tonnes", "value": "1500000", "year": 2023},
        {"attribute_name": "facility_status", "value": "Active", "year": 2023},
        # The same value, dated and undated: it loads once.
        {"attribute_name": "operator", "value": "Ops Ltd", "year": 2024},
    ]
    assert would_load(rows).splitlines() == [
        "facility_status = Active (2023)",
        "operator = Ops Ltd",
        "waste_in_place_metric_tonnes = 1500000 (2023)",
        "waste_in_place_metric_tonnes = 1200000 (2019)",
    ]


def test_our_coordinates_paste_straight_into_google_maps():
    """SMEs copy our location into Google Maps' search box, which takes "lat, lon"."""
    assert our_coordinates({"latitude": "45.51234", "longitude": "-73.55432"}) == "45.51234, -73.55432"
    assert our_coordinates({"latitude": "", "longitude": "-73.5"}) == ""


# --- the flags ---------------------------------------------------------------------------------------
def test_the_same_page_and_name_cited_for_two_sites_is_flagged_under_each(tmp_path):
    """Guelph's search cited Brandon's registry page, and so did Brandon's: the page is listed under
    both, each naming the other."""
    brandon = seed("13338", "Eastview Landfill", 49.83866, -99.88844)
    guelph = seed("18499", "Eastview Landfill", 43.5742, -80.2323)
    registry = "https://www.gov.mb.ca/sd/eal/registries/5597brandoneastview/index.html"
    run_dir = arbitrate(tmp_path, [brandon, guelph], {
        "13338": [source(registry, "City of Brandon - Eastview Landfill", values=[value("opening_year", "2015")])],
        "18499": [source(registry, "City of Brandon – Eastview Landfill.", values=[value("operator", "City of Brandon")])],
    })
    flags = {row["site_id"]: row["flags"] for row in site_review(run_dir)}
    assert flags == {
        "13338": "same page and name also cited for site 18499",
        "18499": "same page and name also cited for site 13338",
    }


def test_a_not_a_waste_facility_verdict_is_flagged_and_says_when_the_site_was_found_closed(tmp_path):
    """The likeliest false verdict is a closed landfill misread as never a waste site."""
    offered = {**seed("1", "Alpha", 50.0, -100.0), "contributing_data_sources": "osm_2022"}
    run_dir = arbitrate(tmp_path, [offered], {"1": [
        source("https://a.example", "Alpha Quarry", values=[value("facility_type", NOT_A_WASTE_FACILITY), value("operator", "Quarry Co")]),
        source("https://b.example", "Alpha", values=[value("closing_year", "1998")]),
    ]})
    [row] = [row for row in site_review(run_dir) if row["source_url"] == "https://a.example"]
    assert row["flags"] == (
        f"says {NOT_A_WASTE_FACILITY}, but this run also found closing_year = 1998: is it a closed landfill "
        "misread as never a waste site?"
    )
    assert row["would_load"] == f"facility_type = {NOT_A_WASTE_FACILITY}\noperator = Quarry Co"


def pair(**fields):
    made = Pair("1", "https://a.example", True, "alpha")
    for name, setting in fields.items():
        setattr(made, name, setting)
    return made


def waiting_row(attribute, quote=FOUND, location=""):
    return {"attribute_name": attribute, "status": SME_REVIEW, "quote_on_page": quote, "location_on_page": location}


def test_a_quote_or_location_the_page_doesnt_bear_out_is_flagged():
    """A quote is named by the value it backs; a location is the pair's, and flagged once."""
    rows = [
        waiting_row("operator", NOT_FOUND, PARTLY),
        waiting_row("opening_year", PARTLY, NOT_FOUND),
        waiting_row("closing_year", NOT_FOUND, NOT_FOUND),
        waiting_row("has_cover", NO_PAGE_TEXT),
    ]
    assert pair_flags(pair(), rows, rows, []) == [
        "quote not found on the page (operator, closing_year)",
        "quote only partly found on the page (opening_year)",
        "address or coordinates not found on the page",
    ]
    partly = [waiting_row("operator", FOUND, PARTLY)]
    assert pair_flags(pair(), partly, partly, []) == ["address or coordinates only partly found on the page"]


def test_only_the_quotes_of_values_approving_would_load_are_flagged():
    loading = [waiting_row("operator")]
    confirming = {**waiting_row("facility_type", NOT_FOUND, NOT_FOUND), "status": "not used"}
    assert pair_flags(pair(), [*loading, confirming], loading, []) == ["address or coordinates not found on the page"]


def test_every_flag_at_once_in_the_order_an_sme_reads_them():
    rows = [waiting_row("operator", NOT_FOUND)]
    flagged = pair(shared_with=["7", "12"], distance_km=5.01, far_away=True, not_a_waste_facility=True)
    assert pair_flags(flagged, rows, rows, []) == [
        "quote not found on the page (operator)",
        "same page and name also cited for sites 7, 12",
        "more than 5 km from our site",
        f"says {NOT_A_WASTE_FACILITY}",
    ]
    assert pair_flags(pair(distance_km=5.0, far_away=False), rows[:0], rows[:0], []) == []


@pytest.mark.parametrize("citation, km, far", [
    # A point good enough to show: more than 5 km is far.
    ({"location_basis": "coordinates", "located_latitude": "49.80", "located_longitude": "-97.2004"}, 4.6, False),
    ({"location_basis": "coordinates", "located_latitude": "49.81", "located_longitude": "-97.2004"}, 5.7, True),
    # A hint, good to about 0.7 km: far only beyond 5 km and that.
    ({"location_basis": "hint", "rough_points": ((49.81, -97.2004, 0.75),)}, 5.7, False),
    ({"location_basis": "hint", "rough_points": ((49.82, -97.2004, 0.75),)}, 6.9, True),
    # A doubtful source: its coordinates and its address are both measured, and either can be far.
    ({"location_basis": "none", "rough_points": ((49.7583, -97.2004, 0.0), (50.6583, -97.2004, 0.0))}, 100.1, True),
    # Nothing stated, or nothing read: no check.
    ({"location_basis": "none"}, None, False),
])
def test_how_far_a_pair_is_goes_by_its_point_or_else_its_rough_points(citation, km, far):
    made = pair()
    made.citations = [citation]
    made.measure({"latitude": "49.7583", "longitude": "-97.2004"})
    assert (None if made.distance_km is None else round(made.distance_km, 1), made.far_away) == (km, far)
    assert made.roughly == ("rough_points" in citation)
