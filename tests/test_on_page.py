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
    assert location_on_page("", "-40.88365, 175.0277", PAGE) == FOUND
    # Trailing zeros say nothing.
    assert location_on_page("", "-40.88365, 175.02770", PAGE) == FOUND
    # A sign or hemisphere is not compared, but the digits are.
    assert location_on_page("", "40.8836512 S, 175.0277 E", PAGE) == FOUND
    assert location_on_page("", "-40.88365, 176.1234", PAGE) == PARTLY
    assert location_on_page("", "-41.2000, 174.7700", PAGE) == NOT_FOUND
    # 140.8836 is not 40.8836.
    assert location_on_page("", "140.8836, 75.0277", PAGE) == NOT_FOUND


def test_a_grid_reference_is_compared_without_its_spaces():
    assert location_on_page("", "TQ 310 806", PAGE) == FOUND
    assert location_on_page("", "TQ 311 806", PAGE) == NOT_FOUND


def test_of_a_stated_pair_the_better_counts_and_an_address_is_a_phrase():
    assert location_on_page("1 Otaihanga Road", "-40.88365, 175.0277", PAGE) == FOUND
    assert location_on_page("Otaihanga Road, Paraparaumu", "", PAGE) == FOUND
    assert location_on_page("", "", PAGE) == ""
    assert location_on_page("Otaihanga Road", "", None) == NO_PAGE_TEXT


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
    assert location_on_page("12 Foo Road, Barton", "", page) == FOUND
    assert location_on_page("12 Foo Road, Barton, Oxon OX1 2AB, United Kingdom", "", page) == PARTLY


def test_the_part_of_a_location_its_point_was_read_from_is_the_one_that_counts():
    """An address on the page says nothing of coordinates beside it that aren't."""
    page = Page("Otaihanga Landfill, 12 Otaihanga Road, Paraparaumu.")
    address, coordinates = "12 Otaihanga Road, Paraparaumu", "-40.8870, 175.0290"
    assert location_on_page(address, coordinates, page, basis="coordinates") == NOT_FOUND
    assert location_on_page(address, coordinates, page, basis="rooftop") == FOUND
    # With no usable point, the better of the two counts.
    assert location_on_page(address, coordinates, page) == FOUND


def test_coordinates_given_as_the_address_are_checked_as_coordinates():
    assert location_on_page("-40.88365, 175.02770", "", PAGE) == FOUND
    assert location_on_page("-41.2000, 174.7700", "", PAGE) == NOT_FOUND
    # Of a grid reference and coordinates, the one the point was read from counts.
    assert location_on_page("", "TQ 310 806 | -41.2000, 174.7700", PAGE, basis="grid reference") == FOUND
    assert location_on_page("", "TQ 310 806 | -41.2000, 174.7700", PAGE, basis="coordinates") == NOT_FOUND


def test_a_point_the_page_gives_in_another_form_counts_within_250_m():
    page = Page("Site location: 40°53'01\"S 175°01'40\"E.")
    assert location_on_page("", "-40.8836, 175.0277", page, "coordinates", (-40.8836, 175.0277)) == FOUND
    assert location_on_page("", "-40.9000, 175.0277", page, "coordinates", (-40.9000, 175.0277)) == NOT_FOUND


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


def source(url, values, name="Otaihanga Landfill", location="", source_type="Municipal document"):
    return {"url": url, "source_title": "Page", "publisher": "Kapiti Coast District Council",
            "source_type": source_type, "publication_date": "2024-03-01",
            "facility_name_in_source": name, "facility_name_english": "", "stated_address": "",
            "stated_coordinates": location, "values": values}


def rows(run_dir, name):
    return list(csv.DictReader((run_dir / name).open(encoding="utf-8-sig")))


def test_found_values_says_whether_each_quote_and_location_is_on_its_page(tmp_path):
    """And the Site_Review tab flags a source for review whose page doesn't bear them out, in place of
    the review queue's CHECK notes."""
    run_dir = tmp_path / "run"
    write_csv_records(run_dir / "seed.csv", [seed("901", "Otaihanga Landfill", -40.8836, 175.0277)], seed_headers())
    council, news, blocked = "https://kapiticoast.example/otaihanga", "https://news.example/otaihanga", "https://blocked.example/x"
    raw = run_dir / "raw_foundry_responses"
    raw.mkdir(parents=True)
    (raw / "site_901.json").write_text(json.dumps({"site_id": "901", "sources": [
        # On its page, quote and location both.
        source(council, [value("operator", "Kapiti Coast District Council", "It is operated by the council")],
               location="-40.88365, 175.02770"),
        # Its quote isn't on its page, and neither is the location it states, 0.7 km from ours. A
        # Tier 3 source, so it goes to an SME.
        source(news, [value("closing_year", "2011", "The landfill finally closed in 2011")],
               location="-40.8900, 175.0300", source_type="News report"),
        # The link check kept no text of this one.
        source(blocked, [value("facility_status", "Inactive", "The site closed to all waste in 2007")]),
    ]}))
    save_page_text(run_dir / PAGE_TEXT_DIR, council, PAGE.plain)
    save_page_text(run_dir / PAGE_TEXT_DIR, news, "Coastal news. The Otaihanga site is now a park.")
    run_arbitration(PipelineConfig(input_csv=None, run_dir=run_dir, run_id="run", dataset_version="v"))

    found = {row["attribute_name"]: row for row in rows(run_dir, "found_values.csv")}
    assert (found["operator"]["quote_on_page"], found["operator"]["location_on_page"]) == (FOUND, FOUND)
    assert (found["closing_year"]["quote_on_page"], found["closing_year"]["location_on_page"]) == (NOT_FOUND, NOT_FOUND)
    assert found["facility_status"]["quote_on_page"] == NO_PAGE_TEXT

    config = {row[0]: row[1] for row in load_workbook(run_dir / "run_review.xlsx")["Run_Config"].values}
    assert config["quotes_on_page"].startswith("found ") and "not found 1" in config["quotes_on_page"]
    assert "not found 1" in config["locations_on_page"]
    sheet = load_workbook(run_dir / "run_review.xlsx")["Site_Review"]
    header = [cell.value for cell in sheet[1]]
    listed = [dict(zip(header, row)) for row in sheet.iter_rows(min_row=2, values_only=True)]
    [review] = [row for row in listed if row["source_url"] == news and not row["check"]]
    assert review["flags"] == "address or coordinates not found on the page"
    # closing_year is a key value from a Tier 3 source, so its quote is flagged on its own row.
    [checked] = [row for row in listed if row["check"] == "closing_year"]
    assert checked["flags"] == "quote not found on the page"


# --- a page whose kept text is only its menu (#25) ------------------------------------------------
# As the link check kept xeu.mx's page for site 9233: the title, then its menu.
MENU = "Clausuran en su totalidad basurero municipal de Veracruz - xeu noticias veracruz\n" + "\n".join(
    ["Portada", "Veracruz", "Boca del Río", "Estado", "Nacional", "Internacional", "Deportes", "Policiaca", "Espectáculos",
     "Sociedad", "Tecnociencia", "Al minuto", "Finanzas", "Destacadas", "Viral", "Videos", "Opinión", "Sondeo",
     "XEU 98.1 FM", "ESCUCHA EN VIVO", "Servicios Informativos", "Radio en vivo", "Mensaje a Cabina",
     "Teléfonos de Emergencia", "Programación xeu 98.1 FM", "Redes Sociales"]
)
ARTICLE = MENU + (
    "\nFue clausurado en su totalidad el basurero municipal de Veracruz la tarde de este martes, tras una "
    "revisión de la Procuraduría de Medio Ambiente junto con representantes del ayuntamiento."
)


def test_a_quote_missing_from_an_html_page_that_kept_only_its_menu_is_no_page_text(tmp_path):
    """The article was built after the page loaded, so the three quotes read "not found" and flagged
    a page the judges called ours. A quote that is there is still found; a PDF or an article with
    prose is checked as before."""
    from waste_ai_search.on_page import menu_only
    from waste_ai_search.page_text import text_file_name

    assert len(MENU) < 1000 and menu_only(MENU) and not menu_only(ARTICLE)
    pages = {"https://xeu.example/menu": MENU, "https://xeu.example/menu.pdf": MENU, "https://xeu.example/nota": ARTICLE}
    for url, text in pages.items():
        save_page_text(tmp_path / PAGE_TEXT_DIR, url, text)
    kinds = {text_file_name(url): "pdf" if url.endswith(".pdf") else "html" for url in pages}
    checker = PageChecker(tmp_path, {}, kinds)

    def check(url, quote, address=""):
        row = {"source_id": text_file_name(url).removesuffix(".txt.gz"), "quoted_evidence_short": quote,
               "stated_address": address, "stated_coordinates": ""}
        checker.check(row)
        return row["quote_on_page"], row["location_on_page"]

    missing, title, address = "Fue clausurado en su totalidad el basurero municipal", "Clausuran en su totalidad basurero municipal", "Carretera Veracruz-Xalapa, El Guayabo"
    assert check("https://xeu.example/menu", missing, address) == (NO_PAGE_TEXT, NO_PAGE_TEXT)
    assert check("https://xeu.example/menu", title) == (FOUND, "")
    assert check("https://xeu.example/menu.pdf", missing, address) == (NOT_FOUND, NOT_FOUND)
    assert check("https://xeu.example/nota", "basurero municipal de Veracruz fue abierto en 1990") == (NOT_FOUND, "")


def test_site_review_says_why_a_page_had_no_automatic_check_and_found_values_keeps_the_verdict(tmp_path):
    from waste_ai_search.link_check import LINK_CHECK_FILE, LinkResult, save_results
    from waste_ai_search.page_text import text_file_name

    run_dir = tmp_path / "run"
    write_csv_records(run_dir / "seed.csv", [seed("9233", "Veracruz Sanitary Landfill", 19.2329, -96.2254, iso3="MEX")], seed_headers())
    urls = {
        "refused": "https://oem.example/nota", "scan": "https://gob.example/acta.pdf",
        "menu": "https://xeu.example/menu", "read": "https://diario.example/nota",
    }
    raw = run_dir / "raw_foundry_responses"
    raw.mkdir(parents=True)
    (raw / "site_9233.json").write_text(json.dumps({"site_id": "9233", "sources": [
        source(url, [value("operator", "Ayuntamiento de Veracruz", "Fue clausurado en su totalidad el basurero municipal")],
               name="Basurero municipal de Veracruz", source_type="News report")
        for url in urls.values()
    ]}))
    save_page_text(run_dir / PAGE_TEXT_DIR, urls["menu"], MENU)
    save_page_text(run_dir / PAGE_TEXT_DIR, urls["read"], ARTICLE)
    save_results(run_dir / LINK_CHECK_FILE, {
        urls["refused"]: LinkResult(urls["refused"], "Unverified", "403", urls["refused"]),
        urls["scan"]: LinkResult(urls["scan"], "OK", "200", urls["scan"], text_kind="pdf",
                                 text_note="a PDF with no text layer, probably scanned"),
        urls["menu"]: LinkResult(urls["menu"], "OK", "200", urls["menu"], text_file=text_file_name(urls["menu"]),
                                 text_kind="html", text_chars=len(MENU)),
        urls["read"]: LinkResult(urls["read"], "OK", "200", urls["read"], text_file=text_file_name(urls["read"]),
                                 text_kind="html", text_chars=len(ARTICLE)),
    })
    run_arbitration(PipelineConfig(input_csv=None, run_dir=run_dir, run_id="run", dataset_version="v"))

    sheet = load_workbook(run_dir / "run_review.xlsx")["Site_Review"]
    header = [cell.value for cell in sheet[1]]
    listed = {row["source_url"]: row for row in (dict(zip(header, r)) for r in sheet.iter_rows(min_row=2, values_only=True)) if not row["check"]}
    assert {name: listed[url]["link_status"] for name, url in urls.items()} == {
        "refused": "Unverified: the site refuses automated readers (403), so quotes weren't checked",
        "scan": "OK, but it's a scanned PDF, so quotes weren't checked",
        "menu": "OK, but its text is only the site's menu, so quotes weren't checked",
        "read": "OK",
    }
    # Its quote isn't flagged on the menu's row, as it was when it read "not found".
    assert not listed[urls["menu"]]["flags"]
    found = {row["source_url"]: row for row in rows(run_dir, "found_values.csv")}
    assert {name: found[url]["link_status"] for name, url in urls.items()} == {
        "refused": "Unverified", "scan": "OK", "menu": "OK", "read": "OK",
    }
    assert (found[urls["menu"]]["quote_on_page"], found[urls["read"]]["quote_on_page"]) == (NO_PAGE_TEXT, FOUND)
