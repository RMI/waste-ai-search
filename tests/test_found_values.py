"""Every value the search found is sorted: SME review or not used (found_values.py).

Step 2 of the approve-sources plan, as the key-values plan changed it. The agent returns only
sources about the site searched, so every source is filed under that site; nothing moves a source to
another site any more. A value's status is decided for its site-source pair:

- SME review: it fills an empty field, and loads once an SME approves its pair; a key value from a
  Tier 3-5 pair (value_check "required") only once its own row is marked Correct too. What once sent
  a Tier 1-2 pair to SMEs (the same page and name cited for another of our sites, a point more than
  5 km from ours, or a "Not a Waste Facility" verdict) is now a warning in its status_reason;
- not used: it confirms or would change a value we hold, its link is dead, it couldn't be converted,
  or the agent excluded it.

The approve-sources plan's auto-approval of Tier 1-2 fills is gone (the key-values plan), and the
matcher's tests went with match.py (test_pooled_review.py and test_match.py). These run end to end through run_arbitration, on hand-built source-first responses.
"""
from __future__ import annotations

import csv
import json
import os
import shutil
from collections import Counter
from datetime import date
from pathlib import Path

import pytest
from openpyxl import load_workbook

from waste_ai_search.arbitrate import run_arbitration
from waste_ai_search.arbitration import site_for_year
from waste_ai_search.found_values import (
    AUTO_APPROVED,
    NOT_USED,
    SME_REVIEW,
    pick_values,
    review_id,
)
from waste_ai_search.input_loader import write_csv_records
from waste_ai_search.link_check import BROKEN, LINK_CHECK_FILE, LinkResult, save_results
from waste_ai_search.run_context import PipelineConfig
from waste_ai_search.schema import FOUND_VALUES_HEADERS, NOT_A_WASTE_FACILITY
from waste_ai_search.seed_source import seed_headers

RUN_YEAR = date.today().year


def seed(site_id, name, lat, lon, iso3="CAN", **extra):
    return {
        "facility_id": site_id, "site_id": site_id, "internal_facility_id": site_id, "year": "2022",
        "site_name": name, "facility_name": name, "country_iso3": iso3, "iso3c_plus": iso3,
        "latitude": str(lat), "longitude": str(lon), "contributing_data_sources": "canada_ghgrp_2021",
        "reference_year": "2022", **extra,
    }


def source(url, name="", location="", values=(), source_type="Regulator", published="2024-03-01", address=""):
    """A source as the agent reports one; `location` is the coordinates it states."""
    return {
        "url": url, "source_title": "Page", "publisher": "Publisher", "source_type": source_type,
        "publication_date": published, "facility_name_in_source": name, "facility_name_english": "",
        "stated_address": address, "stated_coordinates": location, "values": list(values),
    }


def value(attribute, found, value_date="2024", unit="", **extra):
    return {"attribute_name": attribute, "value": found, "unit": unit, "value_basis": "Direct",
            "confidence_score": "High", "value_date": value_date, "evidence_summary": "Says so.",
            "quoted_evidence_short": "It says so.", **extra}


def arbitrate(tmp_path, seeds, responses, leftovers=(), links=None, keep_broken_links=False):
    """Arbitrate one run: `responses` maps each site searched to the sources its search cited, or to
    a whole response."""
    run_dir = tmp_path / "run"
    write_csv_records(run_dir / "seed.csv", seeds, seed_headers())
    raw = run_dir / "raw_foundry_responses"
    raw.mkdir(parents=True, exist_ok=True)
    for site_id, sources in responses.items():
        payload = sources if isinstance(sources, dict) else {"site_id": site_id, "sources": sources}
        (raw / f"site_{site_id}.json").write_text(json.dumps(payload))
    for name in leftovers:
        (run_dir / name).write_text("left by an earlier arbitration")
    if links:
        save_results(run_dir / LINK_CHECK_FILE, links)
    run_arbitration(PipelineConfig(
        input_csv=None, run_dir=run_dir, run_id="run", dataset_version="v", keep_broken_links=keep_broken_links,
    ))
    return run_dir


def rows(run_dir, name="found_values.csv"):
    return list(csv.DictReader((run_dir / name).open(encoding="utf-8-sig")))


def by_attribute(run_dir):
    return {row["attribute_name"]: row for row in rows(run_dir) if row["review_id"]}


def run_config(run_dir):
    return {row[0]: row[1] for row in load_workbook(run_dir / "run_review.xlsx")["Run_Config"].values}


# Brady Road, in Winnipeg, and a site of ours 38 km north of it.
BRADY = seed("13413", "Brady Road Resource Management Facility", 49.7583, -97.2004)
NEARBY = seed("13414", "domestic landfill", 50.10, -97.20)


# --- the statuses ---------------------------------------------------------------------------------
def test_every_fill_goes_to_an_sme_and_a_tier_3_to_5_key_value_is_checked_on_its_own(tmp_path):
    """Nothing is approved automatically (key-values plan): an SME checks every source's match. A
    Tier 1-2 source's values are trusted as read; a key value from a Tier 3-5 source is checked too."""
    run_dir = arbitrate(tmp_path, [BRADY], {"13413": [
        source("https://manitoba.example/permit", "Brady Road Landfill",
               values=[value("operator", "City of Winnipeg"), value("closing_year", "2019")]),
        source("https://stats.example/table", "Brady Road", values=[value("opening_year", "1973")], source_type="Government statistics"),
        source("https://news.example/brady", "Brady Road", values=[value("area_square_meters", "12", unit="ha")], source_type="News report"),
        source("https://wiki.example/brady", "Brady Road", values=[value("facility_status", "Active")], source_type="Wiki or aggregator"),
    ]})
    found = by_attribute(run_dir)
    assert [
        (found[name]["source_tier"], found[name]["status"], found[name]["value_check"])
        for name in ("operator", "closing_year", "opening_year", "area_square_meters", "facility_status")
    ] == [
        ("Tier 1", SME_REVIEW, ""), ("Tier 1", SME_REVIEW, ""), ("Tier 2", SME_REVIEW, ""),
        ("Tier 3", SME_REVIEW, "required"), ("Tier 4", SME_REVIEW, ""),
    ]
    assert found["operator"]["status_reason"] == "Tier 1 source, filling an empty field."
    assert found["area_square_meters"]["status_reason"] == "Tier 3 source, filling an empty field."
    assert {(row["sme_decision"], row["value_decision"], row["corrected_value"], row["loaded"]) for row in found.values()} == {("", "", "", "")}
    assert AUTO_APPROVED not in {row["status"] for row in rows(run_dir)}


def test_gas_use_is_not_a_key_value_but_gas_collection_is(tmp_path):
    """Hugh dropped gas use on 30 October: its quotes were mostly a project title, so checking it
    added little. From a Tier 3 source it loads once the source is approved."""
    run_dir = arbitrate(tmp_path, [BRADY], {"13413": [
        source("https://news.example/brady", "Brady Road", source_type="News report", values=[
            value("has_landfill_gas_collection", "Yes"),
            value("gccs_energy_project_type", "Electricity Generation"),
        ]),
    ]})
    found = by_attribute(run_dir)
    assert found["has_landfill_gas_collection"]["value_check"] == "required"
    assert found["gccs_energy_project_type"]["value_check"] == ""


def test_incoming_waste_the_source_says_is_not_municipal_is_not_used(tmp_path):
    """Incoming waste counts municipal waste only, as the standardized spec asks of every source
    (Hugh, 30 October). An amount the source says is mainly rubble, soil or tailings is listed, not
    loaded; one it says nothing about is taken as municipal."""
    run_dir = arbitrate(tmp_path, [BRADY], {"13413": [
        source("https://news.example/rubble", "Brady Road", source_type="News report", values=[
            value("annual_incoming_waste_metric_tonnes", "20000", unit="t", value_date="2023", not_municipal=True),
        ]),
        source("https://manitoba.example/report", "Brady Road Landfill", values=[
            value("annual_incoming_waste_metric_tonnes", "400000", unit="t", value_date="2024", not_municipal=False),
        ]),
    ]})
    amounts = {row["source_url"]: row for row in rows(run_dir) if row["attribute_name"] == "annual_incoming_waste_metric_tonnes"}
    rubble = amounts["https://news.example/rubble"]
    assert (rubble["status"], rubble["not_municipal"]) == (NOT_USED, "TRUE")
    assert rubble["status_reason"].startswith("Not municipal waste: the source says this amount is mainly other waste")
    assert rubble["value_check"] == ""  # nothing for an SME to check
    municipal = amounts["https://manitoba.example/report"]
    assert (municipal["status"], municipal["not_municipal"]) == (SME_REVIEW, "")
    assert "not municipal waste 1" in run_config(run_dir)["values_not_used"]


def test_only_an_incoming_waste_amount_can_be_not_municipal(tmp_path):
    run_dir = arbitrate(tmp_path, [BRADY], {"13413": [
        source("https://manitoba.example/report", "Brady Road Landfill", values=[
            value("area_square_meters", "12", unit="ha", not_municipal=True),
        ]),
    ]})
    [area] = rows(run_dir)
    assert (area["status"], area["not_municipal"]) == (SME_REVIEW, "")


def test_a_waste_type_from_an_older_response_is_no_longer_used(tmp_path):
    """bulk_waste_type was retired on 30 October 2026; a response cached before still gives it."""
    run_dir = arbitrate(tmp_path, [BRADY], {"13413": [
        source("https://manitoba.example/report", "Brady Road Landfill", values=[
            value("annual_incoming_waste_metric_tonnes", "400000", unit="t"),
            value("bulk_waste_type", "municipal solid waste"),
        ]),
    ]})
    [waste_type] = [row for row in rows(run_dir) if row["attribute_name"] == "bulk_waste_type"]
    assert waste_type["status"] == NOT_USED
    assert waste_type["status_reason"].startswith("No longer asked: whether incoming waste is municipal is marked on the amount itself")
    assert "no longer asked 1" in run_config(run_dir)["values_not_used"]


def test_a_source_whose_values_a_better_source_gives_is_still_listed(tmp_path):
    """With nothing approved automatically, a better source's value loads only if an SME approves it,
    so a weaker source giving the same field is reviewed too (719337d's rule is gone)."""
    run_dir = arbitrate(tmp_path, [BRADY], {"13413": [
        source("https://manitoba.example/permit", "Brady Road Landfill", values=[value("operator", "City of Winnipeg")]),
        source("https://news.example/one", "Brady Road", source_type="News report", values=[value("operator", "Waste Connections")]),
    ]})
    found = {row["source_url"]: row for row in rows(run_dir) if row["review_id"]}
    assert found["https://news.example/one"]["status"] == SME_REVIEW


def test_a_planned_closing_year_is_marked_and_not_read_as_a_closure(tmp_path):
    run_dir = arbitrate(tmp_path, [BRADY], {"13413": [
        source("https://news.example/brady", "Brady Road", source_type="News report",
               values=[value("closing_year", "2031", planned=True)]),
        source("https://news.example/other", "Brady Road", source_type="News report",
               values=[value("operator", "City of Winnipeg", planned=True)]),
    ]})
    found = by_attribute(run_dir)
    assert (found["closing_year"]["planned"], found["closing_year"]["value"]) == ("TRUE", "2031")
    # Only a closing_year can be planned.
    assert found["operator"]["planned"] == ""


def test_a_tier_1_source_sent_to_review_by_an_exception_is_reviewed_though_a_tier_2_one_gives_its_value(tmp_path):
    # 6 km from our site, so for review; approved, its Tier 1 status would beat the Tier 2 one.
    run_dir = arbitrate(tmp_path, [BRADY], {"13413": [
        source("https://stats.example/table", "Brady Road", source_type="Government statistics",
               values=[value("facility_status", "Active", "2024")]),
        source("https://manitoba.example/notice", "Brady Road", "49.8120, -97.2004",
               values=[value("facility_status", "Inactive", "2024")]),
    ]})
    found = {row["source_url"]: row for row in rows(run_dir) if row["review_id"]}
    assert found["https://manitoba.example/notice"]["status"] == SME_REVIEW

def test_an_undated_regulator_is_capped_at_tier_3_and_goes_to_an_sme(tmp_path):
    run_dir = arbitrate(tmp_path, [BRADY], {"13413": [
        source("https://manitoba.example/permit", "Brady Road", values=[value("operator", "City of Winnipeg")], published=""),
    ]})
    [operator] = rows(run_dir)
    assert (operator["source_tier"], operator["status"]) == ("Tier 3", SME_REVIEW)
    assert "no publication_date; capped at Tier 3" in operator["tier_rule_applied"]


def test_only_fills_of_empty_fields_load(tmp_path):
    """A value that confirms ours, or would change it, is listed and not used, whatever its tier."""
    site = {**BRADY, "facility_type": "Sanitary Landfill", "facility_status": "Active"}
    run_dir = arbitrate(tmp_path, [site], {"13413": [source("https://manitoba.example/permit", "Brady Road", values=[
        value("facility_type", "sanitary landfill"), value("facility_status", "Closed", "2022"), value("opening_year", "1973"),
    ])]})
    found = by_attribute(run_dir)
    assert (found["facility_type"]["status"], found["facility_type"]["status_reason"]) == (NOT_USED, "Confirms our value.")
    assert found["facility_status"]["status"] == NOT_USED
    assert found["facility_status"]["status_reason"] == "Would change our value, Active: only fills of empty fields load."
    assert found["facility_status"]["our_value"] == "Active"
    assert found["opening_year"]["status"] == SME_REVIEW


def test_a_value_that_cant_be_used_is_listed_as_not_used_with_why(tmp_path):
    run_dir = arbitrate(tmp_path, [BRADY], {"13413": [
        source("https://manitoba.example/permit", "Brady Road", values=[
            value("annual_incoming_waste_metric_tonnes", "30000", unit="tons"),
            value("closing_year", "1980s"),
            value("facility_status", "Planned"),
        ]),
        source("", "Brady Road", values=[value("operator", "City of Winnipeg")]),
    ]})
    found = {row["attribute_name"]: row for row in rows(run_dir)}
    assert {row["status"] for row in found.values()} == {NOT_USED}
    assert "ambiguous" in found["annual_incoming_waste_metric_tonnes"]["status_reason"].lower()
    assert found["annual_incoming_waste_metric_tonnes"]["value_as_written"] == "30000 tons"
    assert "not numeric" in found["closing_year"]["status_reason"]
    assert found["facility_status"]["status_reason"].startswith("Value empty after normalization: facility_status value 'Planned'")
    assert found["operator"]["status_reason"] == "No clickable source URL."
    assert run_config(run_dir)["values_not_used"] == "4: no link 1, could not be converted or mapped 3"


def test_a_dead_link_is_not_used_unless_broken_links_are_kept(tmp_path):
    url = "https://manitoba.example/gone"
    links = {url: LinkResult(url=url, verdict=BROKEN, status="404", resolved_url=url)}
    responses = {"13413": [source(url, "Brady Road", values=[value("operator", "City of Winnipeg")])]}
    [dead] = rows(arbitrate(tmp_path / "a", [BRADY], responses, links=links))
    assert (dead["status"], dead["status_reason"], dead["link_status"]) == (NOT_USED, "Source link broken (404).", BROKEN)
    [kept] = rows(arbitrate(tmp_path / "b", [BRADY], responses, links=links, keep_broken_links=True))
    assert kept["status"] == SME_REVIEW


def test_a_value_the_agent_excluded_is_not_used(tmp_path):
    """Silverstream's fees page, "disposal charges per tonne, not waste accepted per year", came back
    as 299 tonnes a year, marked an excluded candidate. Approved automatically it would load."""
    run_dir = arbitrate(tmp_path, [BRADY], {"13413": [source("https://city.example/fees", "Brady Road", values=[
        value("annual_incoming_waste_metric_tonnes", "299", unit="metric tonnes", value_basis="Inferred", confidence_score="Excluded candidate"),
        value("closing_year", "2027", value_basis="Not found", confidence_score="Low"),
        value("operator", "City of Winnipeg"),
    ])]})
    found = by_attribute(run_dir)
    for name in ("annual_incoming_waste_metric_tonnes", "closing_year"):
        assert (found[name]["status"], found[name]["status_reason"]) == (
            NOT_USED, "The agent marked it an excluded candidate, not a finding.",
        )
    assert found["operator"]["status"] == SME_REVIEW


def test_the_agents_unverified_leads_are_listed_as_not_used(tmp_path):
    response = {"site_id": "13413", "sources": [], "unverified_leads": [
        {"attribute_name": "operator", "value": "GFL", "url": "https://lead.example", "lead_summary": "A forum post."},
        "A lead in prose",
    ]}
    found = rows(arbitrate(tmp_path, [BRADY], {"13413": response}))
    assert [(row["attribute_name"], row["value"], row["source_url"], row["agent_note"], row["status"]) for row in found] == [
        ("operator", "GFL", "https://lead.example", "A forum post.", NOT_USED),
        ("", "", "", "A lead in prose", NOT_USED),
    ]
    assert {row["status_reason"] for row in found} == {"The agent reported it as unverified."}
    assert {row["review_id"] for row in found} == {""}


# --- every source stays with the site searched -----------------------------------------------------
def test_a_source_placing_the_facility_at_another_site_of_ours_stays_with_the_site_searched(tmp_path):
    """The matcher once filed this permit under Brady Road, 0.1 km from where it places the facility.
    Now the agent decides, and the 5 km check sends it to an SME under the site searched."""
    permit = source("https://manitoba.example/permit/brady", "Brady Road Landfill", "49.7590, -97.2010",
                    values=[value("operator", "City of Winnipeg")])
    run_dir = arbitrate(tmp_path, [BRADY, NEARBY], {"13414": [permit]})
    [operator] = rows(run_dir)
    assert (operator["site_id"], operator["site_name"], operator["status"]) == ("13414", "domestic landfill", SME_REVIEW)
    assert operator["status_reason"] == "Tier 1 source, filling an empty field; it places the facility 38 km from our site."
    assert not (run_dir / "outside_run_matches.csv").exists()


def test_a_source_names_and_places_the_facility_in_columns_of_its_own(tmp_path):
    """The address as written, the coordinates as written, and the point read from them, how, and how
    far from our site's coordinates; or why there is none."""
    both = source("https://manitoba.example/permit", "Brady Road Landfill", "49.7590, -97.2010",
                  address="1777 Brady Road, Winnipeg", values=[value("operator", "City of Winnipeg")])
    # Its address was never geocoded, so it places nothing.
    unplaced = source("https://news.example/brady", "Brady Road", address="1777 Brady Road, Winnipeg",
                      values=[value("opening_year", "1973")], source_type="News report")
    found = by_attribute(arbitrate(tmp_path, [BRADY], {"13413": [both, unplaced]}))
    operator = found["operator"]
    assert (operator["facility_name_in_source"], operator["source_address"], operator["stated_coordinates"]) == (
        "Brady Road Landfill", "1777 Brady Road, Winnipeg", "49.7590, -97.2010",
    )
    assert (operator["source_coordinates"], operator["coordinates_from"], operator["distance_km"]) == ("49.759, -97.201", "stated", "0.09")
    opening = found["opening_year"]
    assert (opening["source_coordinates"], opening["coordinates_from"], opening["distance_km"]) == ("", "", "")
    assert opening["location_note"].startswith("not located yet")


def test_a_grid_reference_is_converted_and_says_so(tmp_path):
    cowpen = seed("18572", "Cowpen Bewley Landfill Site", 54.6136, -1.24064, "GBR")
    register = source("https://environment.data.gov.uk/EPR-RP3531DV", "Cowpen Bewley Landfill Site", "NZ4914124539",
                      values=[value("operator", "Augean")])
    [operator] = rows(arbitrate(tmp_path, [cowpen], {"18572": [register]}))
    assert (operator["coordinates_from"], operator["status"]) == ("grid reference", SME_REVIEW)
    assert float(operator["distance_km"]) < 0.1


# --- the exceptions -------------------------------------------------------------------------------
def test_the_5_km_check_needs_coordinates_on_both_sides(tmp_path):
    """No coordinates means no check, as in batch 1: a Tier 1-2 source giving none auto-approves, and
    so does one at a site of ours with none."""
    unplaced = seed("1", "Alpha Landfill", "", "")
    run_dir = arbitrate(tmp_path, [BRADY, unplaced], {
        "13413": [
            source("https://a.example/near", "Brady Road", "49.7800, -97.2004", values=[value("operator", "City of Winnipeg")]),
            source("https://a.example/silent", "Brady Road", values=[value("opening_year", "1973")]),
            source("https://a.example/far", "Brady Road", "49.8100, -97.2004", values=[value("closing_year", "2045")]),
        ],
        "1": [source("https://b.example", "Alpha Landfill", "10.0, 10.0", values=[value("operator", "Alpha Ltd")])],
    })
    status = {(row["site_id"], row["attribute_name"]): (row["status"], row["distance_km"]) for row in rows(run_dir)}
    assert status == {
        ("13413", "operator"): (SME_REVIEW, "2.41"),
        ("13413", "opening_year"): (SME_REVIEW, ""),
        ("13413", "closing_year"): (SME_REVIEW, "5.75"),
        ("1", "operator"): (SME_REVIEW, ""),
    }


def test_coordinates_far_away_go_to_an_sme_even_where_their_address_disagrees(tmp_path):
    """Coordinates and an address more than 2 km apart are doubtful, so neither is shown as where the
    source places the facility; but coordinates 100 km from ours are still a reason to look. The check
    measures both, and either can send the pair to an SME."""
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    # The locate step's answer: the address on its building, 3 km from the coordinates stated.
    (run_dir / "source_locations.json").write_text(json.dumps({"CAN|1 Other Road, Elsewhere MB": {
        "text": "1 Other Road, Elsewhere MB", "country": "CAN", "latitude": 50.6853, "longitude": -97.2004,
        "basis": "rooftop", "reason": "", "geocoder": "nominatim", "match": "",
    }}))
    permit = source("https://a.example/permit", "Brady Road Landfill", "50.6583, -97.2004",
                    address="1 Other Road, Elsewhere MB", values=[value("operator", "City of Winnipeg")])
    [row] = rows(arbitrate(tmp_path, [BRADY], {"13413": [permit]}))
    assert (row["source_coordinates"], row["distance_km"]) == ("", "103.08")
    assert row["location_note"] == "doubtful: its address lands 3.0 km from its coordinates"
    assert (row["status"], row["status_reason"]) == (SME_REVIEW, "Tier 1 source, filling an empty field; it places the facility roughly 103 km from our site.")


def test_the_same_page_and_name_cited_for_two_sites_goes_to_smes_for_both(tmp_path):
    """Guelph's search cited Brandon's registry page, and so did Brandon's. Both pairs go to an SME,
    and possible_duplicates.csv lists the two sites."""
    brandon = seed("13338", "Eastview Landfill", 49.83866, -99.88844)
    guelph = seed("18499", "Eastview Landfill", 43.5742, -80.2323)
    registry = "https://www.gov.mb.ca/sd/eal/registries/5597brandoneastview/index.html"
    run_dir = arbitrate(tmp_path, [brandon, guelph], {
        "13338": [source(registry, "City of Brandon - Eastview Landfill", values=[value("opening_year", "2015")])],
        # The same name, spelled with a dash and a full stop.
        "18499": [source(registry, "City of Brandon – Eastview Landfill.", values=[value("operator", "City of Brandon")])],
    })
    found = {row["site_id"]: row for row in rows(run_dir)}
    assert found["13338"]["status_reason"] == "Tier 1 source, filling an empty field; the same page and name are also cited for site 18499."
    assert found["18499"]["status_reason"] == "Tier 1 source, filling an empty field; the same page and name are also cited for site 13338."
    [pair] = rows(run_dir, "possible_duplicates.csv")
    assert (pair["site_id_a"], pair["site_id_b"], pair["source_urls"]) == ("13338", "18499", registry)
    config = run_config(run_dir)
    assert (config["pairs_with_warnings"], config["possible_duplicates"]) == ("2: same page and name as another site 2", 1)


def test_two_spellings_of_one_name_on_one_page_are_the_same_page_and_name(tmp_path):
    """In the mini test, Brandon's search cited a Manitoba report as "The Eastview Landfill" and
    Guelph's as "Eastview Landfill"; with no address it would have been approved at Guelph. Names are
    compared by their distinctive words for this. Each spelling is still its own pair, and one
    search's two spellings are not a second site."""
    brandon = seed("13338", "Eastview Landfill", 49.83866, -99.88844)
    guelph = seed("18499", "Eastview Landfill", 43.5742, -80.2323)
    report = "https://www.gov.mb.ca/sd/eal/registries/eap/eap_20141215.pdf"
    run_dir = arbitrate(tmp_path, [brandon, guelph], {
        "13338": [
            source(report, "The Eastview Landfill", values=[value("opening_year", "2015")]),
            source(report, "Eastview Landfill site", values=[value("closing_year", "2060")]),
        ],
        "18499": [source(report, "Eastview Landfill", values=[value("operator", "City of Brandon")])],
    })
    found = {(row["site_id"], row["attribute_name"]): row for row in rows(run_dir)}
    assert found[("18499", "operator")]["status_reason"] == "Tier 1 source, filling an empty field; the same page and name are also cited for site 13338."
    assert found[("13338", "opening_year")]["status_reason"] == "Tier 1 source, filling an empty field; the same page and name are also cited for site 18499."
    assert found[("13338", "opening_year")]["review_id"] != found[("13338", "closing_year")]["review_id"]
    [pair] = rows(run_dir, "possible_duplicates.csv")
    assert (pair["site_id_a"], pair["site_id_b"]) == ("13338", "18499")

    alone = arbitrate(tmp_path / "alone", [brandon], {"13338": [
        source(report, "The Eastview Landfill", values=[value("opening_year", "2015")]),
        source(report, "Eastview Landfill site", values=[value("closing_year", "2060")]),
    ]})
    assert {row["status"] for row in rows(alone)} == {SME_REVIEW}


def test_one_page_naming_different_facilities_for_two_sites_is_no_duplicate(tmp_path):
    """A regulator's list gives each site its own entry: a different name is a different source. A
    page cited with no name for both is one name, though, and goes to an SME for each; but a blank
    name says nothing of the two being one facility, so they aren't listed as possible duplicates."""
    council = [seed("101", "Smith Road Landfill", -41.0, 175.0, "NZL"), seed("102", "Jones Road Landfill", -41.09, 175.0, "NZL")]
    register = "https://council.example.nz/closed-landfills"
    named = arbitrate(tmp_path / "named", council, {
        "101": [source(register, "Smith Road Landfill", values=[value("operator", "Council")])],
        "102": [source(register, "Jones Road Landfill", values=[value("operator", "Council")])],
    })
    assert {row["status"] for row in rows(named)} == {SME_REVIEW}
    assert rows(named, "possible_duplicates.csv") == []
    unnamed = arbitrate(tmp_path / "unnamed", council, {
        "101": [source(register, values=[value("operator", "Council")])],
        "102": [source(register, values=[value("operator", "Council")])],
    })
    assert {row["status"] for row in rows(unnamed)} == {SME_REVIEW}
    assert rows(unnamed, "possible_duplicates.csv") == []


def test_a_not_a_waste_facility_verdict_sends_its_whole_pair_to_an_sme(tmp_path):
    """Where the site was offered the check (WP-525). Where it wasn't, the verdict isn't used, but the
    source saying so is still doubtful, so its other values go to an SME too."""
    offered = {**seed("1", "Alpha", 50.0, -100.0), "contributing_data_sources": "osm_2022"}
    corroborated = seed("2", "Beta", 51.0, -100.0)
    verdict = [value("facility_type", NOT_A_WASTE_FACILITY), value("operator", "Quarry Co")]
    run_dir = arbitrate(tmp_path, [offered, corroborated], {
        "1": [source("https://a.example", "Alpha Quarry", values=verdict)],
        "2": [source("https://b.example", "Beta Quarry", values=verdict)],
    })
    found = {(row["site_id"], row["attribute_name"]): row for row in rows(run_dir)}
    assert found[("1", "facility_type")]["status"] == SME_REVIEW
    assert found[("1", "operator")]["status_reason"] == f"Tier 1 source, filling an empty field; it says the site is {NOT_A_WASTE_FACILITY}."
    assert found[("2", "facility_type")]["status"] == NOT_USED
    assert "this site was not offered it" in found[("2", "facility_type")]["status_reason"]
    assert found[("2", "operator")]["status"] == SME_REVIEW
    assert run_config(run_dir)["not_a_waste_facility"] == 1


def test_a_verdict_beside_a_closure_says_to_check_for_a_closed_landfill(tmp_path):
    offered = {**seed("1", "Alpha", 50.0, -100.0), "contributing_data_sources": "osm_2022"}
    run_dir = arbitrate(tmp_path, [offered], {"1": [
        source("https://a.example", "Alpha", values=[value("facility_type", NOT_A_WASTE_FACILITY)]),
        source("https://b.example", "Alpha", values=[value("closing_year", "1998")]),
    ]})
    verdict = by_attribute(run_dir)["facility_type"]
    assert "CHECK: this run also found closing_year = 1998" in verdict["status_reason"]


# --- per site-source pair ---------------------------------------------------------------------------
def test_a_page_about_two_of_our_sites_is_two_pairs_each_loading_its_own_values(tmp_path):
    """A council page cited by two searches, under each site's own name: each pair is decided on its
    own, and each loads only its site's values."""
    council = [seed("101", "Smith Road Landfill", -41.0, 175.0, "NZL"), seed("102", "Jones Road Landfill", -41.5, 175.0, "NZL")]
    page = "https://council.example.nz/landfills"
    run_dir = arbitrate(tmp_path, council, {
        "101": [source(page, "Smith Road Landfill", "-41.0001, 175.0001", values=[value("operator", "Council")])],
        # Placed at Smith Road, 55 km from Jones Road.
        "102": [source(page, "Jones Road Landfill", "-41.0001, 175.0001", values=[value("opening_year", "1990")])],
    })
    found = {row["site_id"]: row for row in rows(run_dir)}
    assert (found["101"]["attribute_name"], found["101"]["status"]) == ("operator", SME_REVIEW)
    assert (found["102"]["attribute_name"], found["102"]["status"]) == ("opening_year", SME_REVIEW)
    assert found["101"]["review_id"] != found["102"]["review_id"]


def test_a_review_id_is_the_same_whenever_the_site_cites_the_page_under_that_name(tmp_path):
    page = "https://manitoba.example/permit"
    responses = {"13413": [source(page, "Brady Road Landfill", values=[value("operator", "City of Winnipeg"), value("opening_year", "1973")])]}
    first = {row["review_id"] for row in rows(arbitrate(tmp_path / "a", [BRADY], responses))}
    again = {row["review_id"] for row in rows(arbitrate(tmp_path / "b", [BRADY], responses))}
    assert first == again == {review_id("13413", page, "brady road landfill")}
    renamed = {"13413": [source(page, "Brady Road", values=[value("operator", "City of Winnipeg")])]}
    assert {row["review_id"] for row in rows(arbitrate(tmp_path / "c", [BRADY], renamed))} != first


def test_a_page_described_twice_by_one_search_takes_its_best_tier(tmp_path):
    """Cowpen Bewley's register entry: the first pass dated it, Tier 1; the follow-up pass gave it no
    date, Tier 3. It is one page, and one pair, and every value of it shows the pair's tier."""
    cowpen = seed("18572", "Cowpen Bewley Landfill Site", 54.6136, -1.24064, "GBR", has_landfill_gas_collection="TRUE")
    register = "https://environment.data.gov.uk/EPR-RP3531DV"
    run_dir = tmp_path / "run"
    raw = run_dir / "raw_foundry_responses"
    raw.mkdir(parents=True)
    (raw / "site_18572__gccs.json").write_text(json.dumps({"sources": [
        source(register, "Cowpen Bewley Landfill Site", values=[value("has_flare", "Yes")], source_type="Permit or registry", published=""),
    ]}))
    found = arbitrate(tmp_path, [cowpen], {"18572": [
        source(register, "Cowpen Bewley Landfill Site", values=[value("operator", "Augean")], source_type="Permit or registry"),
    ]})
    assert {(row["attribute_name"], row["source_tier"], row["status"]) for row in rows(found)} == {
        ("operator", "Tier 1", SME_REVIEW), ("has_flare", "Tier 1", SME_REVIEW),
    }


# --- years ---------------------------------------------------------------------------------------
def test_a_value_is_the_year_its_date_starts_with_else_the_runs(tmp_path):
    run_dir = arbitrate(tmp_path, [BRADY], {"13413": [source("https://a.example", "Brady Road", values=[
        value("facility_status", "Active", "2024-05"), value("operator", "Ops Ltd", ""),
        # A value_date before 1950 is no year a value applies to: it counts as undated.
        value("closing_year", "2045", "1890"),
    ])]})
    years = {row["attribute_name"]: row["year"] for row in rows(run_dir)}
    assert years == {"facility_status": "2024", "operator": str(RUN_YEAR), "closing_year": str(RUN_YEAR)}
    assert run_config(run_dir)["arbitrated_at"].startswith(str(RUN_YEAR))


def test_a_yearly_baseline_is_ours_for_its_own_year_and_else_our_latest(tmp_path):
    """The seed holds one row per facility, at its reference year, with a yearly field backfilled from
    the newest earlier year that had one. For that latest year we hold nothing, so a value fills it;
    for its own year and any other, ours stands."""
    site = seed("1", "Alpha Landfill", 50.0, -100.0, waste_in_place_metric_tonnes="1000000",
                backfilled_fields="waste_in_place_metric_tonnes@2016",
                attribute_sources="waste_in_place_metric_tonnes@canada_ghgrp_2021")
    attribute = "waste_in_place_metric_tonnes"
    same, note = site_for_year(site, attribute, 2016)
    assert (same[attribute], note) == ("1000000", "")
    latest, note = site_for_year(site, attribute, 2022)
    assert latest[attribute] == "" and note == "We hold no waste_in_place_metric_tonnes for 2022; our value, 1000000, is from 2016."
    other, note = site_for_year(site, attribute, 2010)
    assert other is site and note.startswith("Our value is for 2016;")

    run_dir = arbitrate(tmp_path, [site], {"1": [
        source(f"https://{year}.example", "Alpha Landfill", values=[value(attribute, found, str(year), unit="metric tonnes")])
        for year, found in ((2016, "1000000"), (2022, "1200000"), (2010, "700000"))
    ]})
    found = {row["year"]: row for row in rows(run_dir)}
    assert (found["2016"]["status"], found["2016"]["status_reason"]) == (NOT_USED, "Confirms our value.")
    assert found["2022"]["status"] == SME_REVIEW and found["2022"]["our_value"] == ""
    assert found["2022"]["status_reason"].endswith("our value, 1000000, is from 2016.")
    assert found["2010"]["status"] == NOT_USED and "Our value is for 2016" in found["2010"]["status_reason"]


def test_one_row_per_site_year_source_and_attribute(tmp_path):
    """A source giving one attribute twice for one year keeps the newest, and says what else it gave."""
    run_dir = arbitrate(tmp_path, [BRADY], {"13413": [source("https://a.example", "Brady Road", values=[
        value("facility_status", "Inactive", "2024-01"), value("facility_status", "Active", "2024-06"),
        value("facility_status", "Active", "2019"),
    ])]})
    found = rows(run_dir)
    assert [(row["year"], row["value"]) for row in found] == [("2024", "Active"), ("2019", "Active")]
    assert found[0]["status_reason"].endswith("The source also gives Inactive (2024-01).")
    keys = Counter((row["site_id"], row["year"], row["review_id"], row["attribute_name"]) for row in found)
    assert max(keys.values()) == 1


# --- choosing one value to load -----------------------------------------------------------------
def found_row(found, tier, value_date="2024", source="s", status=AUTO_APPROVED, attribute="operator", year=2024):
    return {"site_id": "1", "attribute_name": attribute, "year": year, "value": found, "source_tier": tier,
            "value_date": value_date, "source_id": source, "source_url": source, "status": status}


def test_one_value_is_loaded_per_site_attribute_and_year_best_tier_then_newest_then_most_sources():
    rows_ = [
        found_row("A", "Tier 2", "2024-01", "s1"), found_row("B", "Tier 1", "2020", "s2"),  # tier first
        found_row("C", "Tier 1", "2023", "s3", attribute="closing_year"), found_row("D", "Tier 1", "2024", "s4", attribute="closing_year"),
        found_row("E", "Tier 1", "2024", "s5", attribute="opening_year"), found_row("F", "Tier 1", "2024", "s6", attribute="opening_year"),
        found_row("F", "Tier 1", "2024", "s7", attribute="opening_year"),
        found_row("G", "Tier 1", "2024", "s8", status=NOT_USED, attribute="has_cover"),
    ]
    picked = {key[1]: row["value"] for key, row in pick_values(rows_).items()}
    assert picked == {"operator": "B", "closing_year": "D", "opening_year": "F"}


@pytest.mark.parametrize("lower_date", ["2030", "1940", "2026-11"])
def test_a_better_tier_always_wins_within_one_key_however_the_other_is_dated(lower_date):
    """A status dated after the run's year, or before 1950, counts as the run's year, so it shares a
    key with this year's. It once won from Tier 3 over a Tier 1 status, as a status years newer."""
    rows_ = [
        found_row("Active", "Tier 1", "2026-02", "s1", attribute="facility_status", year=2026),
        found_row("Inactive", "Tier 3", lower_date, "s2", attribute="facility_status", year=2026),
    ]
    [(key, row)] = pick_values(rows_).items()
    assert (key, row["value"], row["source_tier"]) == (("1", "facility_status", 2026), "Active", "Tier 1")


def test_a_yearly_attribute_loads_one_value_per_year_and_any_other_one_per_site():
    rows_ = [
        found_row("Active", "Tier 1", "2024", attribute="facility_status", year=2024),
        found_row("Inactive", "Tier 1", "2019", attribute="facility_status", year=2019),
        found_row("1990", "Tier 1", "2024", attribute="opening_year", year=2024),
        found_row("1991", "Tier 2", "2019", attribute="opening_year", year=2019),
    ]
    assert sorted(pick_values(rows_)) == [("1", "facility_status", 2019), ("1", "facility_status", 2024), ("1", "opening_year", "")]


# --- the files -----------------------------------------------------------------------------------
def test_found_values_csv_replaces_the_evidence_the_decisions_the_queue_and_the_leads(tmp_path, capsys):
    """Arbitrating a run arbitrated before removes what that arbitration wrote and this one doesn't."""
    retired = ["evidence.csv", "resolved.csv", "review_queue.csv", "supplementary_leads.csv", "outside_run_matches.csv",
               "std_facility_tbl_ai_search_run.csv", "std_facility_tbl_ai_search_run.load.sql"]
    run_dir = arbitrate(tmp_path, [BRADY], {"13413": [source("https://a.example", "Brady Road", values=[value("operator", "Ops")])]},
                        leftovers=retired)
    assert not [name for name in retired if (run_dir / name).exists()]
    assert "Removed std_facility_tbl_ai_search_run.csv, std_facility_tbl_ai_search_run.load.sql, evidence.csv" in capsys.readouterr().out
    with (run_dir / "found_values.csv").open(encoding="utf-8-sig") as handle:
        assert next(csv.reader(handle)) == FOUND_VALUES_HEADERS
    workbook = load_workbook(run_dir / "run_review.xlsx")
    assert workbook.sheetnames == ["ReadMe", "Run_Config", "Site_Review", "Parse_Warnings", "Definitions"]


def test_batch_1s_resolved_review_queue_and_leads_columns_are_kept_for_its_re_key():
    """Nothing writes resolved.csv, a Review_Queue or Leads now, but #26's re-key imports their columns to
    check batch 1's files, and git merges the two without a conflict to warn anyone. They are batch
    1's columns, not the stack's: no match columns, and the queue's translation_note."""
    from waste_ai_search.schema import RESOLVED_HEADERS, REVIEW_QUEUE_HEADERS, SUPPLEMENTARY_LEADS_HEADERS

    assert RESOLVED_HEADERS[:3] == ["site_id", "internal_facility_id", "site_name"]
    assert {"resolved_value", "resolution", "validation_status"} <= set(RESOLVED_HEADERS)
    assert {"translation_note", "baseline_coordinates", "rejection_reason", "corrected_value"} <= set(REVIEW_QUEUE_HEADERS)
    assert not {"searched_site_id", "match", "corrected_site_id", "stated_location"} & set(REVIEW_QUEUE_HEADERS)
    assert {"lead_value", "lead_summary", "exclusion_reason"} <= set(SUPPLEMENTARY_LEADS_HEADERS)


def test_run_config_counts_the_pairs_and_values_by_status(tmp_path):
    site = {**BRADY, "facility_type": "Sanitary Landfill"}
    run_dir = arbitrate(tmp_path, [site, NEARBY], {
        "13413": [
            source("https://a.example", "Brady Road", values=[value("opening_year", "1973"), value("closing_year", "2045")]),
            source("https://b.example", "Brady Road", values=[value("facility_status", "Active")], source_type="News report"),
            source("https://c.example", "Brady Road", values=[value("facility_type", "Sanitary Landfill")]),
        ],
        "13414": [source("https://d.example", "Brady Road", "49.7590, -97.2010", values=[value("operator", "Winnipeg")])],
    })
    config = run_config(run_dir)
    assert config["site_source_pairs"] == "3: Tier 1-2 2, Tier 3-5 1 (4 values)"
    assert config["pairs_with_warnings"] == "1: more than 5 km away 1"
    # The news report's facility_status isn't a key value.
    assert config["values_to_check"] == 0
    assert config["values_not_used"] == "1: confirms our value 1"


def test_new_facility_candidates_keeps_only_those_smes_listed(tmp_path):
    """Arbitration lists no candidates of its own since the matcher went; an earlier arbitration's are
    dropped, and an SME's kept."""
    from waste_ai_search.lists import FOUND_BY_ARBITRATION, FOUND_BY_SME, NEW_FACILITY_FILE, NEW_FACILITY_HEADERS

    run_dir = tmp_path / "run"
    write_csv_records(run_dir / NEW_FACILITY_FILE, [
        {"candidate_id": "WAINEW-1", "found_by": FOUND_BY_ARBITRATION}, {"candidate_id": "WAINEW-2", "found_by": FOUND_BY_SME},
    ], NEW_FACILITY_HEADERS)
    run_dir = arbitrate(tmp_path, [BRADY], {"13413": [source("https://a.example", "Brady Road", values=[value("operator", "Ops")])]})
    assert [row["candidate_id"] for row in rows(run_dir, NEW_FACILITY_FILE)] == ["WAINEW-2"]
    assert run_config(run_dir)["new_facility_candidates"] == 1


def test_a_page_listing_several_landfills_is_listed_for_ingestion(tmp_path):
    council = [seed("101", "Smith Road Landfill", -41.0, 175.0, "NZL"), seed("102", "Jones Road Landfill", -41.09, 175.0, "NZL")]
    register = "https://council.example.nz/closed-landfills"
    run_dir = arbitrate(tmp_path, council, {
        "101": [source(register, "Smith Road Landfill", "-41.0031, 175.0041", values=[value("operator", "Council")])],
        "102": [source(register, "Jones Road Landfill", "-41.0931, 175.0041", values=[value("operator", "Council")])],
    })
    [page] = rows(run_dir, "datasets_found.csv")
    assert (page["source_url"], page["searches"], page["why"]) == (register, "2", "names 2 facilities; places 2 facilities")
    assert run_config(run_dir)["datasets_found"] == 1


def test_a_source_with_no_values_is_still_listed_in_sources_csv(tmp_path):
    run_dir = arbitrate(tmp_path, [BRADY], {"13413": [
        source("https://a.example", "Brady Road", values=[value("operator", "Ops")]),
        source("https://b.example", "Brady Road Landfill", "49.7590, -97.2010"),
    ]})
    assert sorted(row["url"] for row in rows(run_dir, "sources.csv")) == ["https://a.example", "https://b.example"]
    assert [row["source_url"] for row in rows(run_dir)] == ["https://a.example"]


# --- the gas follow-up --------------------------------------------------------------------------
class _GasClient:
    """Pass 1 finds gas collection; the follow-up gives a GCCS value."""

    def __init__(self):
        self.prompts = []

    def search_site(self, site, prompt):  # noqa: ARG002
        self.prompts.append(prompt)
        page = source("https://reg.example/permit", "First Landfill", "6.5001, 3.3001", published="2024-01-01")
        if "gccs_ch4_flared_metric_tonnes" in prompt:
            page["values"] = [value("gccs_ch4_flared_metric_tonnes", "1200", unit="metric tonnes")]
        else:
            page["values"] = [value("has_landfill_gas_collection", "Yes")]
        return {"sources": [page]}


def test_the_follow_up_searches_the_site_whose_search_found_gas_collection(tmp_path, monkeypatch):
    from waste_ai_search import arbitrate as ar
    from waste_ai_search import cli, search as se

    seed_csv = tmp_path / "seed.csv"
    seed_csv.write_text(
        "site_id,internal_facility_id,site_name,country_iso3,latitude,longitude,"
        "is_location_exact,has_landfill_gas_collection,reference_year\n"
        "1,1,First Landfill,PHL,6.5,3.3,TRUE,,2022\n"
        "2,2,Second Landfill,PHL,6.6,3.3,TRUE,,2022\n",
        encoding="utf-8",
    )
    client = _GasClient()
    monkeypatch.setattr(se, "get_client", lambda config: client)
    monkeypatch.setattr(ar, "write_review_workbook", lambda path, **kw: path)
    cli.run_everything(PipelineConfig(input_csv=seed_csv, run_dir=tmp_path / "run", run_id="t", site_delay_seconds=0, limit=1))

    assert len(client.prompts) == 2
    assert "First Landfill" in client.prompts[1] and "gccs_ch4_flared_metric_tonnes" in client.prompts[1]


def test_a_refresh_merges_values_auto_approved_or_for_review_the_newest_year_of_each(tmp_path):
    from waste_ai_search.cli import read_arbitrated_rows
    from waste_ai_search.seed_refresh import refresh_sites

    run_dir = arbitrate(tmp_path, [BRADY], {"13413": [
        source("https://a.example", "Brady Road", values=[value("facility_status", "Inactive", "2019")]),
        source("https://b.example", "Brady Road", values=[value("facility_status", "Active", "2024"),
                                                         value("has_landfill_gas_collection", "Yes")], source_type="News report"),
    ]})
    (refreshed,), stats = refresh_sites([dict(BRADY)], read_arbitrated_rows(run_dir), "run")
    assert (refreshed["facility_status"], refreshed["has_landfill_gas_collection"]) == ("Active", "TRUE")
    assert stats["gas_collection_discovered"] == 1
    assert refreshed["ai_filled_fields"] == "facility_status@Tier 3; has_landfill_gas_collection@Tier 3"


# --- batch 1, replayed --------------------------------------------------------------------------
REPLAY_RUN = os.environ.get("WASTE_AI_SEARCH_REPLAY_RUN", "")


@pytest.mark.skipif(not REPLAY_RUN, reason="set WASTE_AI_SEARCH_REPLAY_RUN to a batch-1 run folder to replay it")
def test_a_batch_1_replay_never_has_two_rows_for_one_site_year_source_and_attribute(tmp_path):
    """Batch 1's cached responses, re-arbitrated offline: a plumbing check. The run folder holds
    seed.csv and raw_foundry_responses/, and link_check.json if it was checked. It is copied, so
    the folder itself is left as it is."""
    source_dir = Path(REPLAY_RUN)
    run_dir = tmp_path / "batch1"
    run_dir.mkdir()
    shutil.copy(source_dir / "seed.csv", run_dir / "seed.csv")
    if (source_dir / "link_check.json").exists():
        shutil.copy(source_dir / "link_check.json", run_dir / "link_check.json")
    (run_dir / "raw_foundry_responses").symlink_to((source_dir / "raw_foundry_responses").resolve())
    run_arbitration(PipelineConfig(input_csv=None, run_dir=run_dir, run_id="batch1", dataset_version="v"))

    found = [row for row in rows(run_dir) if row["review_id"]]
    assert found
    keys = Counter((row["site_id"], row["year"], row["review_id"], row["attribute_name"]) for row in found)
    assert [key for key, count in keys.items() if count > 1] == []
