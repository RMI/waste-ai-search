"""Whether a source's quote, and the location it states, are on its page (on_page.py).

The link check keeps each page's text (page_text.py); arbitration checks against it offline, and the
review queue flags a winning source whose page doesn't bear it out. Nothing is dropped for it.
"""
from __future__ import annotations

import csv
import json

from openpyxl import load_workbook

from waste_ai_search.arbitrate import run_arbitration
from waste_ai_search.input_loader import write_csv_records
from waste_ai_search.on_page import (
    FOUND,
    NO_PAGE_TEXT,
    NOT_FOUND,
    NOT_IN_PART_READ,
    PARTLY,
    Page,
    PageChecker,
    describe_results,
    location_on_page,
    phrase_on_page,
    plain,
    quote_on_page,
)
from waste_ai_search.page_text import PAGE_TEXT_DIR, save_page_text
from waste_ai_search.run_context import PipelineConfig
from waste_ai_search.schema import LOCATION_ATTRIBUTE
from waste_ai_search.seed_source import seed_headers


PAGE = Page(
    "Kapiti Coast District Council\n"
    "The Otaihanga landfill closed to household waste in 2007. It is operated by the council,\n"
    "and lies off Otaihanga Road, Paraparaumu. Location: -40.8836512, 175.0277 (NZTM 1771234 5471234).\n"
    "Grid reference TQ310806. Déchetterie de l'Île."
)


# --- comparing ---------------------------------------------------------------------------------
def test_text_is_compared_in_plain_form():
    assert plain("Déchetterie de l'Île – CLOSED, 2007!") == "dechetterie de l ile closed 2007"


def test_a_quote_is_found_as_written_whatever_its_punctuation_or_line_breaks():
    assert phrase_on_page("closed to household waste in 2007", PAGE) == FOUND
    assert phrase_on_page("It is operated by the council, and lies off Otaihanga Road", PAGE) == FOUND
    assert phrase_on_page("Dechetterie de l’ile", PAGE) == FOUND


def test_each_part_of_a_quote_between_ellipses_must_be_on_the_page():
    assert phrase_on_page("The Otaihanga landfill closed ... off Otaihanga Road", PAGE) == FOUND
    assert phrase_on_page("The Otaihanga landfill closed … in 2011", PAGE) != FOUND


def test_a_quote_mostly_in_other_words_is_partly_found_and_one_that_isnt_there_is_not():
    # Every word is on the page, but not in this order.
    assert phrase_on_page("the council operated the Otaihanga landfill until 2007", PAGE) == PARTLY
    assert phrase_on_page("accepted 45,000 tonnes of municipal waste a year", PAGE) == NOT_FOUND


def test_nothing_to_check_or_no_page_text():
    assert phrase_on_page("", PAGE) == ""
    assert phrase_on_page("closed in 2007", None) == NO_PAGE_TEXT


def test_stated_coordinates_count_where_the_page_gives_them_to_more_decimals():
    assert location_on_page("-40.88365, 175.0277", "coordinates", PAGE) == FOUND
    # Trailing zeros say nothing.
    assert location_on_page("-40.88365, 175.02770", "coordinates", PAGE) == FOUND
    # A sign or hemisphere is not compared, but the digits are.
    assert location_on_page("40.8836512 S, 175.0277 E", "coordinates", PAGE) == FOUND
    assert location_on_page("-40.88365, 176.1234", "coordinates", PAGE) == PARTLY
    assert location_on_page("-41.2000, 174.7700", "coordinates", PAGE) == NOT_FOUND
    # 140.8836 is not 40.8836.
    assert location_on_page("140.8836, 75.0277", "coordinates", PAGE) == NOT_FOUND


def test_a_grid_reference_is_compared_without_its_spaces():
    assert location_on_page("TQ 310 806", "grid reference", PAGE) == FOUND
    assert location_on_page("TQ 311 806", "grid reference", PAGE) == NOT_FOUND


def test_of_a_stated_pair_the_better_counts_and_an_address_is_a_phrase():
    assert location_on_page("-40.88365, 175.0277 | 1 Otaihanga Road", "coordinates | street address", PAGE) == FOUND
    assert location_on_page("Otaihanga Road, Paraparaumu", "street address", PAGE) == FOUND
    assert location_on_page("", "", PAGE) == ""
    assert location_on_page("Otaihanga Road", "street address", None) == NO_PAGE_TEXT


def test_numbers_and_units_count_however_they_are_spaced():
    page = Page("It took 45000 tonnes a year over 10ha, 95% of it household waste, 1234.5 t in 2019.")
    assert phrase_on_page("took 45,000 tonnes a year", page) == FOUND
    assert phrase_on_page("over 10 ha", page) == FOUND
    assert phrase_on_page("95 per cent of it household waste", page) == FOUND
    assert phrase_on_page("1,234.5 t in 2019", page) == FOUND
    spaced = Page("It took 45 000 tonnes a year.")
    assert phrase_on_page("took 45,000 tonnes a year", spaced) == FOUND


def test_invisible_characters_and_a_pdfs_line_break_hyphen_dont_matter():
    assert phrase_on_page("the landfill closed", Page("The land\u00adfill closed in 2007.")) == FOUND
    assert phrase_on_page("the landfill closed", Page("The land\u2060fill closed in 2007.")) == FOUND
    assert phrase_on_page("the Otaihanga landfill closed", Page("The Otaihanga land-\nfill closed in 2007.")) == FOUND


def test_scripts_written_without_spaces_are_found_anywhere_in_the_text():
    page = Page("该垃圾填埋场位于北京市朝阳区。最終処分場は閉鎖された。หลุมฝังกลบแห่งนี้ปิดแล้ว")
    assert phrase_on_page("垃圾填埋场位于北京", page) == FOUND
    assert phrase_on_page("最終処分場", page) == FOUND
    assert phrase_on_page("หลุมฝังกลบ", page) == FOUND
    assert phrase_on_page("垃圾焚烧厂位于上海", page) == NOT_FOUND


def test_a_quote_with_a_number_the_page_doesnt_have_is_not_found():
    """A wrong figure is the error that matters most, so it is never only partly found."""
    page = Page("The approved capacity of 1,500,000 tonnes. The landfill closed in 2007.")
    assert phrase_on_page("approved capacity of 2,500,000 tonnes", page) == NOT_FOUND
    assert phrase_on_page("The landfill finally closed in 2011", page) == NOT_FOUND
    assert phrase_on_page("the landfill finally closed in 2007", page) == PARTLY


def test_a_quote_too_short_to_mean_anything_is_not_checked():
    page = Page("Our offices are closed on Sundays.")
    assert quote_on_page("closed", page) == ""
    assert quote_on_page("Status Operational", page) == ""
    assert quote_on_page("offices are closed on Sundays", page) == FOUND


def test_an_address_written_slightly_differently_still_counts():
    page = Page("Find us at 12 Foo Rd, Barton OX1 2AB.")
    assert location_on_page("12 Foo Road, Barton", "street address", page) == FOUND
    assert location_on_page("12 Foo Road, Barton, Oxon OX1 2AB, United Kingdom", "street address", page) == PARTLY


def test_the_part_of_a_location_its_point_was_read_from_is_the_one_that_counts():
    """An address on the page says nothing of coordinates beside it that aren't."""
    page = Page("Otaihanga Landfill, 12 Otaihanga Road, Paraparaumu.")
    stated, kinds = "-40.8870, 175.0290 | 12 Otaihanga Road, Paraparaumu", "coordinates | street address"
    assert location_on_page(stated, kinds, page, basis="coordinates") == NOT_FOUND
    assert location_on_page(stated, kinds, page, basis="rooftop") == FOUND
    # With no usable point, the better of the two counts.
    assert location_on_page(stated, kinds, page) == FOUND


def test_a_point_the_page_gives_in_another_form_counts_within_250_m():
    page = Page("Site location: 40°53'01\"S 175°01'40\"E.")
    assert location_on_page("-40.8836, 175.0277", "coordinates", page, "coordinates", (-40.8836, 175.0277)) == FOUND
    assert location_on_page("-40.9000, 175.0277", "coordinates", page, "coordinates", (-40.9000, 175.0277)) == NOT_FOUND


def test_a_page_built_by_javascript_is_no_text_and_one_read_in_part_isnt_flagged(tmp_path):
    from waste_ai_search.page_text import PAGE_TEXT_DIR, save_page_text, text_file_name

    shell, long = "https://app.example/landfill", "https://register.example/permit.pdf"
    save_page_text(tmp_path / PAGE_TEXT_DIR, shell, "Loading...")
    save_page_text(tmp_path / PAGE_TEXT_DIR, long, "Permit for the Otaihanga landfill, first 300 pages.")
    notes = {
        text_file_name(shell): "little text: the page may be built by JavaScript",
        text_file_name(long): "its first 300 of 412 pages read",
    }
    checker = PageChecker(tmp_path, notes)
    rows = [
        {"source_id": text_file_name(url).removesuffix(".txt.gz"), "quoted_evidence_short": "capacity of 1,500,000 tonnes", "stated_location": ""}
        for url in (shell, long)
    ]
    for row in rows:
        checker.check(row)
    assert [row["quote_on_page"] for row in rows] == [NO_PAGE_TEXT, NOT_IN_PART_READ]


def test_counts_for_run_config():
    assert describe_results([FOUND, FOUND, NOT_FOUND, "", NO_PAGE_TEXT]) == "found 2, not found 1, no page text 1"
    assert describe_results([]) == "none checked"


# --- end to end ----------------------------------------------------------------------------------
def seed(site_id, name, lat, lon, iso3="NZL"):
    return {
        "facility_id": site_id, "site_id": site_id, "internal_facility_id": site_id, "year": "2022",
        "site_name": name, "facility_name": name, "country_iso3": iso3, "iso3c_plus": iso3,
        "latitude": str(lat), "longitude": str(lon), "contributing_data_sources": "osm_2022",
        "reference_year": "2022",
    }


def value(attribute, found, quote, value_date="2024"):
    return {"attribute_name": attribute, "value": found, "unit": "", "value_basis": "Direct",
            "confidence_score": "High", "value_date": value_date, "evidence_summary": "Says so.",
            "quoted_evidence_short": quote}


def source(url, values, name="Otaihanga Landfill", location="", kind=""):
    return {"url": url, "source_title": "Page", "publisher": "Kapiti Coast District Council",
            "source_type": "Municipal document", "publication_date": "2024-03-01",
            "facility_name_in_source": name, "facility_name_english": "", "stated_location": location,
            "stated_location_kind": kind, "values": values}


def rows(run_dir, name):
    return list(csv.DictReader((run_dir / name).open(encoding="utf-8-sig")))


def test_the_queue_flags_a_winning_source_its_own_page_doesnt_bear_out(tmp_path):
    run_dir = tmp_path / "run"
    write_csv_records(run_dir / "seed.csv", [seed("901", "Otaihanga Landfill", -40.8836, 175.0277)], seed_headers())
    council, news, blocked = "https://kapiticoast.example/otaihanga", "https://news.example/otaihanga", "https://blocked.example/x"
    raw = run_dir / "raw_foundry_responses"
    raw.mkdir(parents=True)
    (raw / "site_901.json").write_text(json.dumps({"site_id": "901", "sources": [
        # On its page, quote and location both.
        source(council, [value("operator", "Kapiti Coast District Council", "It is operated by the council")],
               location="-40.88365, 175.02770", kind="coordinates"),
        # Its quote isn't on its page, and neither is the location it states, 0.7 km from ours.
        source(news, [value("closing_year", "2011", "The landfill finally closed in 2011")],
               location="-40.8900, 175.0300", kind="coordinates"),
        # The link check kept no text of this one.
        source(blocked, [value("facility_status", "Inactive", "The site closed to all waste in 2007")]),
    ]}))
    save_page_text(run_dir / PAGE_TEXT_DIR, council, PAGE.plain)
    save_page_text(run_dir / PAGE_TEXT_DIR, news, "Coastal news. The Otaihanga site is now a park.")
    run_arbitration(PipelineConfig(input_csv=None, run_dir=run_dir, run_id="run", dataset_version="v"))

    evidence = {(row["attribute_name"], row["claimed_value"]): row for row in rows(run_dir, "evidence.csv")}
    assert (evidence[("operator", "Kapiti Coast District Council")]["quote_on_page"],
            evidence[("operator", "Kapiti Coast District Council")]["location_on_page"]) == (FOUND, FOUND)
    assert (evidence[("closing_year", "2011")]["quote_on_page"], evidence[("closing_year", "2011")]["location_on_page"]) == (NOT_FOUND, NOT_FOUND)
    assert evidence[("facility_status", "Inactive")]["quote_on_page"] == NO_PAGE_TEXT

    queue = {row["attribute_name"]: row for row in rows(run_dir, "review_queue.csv")}
    assert "CHECK" not in queue["operator"]["evidence_summary"]
    assert "CHECK: its quote is not on the source's page." in queue["closing_year"]["evidence_summary"]
    # The source was filed by the location it states, so that is flagged too.
    assert "CHECK: the location the source states is not on its page." in queue["closing_year"]["evidence_summary"]
    # No text kept is not evidence against it: nothing is flagged.
    assert "CHECK" not in queue["facility_status"]["evidence_summary"]

    config = {row[0]: row[1] for row in load_workbook(run_dir / "run_review.xlsx")["Run_Config"].values}
    assert config["quotes_on_page"].startswith("found ") and "not found 1" in config["quotes_on_page"]
    assert "not found 1" in config["locations_on_page"]
    queue_sheet = load_workbook(run_dir / "run_review.xlsx")["Review_Queue"]
    assert any("on its page" in str(rule.formula) for ranges in queue_sheet.conditional_formatting for rule in ranges.rules)


def test_a_found_location_row_flags_a_location_not_on_its_page(tmp_path):
    run_dir = tmp_path / "run"
    write_csv_records(run_dir / "seed.csv", [seed("902", "Otaihanga Landfill", -40.8836, 175.0277)], seed_headers())
    page_url = "https://register.example/otaihanga"
    raw = run_dir / "raw_foundry_responses"
    raw.mkdir(parents=True)
    # A location 0.6 km from ours, so a value to review; the page gives other coordinates.
    (raw / "site_902.json").write_text(json.dumps({"site_id": "902", "sources": [
        source(page_url, [], location="-40.8890, 175.0300", kind="coordinates"),
    ]}))
    save_page_text(run_dir / PAGE_TEXT_DIR, page_url, "Otaihanga Landfill. Coordinates -40.8836, 175.0277.")
    run_arbitration(PipelineConfig(input_csv=None, run_dir=run_dir, run_id="run", dataset_version="v"))
    [location] = [row for row in rows(run_dir, "review_queue.csv") if row["attribute_name"] == LOCATION_ATTRIBUTE]
    assert "CHECK: the location the source states is not on its page." in location["evidence_summary"]
