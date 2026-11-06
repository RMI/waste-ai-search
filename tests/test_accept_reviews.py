"""accept-reviews loads what SMEs approve: the pairs, and the key values under them (accept_reviews.py).

Step 4 of the approve-sources plan, as the key-values plan changed it. SMEs decide each site-source
pair on the Site_Review tab: Correct site, Different site (with other_site_id), New site or Junk; and
each key value of a Tier 3-5 pair on its own row: Correct, or Wrong with an optional corrected_value.
accept-reviews reads the decisions back by review_id (and check, for a value row), applies them to
found_values.csv's rows, picks one value per site, attribute and year where accepted values disagree,
and writes accepted_values.csv for ingestion, found_values.csv's sme_decision, value_decision,
corrected_value and loaded, and the New site sources to new_facility_candidates.csv. It replaces collect-new-facilities,
which read "New facility - not in our database" rejections off the Review_Queue that Site_Review
replaced (its tests, test_new_facilities.py, went with it).

Most of these run end to end: arbitrate hand-built source-first responses, record decisions in the
workbook as an SME would, and accept them. The pick between disagreeing values is tested on found
values built by hand.
"""
from __future__ import annotations

import csv
import json
from datetime import datetime
from types import SimpleNamespace

import pytest
from openpyxl import load_workbook

from waste_ai_search.accept_reviews import (
    ACCEPTED_VALUES_FILE,
    Decision,
    ReviewError,
    ValueDecision,
    accept,
    accept_reviews,
    read_decisions,
    read_value_decisions,
    reviewed_workbook,
    value_warnings,
)
from waste_ai_search.arbitrate import run_arbitration
from waste_ai_search.cli import main
from waste_ai_search.found_values import AUTO_APPROVED, NOT_USED, SME_REVIEW
from waste_ai_search.input_loader import write_csv_records
from waste_ai_search.run_context import PipelineConfig
from waste_ai_search.schema import ACCEPTED_VALUES_HEADERS, CORRECT_SITE, DIFFERENT_SITE, JUNK, NEW_SITE
from waste_ai_search.seed_source import seed_headers
from waste_ai_search.site_review import checked_on_page
from waste_ai_search.workbook_io import write_review_workbook


def seed(site_id, name, lat, lon, iso3="CAN", **extra):
    return {
        "facility_id": site_id, "site_id": site_id, "internal_facility_id": site_id, "year": "2022",
        "site_name": name, "facility_name": name, "country_iso3": iso3, "iso3c_plus": iso3,
        "latitude": str(lat), "longitude": str(lon), "contributing_data_sources": "canada_ghgrp_2021",
        "reference_year": "2022", **extra,
    }


def source(url, name="", location="", values=(), source_type="News report", address=""):
    """A source as the agent reports one; `location` is the coordinates it states. A news report is
    Tier 3, so its pair waits on an SME."""
    return {
        "url": url, "source_title": "Page", "publisher": "Publisher", "source_type": source_type,
        "publication_date": "2024-03-01", "facility_name_in_source": name, "facility_name_english": "",
        "stated_address": address, "stated_coordinates": location, "values": list(values),
    }


def value(attribute, found, value_date="2024"):
    return {"attribute_name": attribute, "value": found, "unit": "", "value_basis": "Direct",
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


def decide(run_dir, decisions):
    """Record decisions in the run's workbook as an SME would: `decisions` maps a source_url (and,
    where a page is listed under two sites, its site_id) to the cells to fill in on its source row,
    and (source_url, check) to those on one of its value rows."""
    path = run_dir / "run_review.xlsx"
    book = load_workbook(path)
    ws = book["Site_Review"]
    column = {cell.value: cell.column for cell in ws[1]}
    url_of = {}
    for row in range(2, ws.max_row + 1):
        url, site_id = ws.cell(row, column["source_url"]).value, ws.cell(row, column["site_id"]).value
        review, check = ws.cell(row, column["review_id"]).value, ws.cell(row, column["check"]).value
        if check and not checked_on_page(check):
            cells = decisions.get((url_of.get(review), check))
        else:
            url_of[review] = url
            cells = decisions.get((url, site_id), decisions.get(url))
        for name, typed in (cells or {}).items():
            ws.cell(row, column[name]).value = typed
    book.save(path)
    return path


def rows(path):
    with path.open(newline="", encoding="utf-8-sig") as handle:
        return list(csv.DictReader(handle))


def accepted(run_dir):
    return rows(run_dir / ACCEPTED_VALUES_FILE)


def found(run_dir):
    return rows(run_dir / "found_values.csv")


SME = {"reviewer": "A. Reviewer", "reviewed_date": datetime(2026, 11, 2)}

# Brady Road, in Winnipeg, and a site of ours 38 km north of it.
BRADY = seed("13413", "Brady Road Resource Management Facility", 49.7583, -97.2004, opening_year="1973")
NEARBY = seed("900", "domestic landfill", 50.10, -97.20, closing_year="2010")
PERMIT = source("https://manitoba.example/permit", "Brady Road Landfill", source_type="Regulator",
                values=[value("facility_type", "Sanitary Landfill")])
NEWS = source("https://news.example/landfill", "Springfield Landfill",
              values=[value("operator", "RM of Springfield"), value("opening_year", "1960"), value("closing_year", "2045")])


# --- each decision -------------------------------------------------------------------------------
def test_nothing_loads_before_an_sme_decides_whatever_the_tier(tmp_path):
    """accept-reviews can run before review is finished: a pair with no decision loads nothing yet,
    a Tier 1 one included (key-values plan: nothing is approved automatically)."""
    run_dir = arbitrate(tmp_path, [BRADY], {"13413": [PERMIT, NEWS]})
    accept_reviews(run_dir, run_dir / "run_review.xlsx")

    assert accepted(run_dir) == []
    loaded = {r["attribute_name"]: (r["status"], r["value_check"], r["sme_decision"], r["loaded"]) for r in found(run_dir)}
    assert loaded == {
        # A Tier 1 source's site type is trusted as read; a Tier 3 source's closing year is checked.
        "facility_type": (SME_REVIEW, "", "", ""),
        "operator": (SME_REVIEW, "", "", ""),
        "opening_year": (NOT_USED, "", "", ""),
        "closing_year": (SME_REVIEW, "required", "", ""),
    }


def test_correct_site_loads_the_pairs_values_and_a_key_value_once_marked_correct(tmp_path):
    run_dir = arbitrate(tmp_path, [BRADY], {"13413": [PERMIT, NEWS]})
    decide(run_dir, {PERMIT["url"]: {"decision": CORRECT_SITE, **SME}, NEWS["url"]: {"decision": CORRECT_SITE, **SME}})
    accept_reviews(run_dir, run_dir / "run_review.xlsx")

    [row, _operator] = accepted(run_dir)
    assert list(row) == ACCEPTED_VALUES_HEADERS
    assert (row["site_id"], row["site_name"], row["country_iso3"], row["attribute_name"], row["value"]) == (
        "13413", "Brady Road Resource Management Facility", "CAN", "facility_type", "Sanitary Landfill",
    )
    # Excel keeps the typed date as a date and time; it is read back as the date.
    assert (row["source_url"], row["source_tier"], row["approved_by"], row["reviewed_date"], row["year"]) == (
        "https://manitoba.example/permit", "Tier 1", "A. Reviewer", "2026-11-02", "2024",
    )
    # The closing year is a key value from a Tier 3 source: it waits on its own row.
    assert {r["attribute_name"] for r in accepted(run_dir)} == {"facility_type", "operator"}

    decide(run_dir, {(NEWS["url"], "closing_year"): {"decision": "correct"}})
    accept_reviews(run_dir, run_dir / "run_review.xlsx")
    loads = {r["attribute_name"]: r["value"] for r in accepted(run_dir)}
    assert loads == {"facility_type": "Sanitary Landfill", "operator": "RM of Springfield", "closing_year": "2045"}
    news = {r["attribute_name"]: (r["sme_decision"], r["value_decision"], r["loaded"]) for r in found(run_dir) if r["source_url"] == NEWS["url"]}
    # 1960 would change our opening_year, 1973: only fills of empty fields load.
    assert news == {
        "operator": (CORRECT_SITE, "", "yes"),
        "opening_year": (CORRECT_SITE, "", ""),
        "closing_year": (CORRECT_SITE, "Correct", "yes"),
    }


@pytest.mark.parametrize("cells, loads", [
    # Wrong, with the right value typed in: that loads instead.
    ({"decision": "Wrong", "corrected_value": 2041}, "2041"),
    # A value typed with no decision says the found one is wrong.
    ({"corrected_value": "2041"}, "2041"),
    # Wrong alone loads nothing.
    ({"decision": "Wrong"}, None),
])
def test_a_wrong_key_value_loads_the_smes_correction_if_they_give_one(tmp_path, cells, loads):
    run_dir = arbitrate(tmp_path, [BRADY], {"13413": [NEWS]})
    decide(run_dir, {NEWS["url"]: {"decision": CORRECT_SITE}, (NEWS["url"], "closing_year"): cells})
    accept_reviews(run_dir, run_dir / "run_review.xlsx")
    assert {r["attribute_name"]: r["value"] for r in accepted(run_dir)}.get("closing_year") == loads
    [closing] = [r for r in found(run_dir) if r["attribute_name"] == "closing_year"]
    assert (closing["value"], closing["value_decision"], closing["corrected_value"]) == ("2045", "Wrong", str(cells.get("corrected_value", "")))


# A page giving one key value only: its own row checks it (Hugh, 2 November).
CLOSURE = source("https://news.example/closure", "Springfield Landfill", values=[value("closing_year", "2045")])


@pytest.mark.parametrize("cells, loads, value_decision", [
    ({"decision": CORRECT_SITE}, "2045", "Correct"),
    # Its page is ours, and the value is wrong: the SME's loads instead.
    ({"decision": CORRECT_SITE, "corrected_value": "2041"}, "2041", "Wrong"),
    ({"decision": CORRECT_SITE, "corrected_value": "closing_year = 2041"}, "2041", "Wrong"),
    # Its name alone, as on a page row giving several values: wrong, with nothing instead.
    ({"decision": CORRECT_SITE, "corrected_value": "closing_year"}, None, "Wrong"),
    # A value typed under any other decision, or none, says the value is wrong, but nothing loads.
    ({"decision": JUNK, "corrected_value": "2041"}, None, "Wrong"),
    ({"corrected_value": "2041"}, None, "Wrong"),
    ({"decision": JUNK}, None, ""),
])
def test_a_pages_one_key_value_is_decided_by_the_pages_own_decision(tmp_path, cells, loads, value_decision):
    run_dir = arbitrate(tmp_path, [BRADY], {"13413": [CLOSURE]})
    decide(run_dir, {CLOSURE["url"]: cells})
    accept_reviews(run_dir, run_dir / "run_review.xlsx")
    assert {r["attribute_name"]: r["value"] for r in accepted(run_dir)}.get("closing_year") == loads
    [closing] = found(run_dir)
    assert closing["value_decision"] == value_decision


def test_a_value_says_whether_its_source_dated_it_to_its_year(tmp_path):
    """An undated value takes the run's year, which ingestion records as assumed (is_year_exact)."""
    permit = source("https://manitoba.example/permit", "Brady Road Landfill", source_type="Regulator",
                    values=[value("facility_type", "Sanitary Landfill", value_date="2019-05"),
                            value("operator", "RM of Springfield", value_date="")])
    run_dir = arbitrate(tmp_path, [BRADY], {"13413": [permit]})
    decide(run_dir, {permit["url"]: {"decision": CORRECT_SITE, **SME}})
    accept_reviews(run_dir, run_dir / "run_review.xlsx")

    loads = {r["attribute_name"]: (r["year"], r["is_year_exact"]) for r in accepted(run_dir)}
    assert loads["facility_type"] == ("2019", "TRUE")
    assert loads["operator"][1] == "FALSE"


def test_a_pages_one_key_value_loads_at_the_site_an_sme_says_it_is_about(tmp_path):
    other = seed("901", "Springfield landfill", 49.9, -96.9)
    run_dir = arbitrate(tmp_path, [BRADY, other], {"13413": [CLOSURE]})
    decide(run_dir, {CLOSURE["url"]: {"decision": DIFFERENT_SITE, "other_site_id": "901"}})
    accept_reviews(run_dir, run_dir / "run_review.xlsx")
    assert [(r["site_id"], r["attribute_name"], r["value"]) for r in accepted(run_dir)] == [("901", "closing_year", "2045")]


def test_a_corrected_value_on_a_pages_own_row_is_checked_as_on_a_value_row(tmp_path):
    run_dir = arbitrate(tmp_path, [BRADY], {"13413": [CLOSURE]})
    decide(run_dir, {CLOSURE["url"]: {"decision": CORRECT_SITE, "corrected_value": "soon"}})
    with pytest.raises(ReviewError) as stopped:
        accept_reviews(run_dir, run_dir / "run_review.xlsx")
    assert "(closing_year): corrected_value 'soon' can't be used" in str(stopped.value)


# A Tier 1 page giving several values, none checked on a row of its own: a wrong one is corrected on
# the page's row (Yuchen, 6 November).
GAS = source("https://manitoba.example/gas", "Brady Road Landfill", source_type="Regulator", values=[
    value("gccs_energy_project_type", "Electricity Generation", value_date="2015"),
    value("gccs_current_project_status", "Operational", value_date="2015"),
])


def gas_loads(tmp_path, corrected):
    run_dir = arbitrate(tmp_path, [BRADY], {"13413": [GAS]})
    decide(run_dir, {GAS["url"]: {"decision": CORRECT_SITE, "corrected_value": corrected}})
    accept_reviews(run_dir, run_dir / "run_review.xlsx")
    return run_dir, {r["attribute_name"]: (r["value"], r["year"], r["is_year_exact"]) for r in accepted(run_dir)}


@pytest.mark.parametrize("corrected, energy", [
    # As the attributes column writes it, or in short.
    ("gccs_energy_project_type = Renewable Natural Gas (2015)", ("Renewable Natural Gas", "2015", "TRUE")),
    ("gccs_energy_project_type = RNG", ("Renewable Natural Gas", "2015", "TRUE")),
    # Another year: the value loads at that one.
    ("gccs_energy_project_type = Renewable Natural Gas (2020)", ("Renewable Natural Gas", "2020", "TRUE")),
    # No value: the page's is wrong, and nothing loads instead.
    ("gccs_energy_project_type", None),
    ("gccs_energy_project_type (2015)", None),
])
def test_a_wrong_value_with_no_row_of_its_own_is_corrected_on_its_pages_row(tmp_path, corrected, energy):
    run_dir, loads = gas_loads(tmp_path, corrected)
    assert loads.get("gccs_energy_project_type") == energy
    assert loads["gccs_current_project_status"] == ("Operational", "2015", "TRUE")
    by_attribute = {r["attribute_name"]: (r["value_decision"], r["corrected_value"]) for r in found(run_dir)}
    assert by_attribute == {"gccs_energy_project_type": ("Wrong", corrected), "gccs_current_project_status": ("", "")}


def test_several_corrections_on_a_page_row_are_split_at_each_attributes_name(tmp_path):
    """A list value is written with semicolons too, so only a semicolon or line break before an
    attribute's name separates two corrections."""
    corrected = "gccs_energy_project_type = Electricity Generation; Direct Use (2015);\ngccs_current_project_status"
    _run_dir, loads = gas_loads(tmp_path, corrected)
    assert loads == {"gccs_energy_project_type": ("Electricity Generation; Direct Use", "2015", "TRUE")}


def test_a_value_rows_corrected_value_may_name_its_attribute_and_give_another_year(tmp_path):
    news = source("https://news.example/landfill", "Springfield Landfill", values=[
        value("closing_year", "2045"), value("annual_incoming_waste_metric_tonnes", "12000", value_date="2019"),
    ])
    run_dir = arbitrate(tmp_path, [BRADY], {"13413": [news]})
    decide(run_dir, {
        news["url"]: {"decision": CORRECT_SITE},
        (news["url"], "closing_year"): {"decision": "Wrong", "corrected_value": "closing_year = 2041"},
        (news["url"], "annual_incoming_waste_metric_tonnes (2019)"): {"decision": "Wrong", "corrected_value": "15000 (2021)"},
    })
    accept_reviews(run_dir, run_dir / "run_review.xlsx")
    loads = {r["attribute_name"]: (r["value"], r["year"]) for r in accepted(run_dir)}
    assert loads["closing_year"][0] == "2041"
    assert loads["annual_incoming_waste_metric_tonnes"] == ("15000", "2021")


def test_a_corrected_year_where_we_hold_a_value_loads_nothing_and_is_said(tmp_path):
    """Our latest year is 2015, and our energy type is 2010's, carried forward: the page's 2015 value
    fills an empty field, but a correction to 2010 would change ours."""
    site = {**BRADY, "year": "2015", "reference_year": "2015", "gccs_energy_project_type": "Direct Use",
            "backfilled_fields": "gccs_energy_project_type@2010"}
    run_dir = arbitrate(tmp_path, [site], {"13413": [GAS]})
    decide(run_dir, {GAS["url"]: {"decision": CORRECT_SITE, "corrected_value": "gccs_energy_project_type = RNG (2010)"}})
    result = accept_reviews(run_dir, run_dir / "run_review.xlsx")
    assert "gccs_energy_project_type" not in {r["attribute_name"] for r in accepted(run_dir)}
    assert any("already has gccs_energy_project_type for 2010, so it loads nothing." in w for w in result.value_warnings)


@pytest.mark.parametrize("page, corrected, check", [
    (GAS, "gccs_energy_project_type = RNG", "gccs_energy_project_type = RNG"),
    # A page giving one key value checks it on its own row.
    (CLOSURE, "2041", "closing_year"),
])
@pytest.mark.parametrize("decision, says", [
    (JUNK, "its source is Junk, so it loads nothing."),
    (None, "its source has no decision yet, so it loads nothing until it has one."),
])
def test_a_correction_on_a_rejected_or_undecided_page_is_said_and_loads_nothing(tmp_path, page, corrected, check, decision, says):
    run_dir = arbitrate(tmp_path, [BRADY], {"13413": [page]})
    decide(run_dir, {page["url"]: {"decision": decision, "corrected_value": corrected}})
    result = accept_reviews(run_dir, run_dir / "run_review.xlsx")
    assert accepted(run_dir) == []
    assert f"Site_Review row 2 ({check}): {says}" in result.value_warnings


@pytest.mark.parametrize("corrected, loads", [
    # Site 901's energy type is 2015's, carried forward to 2020, its latest year, which holds none.
    ("gccs_energy_project_type = RNG (2020)", ("Renewable Natural Gas", "2020")),
    ("gccs_energy_project_type = RNG", None),
])
def test_a_corrected_year_at_a_different_site_loads_where_that_years_field_is_empty(tmp_path, corrected, loads):
    other = seed("901", "Springfield landfill", 49.9, -96.9, year="2020", reference_year="2020",
                 gccs_energy_project_type="Direct Use", backfilled_fields="gccs_energy_project_type@2015")
    run_dir = arbitrate(tmp_path, [BRADY, other], {"13413": [GAS]})
    decide(run_dir, {GAS["url"]: {"decision": DIFFERENT_SITE, "other_site_id": "901", "corrected_value": corrected}})
    accept_reviews(run_dir, run_dir / "run_review.xlsx")
    loads_at = {r["attribute_name"]: (r["site_id"], r["value"], r["year"]) for r in accepted(run_dir)}
    assert loads_at["gccs_current_project_status"] == ("901", "Operational", "2015")
    assert loads_at.get("gccs_energy_project_type") == (loads and ("901", *loads))


@pytest.mark.parametrize("sources, corrected, says", [
    ([GAS], "Renewable Natural Gas", "corrected_value on a page row names the value it corrects"),
    ([GAS], "gas_use = RNG", "corrected_value on a page row names the value it corrects"),
    ([GAS], "gccs_energy_project_type = Coal", "(gccs_energy_project_type = Coal): corrected_value can't be used"),
    ([GAS], "operator = Someone", "(operator = Someone): the page gives no operator."),
    ([GAS], "gccs_energy_project_type (2019)", "the page gives gccs_energy_project_type for 2015, not 2019."),
    ([GAS], "gccs_energy_project_type; gccs_energy_project_type = RNG", "on the same row corrects the same value."),
    # A key value from a Tier 3 page is decided on its own row.
    ([NEWS], "closing_year = 2041", "closing_year has its own ↳ row under the page. Decide it there."),
])
def test_a_correction_on_a_page_row_that_cant_be_applied_stops_it_naming_the_row(tmp_path, sources, corrected, says):
    run_dir = arbitrate(tmp_path, [BRADY], {"13413": sources})
    decide(run_dir, {sources[0]["url"]: {"decision": CORRECT_SITE, "corrected_value": corrected}})
    with pytest.raises(ReviewError) as stopped:
        accept_reviews(run_dir, run_dir / "run_review.xlsx")
    assert "row 2 (" in str(stopped.value) and says in str(stopped.value)
    assert not (run_dir / ACCEPTED_VALUES_FILE).exists()


def test_a_correction_for_one_of_several_years_names_the_year(tmp_path):
    yearly = source("https://manitoba.example/tonnes", "Brady Road Landfill", source_type="Regulator", values=[
        value("annual_incoming_waste_metric_tonnes", "12000", value_date="2018"),
        value("annual_incoming_waste_metric_tonnes", "13000", value_date="2019"),
        value("operator", "City of Winnipeg"),
    ])
    run_dir = arbitrate(tmp_path, [BRADY], {"13413": [yearly]})
    decide(run_dir, {yearly["url"]: {"decision": CORRECT_SITE, "corrected_value": "annual_incoming_waste_metric_tonnes = 14000"}})
    with pytest.raises(ReviewError, match=r"for 2018, 2019\. Say which year is wrong"):
        accept_reviews(run_dir, run_dir / "run_review.xlsx")
    decide(run_dir, {yearly["url"]: {"corrected_value": "annual_incoming_waste_metric_tonnes = 14000 (2019)"}})
    accept_reviews(run_dir, run_dir / "run_review.xlsx")
    loads = {(r["attribute_name"], r["year"]): r["value"] for r in accepted(run_dir) if r["attribute_name"] != "operator"}
    assert loads == {("annual_incoming_waste_metric_tonnes", "2018"): "12000", ("annual_incoming_waste_metric_tonnes", "2019"): "14000"}
    # Removing needs no year: the name alone removes every year's.
    decide(run_dir, {yearly["url"]: {"corrected_value": "annual_incoming_waste_metric_tonnes"}})
    accept_reviews(run_dir, run_dir / "run_review.xlsx")
    assert [r["attribute_name"] for r in accepted(run_dir)] == ["operator"]


@pytest.mark.parametrize("attribute, typed, says", [
    ("closing_year", "soon", "corrected_value 'soon' can't be used: closing_year value 'soon' is not numeric"),
    ("closing_year", "20410", "corrected_value '20410' can't be used: closing_year '20410' is not a year"),
    ("facility_type", "Quarry", "corrected_value 'Quarry' can't be used"),
    # 75 or 75% reads as 0.75, as the search's own values do; 150% is more than all of it.
    ("gccs_collection_efficiency", "150%", "value 1.5 is outside the required 0-1 range."),
])
def test_a_corrected_value_that_isnt_a_valid_value_stops_it_naming_the_row(tmp_path, attribute, typed, says):
    news = source("https://news.example/landfill", "Springfield Landfill", values=[
        value("closing_year", "2045"), value("facility_type", "Sanitary Landfill"), value("has_landfill_gas_collection", "TRUE"),
    ])
    gas = source("https://news.example/gas", "Springfield Landfill", values=[
        value("gccs_collection_efficiency", "0.6"), value("has_landfill_gas_collection", "TRUE"),
    ])
    run_dir = arbitrate(tmp_path, [BRADY], {"13413": [news, gas]})
    decide(run_dir, {(url, attribute): {"decision": "Wrong", "corrected_value": typed} for url in (news["url"], gas["url"])})
    with pytest.raises(ReviewError) as stopped:
        accept_reviews(run_dir, run_dir / "run_review.xlsx")
    assert f"({attribute}): " in str(stopped.value) and says in str(stopped.value)
    assert not (run_dir / ACCEPTED_VALUES_FILE).exists()


def test_a_value_decision_under_a_rejected_or_undecided_source_is_said_and_loads_nothing(tmp_path, capsys):
    run_dir = arbitrate(tmp_path, [BRADY], {"13413": [NEWS]})
    decide(run_dir, {NEWS["url"]: {"decision": JUNK}, (NEWS["url"], "closing_year"): {"decision": "Correct", "corrected_value": "2041"}})
    capsys.readouterr()
    result = accept_reviews(run_dir, run_dir / "run_review.xlsx")
    out = capsys.readouterr().out
    assert accepted(run_dir) == []
    assert any("(closing_year): its source is Junk, so it loads nothing." in warning for warning in result.value_warnings)
    assert any("it is marked Correct, so its corrected_value '2041' is ignored." in warning for warning in result.value_warnings)
    assert "!! 2 thing(s) to check in value decisions:" in out


def test_a_tier_3_key_value_with_no_row_of_its_own_doesnt_load_at_a_different_site(tmp_path):
    """A value that confirms ours at the site searched gets no value row, so at the Different site,
    where it would fill a field, nothing has checked it."""
    site = {**BRADY, "facility_type": "Sanitary Landfill"}
    news = source("https://news.example/landfill", "Springfield Landfill",
                  values=[value("operator", "RM of Springfield"), value("facility_type", "Sanitary Landfill")])
    run_dir = arbitrate(tmp_path, [site, NEARBY], {"13413": [news]})
    decide(run_dir, {news["url"]: {"decision": DIFFERENT_SITE, "other_site_id": "900"}})
    accept_reviews(run_dir, run_dir / "run_review.xlsx")
    assert [(r["site_id"], r["attribute_name"]) for r in accepted(run_dir)] == [("900", "operator")]


def test_a_planned_closing_year_that_has_passed_isnt_weighed_again_at_a_different_site(tmp_path):
    """A plan to close by a year now past says nothing of whether either site closed, so it isn't used
    even where it matches the year we hold. Read as confirming ours, a Different site would weigh it
    again at the other site, where a Tier 1 source's closing year loads unchecked."""
    passed = str(datetime.now().year - 2)
    permit = source("https://manitoba.example/permit", "Brady Road Landfill", source_type="Regulator", values=[
        value("operator", "City of Winnipeg"), {**value("closing_year", passed), "planned": True},
    ])
    run_dir = arbitrate(tmp_path, [{**BRADY, "closing_year": passed}, seed("900", "domestic landfill", 50.10, -97.20)],
                        {"13413": [permit]})
    [closing] = [r for r in found(run_dir) if r["attribute_name"] == "closing_year"]
    assert closing["status"] == NOT_USED
    assert closing["status_reason"].startswith("A planned closure date that has passed")
    decide(run_dir, {permit["url"]: {"decision": DIFFERENT_SITE, "other_site_id": "900"}})
    accept_reviews(run_dir, run_dir / "run_review.xlsx")
    assert [(r["site_id"], r["attribute_name"]) for r in accepted(run_dir)] == [("900", "operator")]


@pytest.mark.parametrize("source_type, tier", [("Regulator", "Tier 1"), ("News report", "Tier 3")])
def test_an_inferred_operator_loads_only_once_its_own_row_is_correct_whatever_the_tier(tmp_path, source_type, tier):
    """The agent marked the operator Inferred, so it has a row of its own under a Tier 1 source as under
    a Tier 3 one: approving the source loads its other values, and the operator waits on its row. An
    SME's Wrong there stops it loading."""
    page = source("https://town.example/landfill", "Springfield Landfill", source_type=source_type, values=[
        {**value("operator", "RM of Springfield"), "value_basis": "Inferred"}, value("facility_status", "Active"),
    ])
    run_dir = arbitrate(tmp_path, [BRADY], {"13413": [page]})
    decide(run_dir, {page["url"]: {"decision": CORRECT_SITE}})
    accept_reviews(run_dir, run_dir / "run_review.xlsx")
    assert [(r["attribute_name"], r["source_tier"]) for r in accepted(run_dir)] == [("facility_status", tier)]

    decide(run_dir, {(page["url"], "operator"): {"decision": "Wrong"}})
    accept_reviews(run_dir, run_dir / "run_review.xlsx")
    assert [r["attribute_name"] for r in accepted(run_dir)] == ["facility_status"]
    [operator] = [r for r in found(run_dir) if r["attribute_name"] == "operator"]
    assert (operator["value_check"], operator["value_decision"], operator["loaded"]) == ("required", "Wrong", "")

    decide(run_dir, {(page["url"], "operator"): {"decision": "Correct"}})
    accept_reviews(run_dir, run_dir / "run_review.xlsx")
    assert {r["attribute_name"]: r["value"] for r in accepted(run_dir)} == {"facility_status": "Active", "operator": "RM of Springfield"}


@pytest.mark.parametrize("typed, says", [
    # Any operator name is a value, so these would load as one.
    ("gas_use = Springfield Utilities", "it names gas_use, but this row checks operator"),
    ("Springfield Utilities (1700)", "1700 is not a year"),
])
def test_an_operators_corrected_value_naming_another_attribute_or_a_bad_year_stops_it(tmp_path, typed, says):
    page = source("https://town.example/landfill", "Springfield Landfill", values=[
        {**value("operator", "RM of Springfield"), "value_basis": "Inferred"}, value("facility_status", "Active"),
    ])
    run_dir = arbitrate(tmp_path, [BRADY], {"13413": [page]})
    decide(run_dir, {page["url"]: {"decision": CORRECT_SITE}, (page["url"], "operator"): {"corrected_value": typed}})
    with pytest.raises(ReviewError) as stopped:
        accept_reviews(run_dir, run_dir / "run_review.xlsx")
    assert f"(operator): corrected_value {typed!r} can't be used: {says}." in str(stopped.value)


def test_an_inferred_operator_weighed_again_at_a_different_site_has_no_row_so_doesnt_load():
    """Set aside at the site searched because it would change our operator, it is weighed again at
    the Different site, where its field is empty; but nothing has checked it there. A Direct one loads."""
    def operator(review, basis):
        return found_row(review, "operator", "RM of Springfield", tier="Tier 1", status=NOT_USED, value_basis=basis,
                         status_reason="Would change our value, Town of Hartney: only fills of empty fields load.")

    elsewhere = lambda *reviews: {review: Decision(review, "7", DIFFERENT_SITE, "8", "SME", "", "", 2) for review in reviews}
    result = accept([operator("inferred", "Inferred"), operator("direct", "Direct")], elsewhere("inferred", "direct"), SITES)
    assert [(row["review_id"], row["site_id"]) for row in result.values] == [("direct", "8")]


def test_value_decisions_are_read_by_review_id_and_check(tmp_path):
    run_dir = arbitrate(tmp_path, [BRADY], {"13413": [NEWS]})
    decide(run_dir, {(NEWS["url"], "closing_year"): {"decision": " WRONG ", "corrected_value": "2041"}})
    [decision] = read_value_decisions(run_dir / "run_review.xlsx").values()
    assert (decision.check, decision.attribute, decision.decision, decision.typed, decision.corrected) == (
        "closing_year", "closing_year", "Wrong", "2041", "2041",
    )
    assert decision.review_id.startswith("WAIREV-13413-")


def test_a_different_site_worth_a_second_look_is_applied_and_said(tmp_path, capsys):
    """The seed holds our whole database, so a mistyped other_site_id is still a site of ours, in any
    country. A site in another country, one the run didn't search, or one far from where the source
    places the facility, is said, naming the row; the decision is still applied, as each can be
    right."""
    oregon = seed("368", "Northwest Regional MSW Landfill", 45.5, -122.6, iso3="USA")
    near = source("https://news.example/landfill", "Springfield Landfill", "49.7590, -97.2010", values=[value("operator", "RM of Springfield")])
    run_dir = arbitrate(tmp_path, [BRADY, NEARBY, oregon], {"13413": [near]})
    # The run searched 900 too, and found nothing there.
    write_csv_records(run_dir / "foundry_run_log.csv", [{"site_id": "13413"}, {"site_id": "900"}], ["site_id"])
    decide(run_dir, {near["url"]: {"decision": DIFFERENT_SITE, "other_site_id": "368"}})
    capsys.readouterr()
    accept_reviews(run_dir, run_dir / "run_review.xlsx")
    out = capsys.readouterr().out
    assert "!! 3 thing(s) to check in Different site decisions, which were applied." in out
    where = "Site_Review row 2 (WAIREV-13413-"
    assert f"{where}" in out
    assert "other_site_id 368 (Northwest Regional MSW Landfill) is in USA, but site 13413, whose search cited the source, is in CAN." in out
    assert "other_site_id 368 (Northwest Regional MSW Landfill) wasn't searched in this run" in out
    assert "other_site_id 368 (Northwest Regional MSW Landfill) is 1,951 km from where the source places the facility." in out
    assert [(r["site_id"], r["value"]) for r in accepted(run_dir)] == [("368", "RM of Springfield")]

    decide(run_dir, {near["url"]: {"decision": DIFFERENT_SITE, "other_site_id": "900"}})
    capsys.readouterr()
    accept_reviews(run_dir, run_dir / "run_review.xlsx")
    out = capsys.readouterr().out
    assert "1 thing(s) to check" in out and "other_site_id 900 (domestic landfill) is 38 km from where the source places" in out


def test_different_site_loads_the_values_at_that_site_only_where_its_fields_are_empty(tmp_path):
    """The source is about site 900. Its operator and opening year fill empty fields there - the
    opening year although it would change ours at the site searched, which says nothing of 900 - and
    its closing year doesn't load, as 900 holds one."""
    run_dir = arbitrate(tmp_path, [BRADY, NEARBY], {"13413": [NEWS]})
    decide(run_dir, {NEWS["url"]: {"decision": DIFFERENT_SITE, "other_site_id": 900, **SME}})
    accept_reviews(run_dir, run_dir / "run_review.xlsx")

    loads = {(r["site_id"], r["site_name"], r["attribute_name"]): r["value"] for r in accepted(run_dir)}
    assert loads == {
        ("900", "domestic landfill", "operator"): "RM of Springfield",
        ("900", "domestic landfill", "opening_year"): "1960",
    }
    assert {r["review_id"] for r in accepted(run_dir)} == {r["review_id"] for r in found(run_dir)}
    news = {r["attribute_name"]: (r["site_id"], r["sme_decision"], r["loaded"]) for r in found(run_dir)}
    assert news == {
        "operator": ("13413", "Different site (900)", "yes"),
        "opening_year": ("13413", "Different site (900)", "yes"),
        "closing_year": ("13413", "Different site (900)", ""),
    }


def test_new_site_loads_nothing_and_lists_the_source_once(tmp_path):
    hartney = source("https://council.example/tip", "Hartney Waste Disposal Grounds", "49.5612, -100.5501",
                     address="Road 22 N, Hartney", values=[value("operator", "Municipality of Grassland")])
    run_dir = arbitrate(tmp_path, [BRADY, NEARBY], {"13413": [hartney]})
    decide(run_dir, {hartney["url"]: {"decision": NEW_SITE, "notes": "A council tip west of Brandon.", **SME}})
    accept_reviews(run_dir, run_dir / "run_review.xlsx")

    assert accepted(run_dir) == []
    [row] = found(run_dir)
    assert (row["sme_decision"], row["loaded"]) == (NEW_SITE, "")
    [listed] = rows(run_dir / "new_facility_candidates.csv")
    assert (listed["found_by"], listed["country_iso3"], listed["source_urls"], listed["facility_names_in_source"]) == (
        "SME", "CAN", "https://council.example/tip", "Hartney Waste Disposal Grounds",
    )
    assert (listed["stated_locations"], listed["latitude"], listed["longitude"], listed["location_basis"]) == (
        "49.5612, -100.5501 | Road 22 N, Hartney", "49.5612", "-100.5501", "stated",
    )
    assert (listed["searched_site_ids"], listed["nearest_site_id"]) == ("13413", "13413")
    assert listed["note"] == (
        "Marked New site by an SME (A. Reviewer on 2026-11-02) under site 13413 (Brady Road Resource "
        "Management Facility), whose search cited it. It gives operator = Municipality of Grassland. "
        "A council tip west of Brandon."
    )

    # Accepting again lists it no second time.
    accept_reviews(run_dir, run_dir / "run_review.xlsx")
    assert len(rows(run_dir / "new_facility_candidates.csv")) == 1


def test_junk_loads_nothing(tmp_path):
    run_dir = arbitrate(tmp_path, [BRADY], {"13413": [NEWS]})
    decide(run_dir, {NEWS["url"]: {"decision": JUNK, **SME}})
    accept_reviews(run_dir, run_dir / "run_review.xlsx")
    assert accepted(run_dir) == []
    assert {(r["sme_decision"], r["loaded"]) for r in found(run_dir)} == {(JUNK, "")}
    assert rows(run_dir / "new_facility_candidates.csv") == []


def test_decisions_are_read_in_any_case_and_with_dashes(tmp_path):
    """As typed or pasted: any case, extra spaces, and a hyphen or an en dash for a space. A site id
    Excel kept as a number reads as the id."""
    run_dir = arbitrate(tmp_path, [BRADY, NEARBY], {"13413": [NEWS, PERMIT]})
    decide(run_dir, {NEWS["url"]: {"decision": " different–SITE ", "other_site_id": 900.0}})
    [decision] = read_decisions(run_dir / "run_review.xlsx", {"13413", "900"}).values()
    assert (decision.decision, decision.other_site_id, decision.approved_by) == (DIFFERENT_SITE, "900", "SME")


# --- picking one value ---------------------------------------------------------------------------
def found_row(review, attribute, found_value, tier="Tier 3", status=SME_REVIEW, year="2024", value_date="2024", site="7", **extra):
    return {
        "run_id": "run", "site_id": site, "year": year, "source_id": f"src-{review}", "source_url": f"https://{review}.example",
        "source_tier": tier, "attribute_name": attribute, "value": found_value, "unit": "", "value_date": value_date,
        "review_id": review, "status": status, "status_reason": "", "sme_decision": "", "loaded": "", **extra,
    }


def correct(*reviews):
    return {review: Decision(review, "7", CORRECT_SITE, "", "SME", "", "", 2) for review in reviews}


SITES = {"7": seed("7", "Hartney Landfill", 49.5, -100.5), "8": seed("8", "Other Landfill", 50.0, -99.0)}


def picked(rows_in, decisions):
    result = accept(rows_in, decisions, SITES)
    return [(row["review_id"], row["value"]) for row in result.values], [row["loaded"] for row in rows_in]


def test_where_accepted_values_disagree_the_best_tier_wins():
    rows_in = [
        found_row("news", "operator", "Town of Hartney", value_date="2024"),
        found_row("permit", "operator", "Municipality of Grassland", tier="Tier 1", status=AUTO_APPROVED, value_date="2019"),
    ]
    assert picked(rows_in, correct("news")) == ([("permit", "Municipality of Grassland")], ["no", "yes"])


def test_within_a_tier_the_newest_wins_then_the_value_most_sources_give():
    newest = [found_row("old", "operator", "A", value_date="2019"), found_row("new", "operator", "B", value_date="2023")]
    assert picked(newest, correct("old", "new")) == ([("new", "B")], ["no", "yes"])
    most = [
        found_row("one", "operator", "A"), found_row("two", "operator", "B"), found_row("three", "operator", "B"),
    ]
    assert picked(most, correct("one", "two", "three"))[1].count("yes") == 1
    assert picked(most, correct("one", "two", "three"))[0][0][1] == "B"


def test_a_yearly_attribute_loads_one_value_per_year():
    rows_in = [
        found_row("a", "waste_in_place_metric_tonnes", "1000", year="2022", value_date="2022"),
        found_row("a", "waste_in_place_metric_tonnes", "1200", year="2023", value_date="2023"),
        found_row("b", "waste_in_place_metric_tonnes", "1100", year="2023", value_date="2023-06"),
    ]
    assert picked(rows_in, correct("a", "b")) == ([("b", "1100"), ("a", "1000")], ["yes", "no", "yes"])


def test_re_filed_values_compete_with_the_other_sites_own():
    """A value an SME re-files to site 8 is weighed against site 8's own accepted values."""
    rows_in = [
        found_row("theirs", "operator", "Town of Hartney"),
        found_row("own", "operator", "Grassland", tier="Tier 2", status=AUTO_APPROVED, site="8"),
    ]
    decisions = {"theirs": Decision("theirs", "7", DIFFERENT_SITE, "8", "SME", "", "", 2)}
    result = accept(rows_in, decisions, SITES)
    assert [(row["site_id"], row["value"]) for row in result.values] == [("8", "Grassland")]
    assert [row["loaded"] for row in rows_in] == ["no", "yes"]


def test_an_smes_decision_stands_on_a_pair_since_auto_approved():
    """A pair can be auto-approved on a later arbitration, but an SME who decided it was Junk on the
    earlier one has the last word."""
    rows_in = [found_row("permit", "operator", "A", tier="Tier 1", status=AUTO_APPROVED)]
    result = accept(rows_in, {"permit": Decision("permit", "7", JUNK, "", "SME", "", "", 2)}, SITES)
    assert (result.values, rows_in[0]["loaded"], rows_in[0]["sme_decision"]) == ([], "", JUNK)


def test_one_source_marked_new_site_under_two_sites_is_one_candidate():
    rows_in = [
        found_row("a", "operator", "A", site="7", facility_name_in_source="Hartney Tip", country_iso3="CAN"),
        found_row("b", "operator", "A", site="8", facility_name_in_source="Hartney tip.", country_iso3="CAN"),
    ]
    for row in rows_in:
        row["source_url"] = "https://council.example/tip"
    decisions = {
        "a": Decision("a", "7", NEW_SITE, "", "SME", "", "", 2),
        "b": Decision("b", "8", NEW_SITE, "", "SME", "", "", 3),
    }
    [candidate] = accept(rows_in, decisions, SITES).new_sites
    assert candidate["searched_site_ids"] == "7; 8"


def test_two_spellings_of_one_operator_marked_correct_are_one_value_not_two():
    """Different values for one field all marked Correct are said, as one of them loads. Two spellings
    of one operator's name are one value."""
    def checked(review, operator, row):
        return {"site_id": "7", "attribute_name": "operator", "year": "2024", "value": operator,
                "_value_decision": ValueDecision(review, "operator", "operator", "Correct", "", "", row)}

    spellings = [checked("a", "H. Ayuntamiento de Tixkokob 2024-2027", 3), checked("b", "Ayuntamiento de Tixkokob", 5)]
    assert value_warnings({}, set(), {}, spellings) == []
    [warning] = value_warnings({}, set(), {}, [*spellings, checked("c", "Red Ambiental", 7)])
    assert warning.startswith("Site_Review: different values for operator at site 7 are all approved")


# --- running it again ----------------------------------------------------------------------------
def test_accepting_again_writes_the_same_files(tmp_path):
    hartney = source("https://council.example/tip", "Hartney Waste Disposal Grounds", "49.5612, -100.5501",
                     values=[value("operator", "Municipality of Grassland")])
    run_dir = arbitrate(tmp_path, [BRADY, NEARBY], {"13413": [PERMIT, NEWS, hartney]})
    workbook = decide(run_dir, {NEWS["url"]: {"decision": DIFFERENT_SITE, "other_site_id": "900"}, hartney["url"]: {"decision": NEW_SITE}})
    names = (ACCEPTED_VALUES_FILE, "found_values.csv", "new_facility_candidates.csv")

    accept_reviews(run_dir, workbook)
    first = {name: (run_dir / name).read_bytes() for name in names}
    accept_reviews(run_dir, workbook)
    assert {name: (run_dir / name).read_bytes() for name in names} == first


def test_a_decision_changed_since_is_applied_as_it_now_stands(tmp_path):
    run_dir = arbitrate(tmp_path, [BRADY], {"13413": [NEWS]})
    workbook = decide(run_dir, {NEWS["url"]: {"decision": CORRECT_SITE}, (NEWS["url"], "closing_year"): {"decision": "Correct"}})
    accept_reviews(run_dir, workbook)
    assert len(accepted(run_dir)) == 2
    decide(run_dir, {NEWS["url"]: {"decision": JUNK}})
    accept_reviews(run_dir, workbook)
    assert accepted(run_dir) == []
    assert {r["loaded"] for r in found(run_dir)} == {""}


def test_arbitrating_again_empties_accepted_values_and_keeps_the_smes_new_sites(tmp_path):
    """Arbitrating again sorts every value afresh with sme_decision and loaded blank, so an
    accepted_values.csv beside it would be out of date; the review_ids are the same, so accepting
    again from the reviewed workbook restores it."""
    hartney = source("https://council.example/tip", "Hartney Waste Disposal Grounds", values=[value("operator", "Grassland")])
    run_dir = arbitrate(tmp_path, [BRADY], {"13413": [PERMIT, NEWS, hartney]})
    reviewed = tmp_path / "reviewed.xlsx"
    decide(run_dir, {NEWS["url"]: {"decision": CORRECT_SITE}, hartney["url"]: {"decision": NEW_SITE}}).rename(reviewed)
    accept_reviews(run_dir, reviewed)
    before = (run_dir / ACCEPTED_VALUES_FILE).read_bytes()

    run_arbitration(PipelineConfig(input_csv=None, run_dir=run_dir, run_id="run", dataset_version="v"))
    assert accepted(run_dir) == []
    with (run_dir / ACCEPTED_VALUES_FILE).open(encoding="utf-8-sig") as handle:
        assert next(csv.reader(handle)) == ACCEPTED_VALUES_HEADERS
    assert [row["found_by"] for row in rows(run_dir / "new_facility_candidates.csv")] == ["SME"]
    accept_reviews(run_dir, reviewed)
    assert (run_dir / ACCEPTED_VALUES_FILE).read_bytes() == before


class Blob:
    """Just enough of a blob container to push a run folder up and pull it to another machine."""

    def __init__(self):
        self.blobs = {}

    def upload_blob(self, name, data, overwrite=False):
        self.blobs[name] = data.read()

    def list_blobs(self, name_starts_with=""):
        return [SimpleNamespace(name=name) for name in self.blobs if name.startswith(name_starts_with)]

    def download_blob(self, name):
        payload = self.blobs[name]
        return SimpleNamespace(readinto=lambda handle: handle.write(payload))


def test_arbitrating_again_replaces_the_accepted_values_in_blob_too(tmp_path):
    """Push never deletes a blob, so a deleted accepted_values.csv would come back on the next pull,
    to this machine or another, beside found values that no longer load it. The emptied file is
    pushed over it instead."""
    from waste_ai_search import storage

    blob = Blob()
    run_dir = arbitrate(tmp_path, [BRADY], {"13413": [NEWS]})
    accept_reviews(run_dir, decide(run_dir, {NEWS["url"]: {"decision": CORRECT_SITE}}))
    storage.push(run_dir, "outputs/runs/run", client=blob)
    run_arbitration(PipelineConfig(input_csv=None, run_dir=run_dir, run_id="run", dataset_version="v"))
    storage.push(run_dir, "outputs/runs/run", client=blob)

    other_machine = tmp_path / "other"
    storage.pull("outputs/runs/run", other_machine, client=blob)
    assert accepted(other_machine) == []
    assert {row["loaded"] for row in found(other_machine)} == {""}


def test_a_decision_on_a_pair_the_run_no_longer_has_is_reported_not_applied(tmp_path, capsys):
    run_dir = arbitrate(tmp_path, [BRADY], {"13413": [NEWS]})
    workbook = decide(run_dir, {NEWS["url"]: {"decision": CORRECT_SITE, "review_id": "WAIREV-13413-000000000000"}})
    result = accept_reviews(run_dir, workbook)
    assert [decision.row for decision in result.unmatched] == [2]
    assert accepted(run_dir) == []
    assert "1 decision(s) are on pairs found_values.csv no longer has (Site_Review rows 2)" in capsys.readouterr().out


def test_a_value_whose_row_the_reviewed_workbook_doesnt_have_is_said_and_loads_nothing(tmp_path, capsys):
    """A workbook made before the run was arbitrated again can lack a ↳ row the run now checks a value
    on: until 1 November, an operator the agent inferred had none under a Tier 1 page. The value waits
    as any undecided one does, and accept-reviews says no SME can decide it on that workbook."""
    page = source("https://manitoba.example/permit", "Brady Road Landfill", source_type="Regulator", values=[
        {**value("operator", "City of Winnipeg"), "value_basis": "Inferred"}, value("facility_status", "Active"),
    ])
    run_dir = arbitrate(tmp_path, [BRADY], {"13413": [page]})
    workbook = decide(run_dir, {page["url"]: {"decision": CORRECT_SITE}})
    capsys.readouterr()
    result = accept_reviews(run_dir, workbook)
    assert (result.undecided_values, result.missing_value_rows) == (1, 0)
    assert "!!" not in capsys.readouterr().out

    book = load_workbook(workbook)
    ws = book["Site_Review"]
    check = [cell.value for cell in ws[1]].index("check") + 1
    ws.delete_rows(next(row for row in range(2, ws.max_row + 1) if ws.cell(row, check).value == "operator"))
    book.save(workbook)
    result = accept_reviews(run_dir, workbook)
    assert (result.undecided_values, result.missing_value_rows) == (1, 1)
    assert [r["attribute_name"] for r in accepted(run_dir)] == ["facility_status"]
    assert "!! 1 value(s) waiting under approved sources have no ↳ row in run_review.xlsx: the run was" in capsys.readouterr().out


# --- what stops it -------------------------------------------------------------------------------
@pytest.mark.parametrize("cells, says", [
    ({"decision": "Approve"}, "row 3 (WAIREV-13413-"),
    ({"decision": "Approve"}, "'Approve' is not a decision. Pick one of Correct site, Different site, New site, Junk."),
    ({"decision": DIFFERENT_SITE}, "Different site needs the id of the site it is about in other_site_id."),
    ({"decision": DIFFERENT_SITE, "other_site_id": "12345"}, "other_site_id 12345 is not a site in the run's seed.csv."),
    ({"decision": DIFFERENT_SITE, "other_site_id": "13413"}, "other_site_id 13413 is this row's own site. Pick Correct site instead."),
    ({"decision": CORRECT_SITE, "other_site_id": "900"}, "only Different site takes one, and the decision is Correct site."),
    ({"other_site_id": "900"}, "only Different site takes one, and the decision is blank."),
    ({"decision": CORRECT_SITE, "review_id": None}, "row 3: it is filled in but has no review_id"),
])
def test_a_decision_that_cant_be_read_back_stops_it_naming_the_row(tmp_path, cells, says):
    run_dir = arbitrate(tmp_path, [BRADY, NEARBY], {"13413": [PERMIT, NEWS]})
    workbook = decide(run_dir, {NEWS["url"]: cells})
    with pytest.raises(ReviewError) as stopped:
        accept_reviews(run_dir, workbook)
    assert says in str(stopped.value)
    # Nothing is written.
    assert not (run_dir / ACCEPTED_VALUES_FILE).exists()
    assert {r["sme_decision"] for r in found(run_dir)} == {""}


def test_two_rows_deciding_one_pair_two_ways_stop_it(tmp_path):
    run_dir = arbitrate(tmp_path, [BRADY], {"13413": [NEWS]})
    path = decide(run_dir, {NEWS["url"]: {"decision": CORRECT_SITE}})
    book = load_workbook(path)
    ws = book["Site_Review"]
    ws.append([cell.value for cell in ws[2]])
    ws.cell(ws.max_row, [cell.value for cell in ws[1]].index("decision") + 1, JUNK)
    book.save(path)
    with pytest.raises(ReviewError, match=f"row {ws.max_row} .*: row 2 decides the same pair Correct site, and this row Junk"):
        accept_reviews(run_dir, path)


def test_a_workbook_with_no_site_review_stops_it(tmp_path):
    """A workbook arbitrated before Site_Review, with a Review_Queue instead."""
    run_dir = arbitrate(tmp_path, [BRADY], {"13413": [NEWS]})
    old = tmp_path / "old.xlsx"
    book = load_workbook(write_review_workbook(old, run_config=[], site_review=[], parse_warnings=[]))
    book["Site_Review"].title = "Review_Queue"
    book.save(old)
    with pytest.raises(ReviewError, match="has no Site_Review tab"):
        accept_reviews(run_dir, old)


# --- the command ---------------------------------------------------------------------------------
def test_the_command_accepts_the_reviewed_workbook(tmp_path, capsys):
    run_dir = arbitrate(tmp_path, [BRADY], {"13413": [PERMIT, NEWS]})
    workbook = decide(run_dir, {NEWS["url"]: {"decision": "correct site", **SME}})
    capsys.readouterr()
    args = ["accept-reviews", "--run-id", "run", "--run-dir", str(run_dir), "--workbook", str(workbook), "--local"]
    assert main(args) == 0
    out = capsys.readouterr().out
    assert "Decisions read from" in out and "1 (Correct site 1)" in out
    assert "Key values decided: 0 (Correct 0, Wrong 0, 0 of them corrected); waiting under approved sources: 1" in out
    assert "Values to load: 1 for 1 site(s), in accepted_values.csv" in out
    assert len(accepted(run_dir)) == 1


def test_the_command_removes_the_retired_outputs_a_pull_brings_back(tmp_path):
    """Blob keeps what an earlier arbitration wrote, and the pull before accepting brings it back;
    beside accepted_values.csv, an old resolved.csv or review queue would look current."""
    run_dir = arbitrate(tmp_path, [BRADY], {"13413": [NEWS]})
    workbook = decide(run_dir, {NEWS["url"]: {"decision": CORRECT_SITE}})
    retired = ["evidence.csv", "resolved.csv", "review_queue.csv", "supplementary_leads.csv", "outside_run_matches.csv"]
    for name in retired:
        (run_dir / name).write_text("pulled back from blob")
    assert main(["accept-reviews", "--run-id", "run", "--run-dir", str(run_dir), "--workbook", str(workbook), "--local"]) == 0
    assert not [name for name in retired if (run_dir / name).exists()]
    assert len(accepted(run_dir)) == 1


def test_the_command_says_why_it_stopped(tmp_path, capsys):
    run_dir = arbitrate(tmp_path, [BRADY], {"13413": [NEWS]})
    workbook = decide(run_dir, {NEWS["url"]: {"decision": "Maybe"}})
    args = ["accept-reviews", "--run-id", "run", "--run-dir", str(run_dir), "--workbook", str(workbook), "--local"]
    assert main(args) == 1
    assert "'Maybe' is not a decision" in capsys.readouterr().err
    assert main(["accept-reviews", "--run-id", "run", "--run-dir", str(tmp_path / "none"), "--local"]) == 1


def test_the_smes_copy_is_found_in_the_review_folder_first(tmp_path):
    run_dir, shared = tmp_path / "run", tmp_path / "SharePoint"
    (shared / "run").mkdir(parents=True)
    assert reviewed_workbook(run_dir, "run", shared) == run_dir / "run_review.xlsx"
    (shared / "run" / "run_review.xlsx").write_bytes(b"")
    assert reviewed_workbook(run_dir, "run", shared) == shared / "run" / "run_review.xlsx"
    assert reviewed_workbook(run_dir, "run", None) == run_dir / "run_review.xlsx"


def test_collect_new_facilities_is_gone():
    with pytest.raises(SystemExit):
        main(["collect-new-facilities", "--run-id", "run", "--local"])
