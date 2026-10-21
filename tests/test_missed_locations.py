"""How often a cited page states a location the agent didn't copy (missed_locations.py), the
measurement behind deciding whether to add a page-reading pass."""
from __future__ import annotations

import csv
import json

import pytest

from waste_ai_search.input_loader import write_csv_records
from waste_ai_search.link_check import LINK_CHECK_FILE
from waste_ai_search.missed_locations import (
    COORDINATES,
    FAR_POINT,
    GRID_REFERENCE,
    POSTCODE,
    STREET_ADDRESS,
    Site,
    find_location,
    measure_run,
    summary,
    write_missed,
)
from waste_ai_search.page_text import PAGE_TEXT_DIR, save_page_text, text_file_name
from waste_ai_search.payload import source_id_for

OTAIHANGA = Site((-40.8836, 175.0277), "NZL")
LONDON = Site((51.5136, -0.1022), "GBR")
OTTAWA = Site((45.3311, -75.6081), "CAN")


# --- finding a location in a page's text ----------------------------------------------------------
def test_coordinates_near_a_site_that_cited_the_page_count():
    finding = find_location("Site details. Location: -40.88365, 175.02770. Open daily.", [OTAIHANGA])
    assert finding.kind == COORDINATES and finding.km < 1
    assert "-40.88365" in finding.text


def test_coordinates_far_from_every_site_or_too_coarse_dont():
    # Another country's facility, or a pair of figures in a table.
    assert find_location("Location: -33.8688, 151.2093.", [OTAIHANGA]) is None
    # Two decimals is only a hint.
    assert find_location("Location: -40.88, 175.03.", [OTAIHANGA]) is None


def test_a_grid_reference_counts_for_a_uk_site():
    finding = find_location("Permit EPR/AB1234CD. Grid reference TQ 31090 80600.", [LONDON])
    assert finding.kind == GRID_REFERENCE and finding.km < 5


def test_a_street_address_and_a_lot_and_concession_count():
    assert find_location("The landfill is at 4800 Old Highway 99, Abbotsford.", [OTAIHANGA]).kind == STREET_ADDRESS
    assert find_location("Located on Lot 12, Concession 4, Township of Osgoode.", [OTTAWA]).kind == STREET_ADDRESS


@pytest.mark.parametrize("text", [
    "It takes 200 tonnes a day by road.",
    "The site lies 3 km from the highway.",
    "In 1998 close to the river.",
    "A fence 2 m above the property line.",
])
def test_figures_beside_a_road_word_are_no_street_address(text):
    assert find_location(text, [OTAIHANGA]) is None


@pytest.mark.parametrize("text", ["Vertedero en Calle Mayor 12, Madrid.", "Deponie Hauptstraße 5, Berlin.", "Aterro na Rodovia BR-101, km 25."])
def test_a_road_named_before_its_number_is_a_street_address(text):
    assert find_location(text, [OTAIHANGA]).kind == STREET_ADDRESS


def test_a_registers_other_facility_is_counted_apart_and_not_missed(tmp_path):
    """Brookhurst Wood, 50 km from a London site, was counted as the London site's location."""
    finding = find_location("Brookhurst Wood landfill, 51.0921, -0.3225.", [LONDON])
    assert finding.kind == FAR_POINT and 40 < finding.km < 60
    # The nearest point is the one taken, and one within 5 km is this facility's.
    finding = find_location("Brookhurst Wood 51.0921, -0.3225. This site: 51.5140, -0.1030.", [LONDON])
    assert finding.kind == COORDINATES and finding.km < 1


def test_only_a_postcode_counts_as_a_postcode_for_the_sites_country():
    assert find_location("Write to us: Ottawa ON K1A 0B1.", [OTTAWA]).kind == POSTCODE
    assert find_location("Write to us: Ottawa ON K1A 0B1.", [OTAIHANGA]) is None


def test_a_page_with_no_location_finds_none():
    assert find_location("The landfill closed in 2007 and is now a park.", [OTAIHANGA]) is None


# --- measuring a run ----------------------------------------------------------------------------
def build_run(tmp_path, pages):
    """A run whose link check kept the text of `pages`: [(url, text, stated location, basis, site)]."""
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    write_csv_records(run_dir / "seed.csv", [
        {"site_id": "901", "latitude": "-40.8836", "longitude": "175.0277", "country_iso3": "NZL"},
        {"site_id": "902", "latitude": "51.5136", "longitude": "-0.1022", "country_iso3": "GBR"},
    ], ["site_id", "latitude", "longitude", "country_iso3"])
    evidence, sources, checked = [], [], {}
    for url, text, stated, basis, site in pages:
        source_id = source_id_for(url, "")
        evidence.append({"source_id": source_id, "site_id": site, "searched_site_id": site,
                         "stated_location": stated, "location_basis": basis})
        sources.append({"source_id": source_id, "url": url, "source_type": "Municipal document"})
        checked[url] = {"url": url, "verdict": "OK", "status": "200", "resolved_url": url}
        if text:
            save_page_text(run_dir / PAGE_TEXT_DIR, url, text)
            checked[url].update(text_file=text_file_name(url), text_kind="html")
    write_csv_records(run_dir / "evidence.csv", evidence, ["source_id", "site_id", "searched_site_id", "stated_location", "location_basis"])
    write_csv_records(run_dir / "sources.csv", sources, ["source_id", "url", "source_type"])
    (run_dir / LINK_CHECK_FILE).write_text(json.dumps(checked))
    return run_dir


def test_a_run_counts_the_pages_whose_location_the_agent_left_out(tmp_path):
    run_dir = build_run(tmp_path, [
        ("https://a.example/coords", "Location: -40.88365, 175.02770.", "", "", "901"),
        ("https://b.example/address", "Find us at 12 Otaihanga Road, Paraparaumu.", "12 Otaihanga Road, Paraparaumu", "street", "901"),
        ("https://c.example/grid", "Grid reference TQ 31090 80600.", "", "", "902"),
        ("https://d.example/nothing", "Closed in 2007.", "", "", "901"),
        ("https://e.example/blocked", "", "", "", "901"),
    ])
    measurement = measure_run(run_dir)
    by_url = {report.url: report for report in measurement.reports}
    # Only pages whose text was kept are measured.
    assert set(by_url) == {"https://a.example/coords", "https://b.example/address", "https://c.example/grid", "https://d.example/nothing"}
    assert by_url["https://a.example/coords"].missed and by_url["https://c.example/grid"].missed
    assert not by_url["https://b.example/address"].missed  # the agent stated it
    assert by_url["https://b.example/address"].agent_usable
    text = summary(measurement)
    assert "Pages measured (text kept, cited in evidence.csv): 4" in text
    assert "no location: 2 (50%)" in text

    out = tmp_path / "missed.csv"
    assert write_missed(out, measurement) == 2
    rows = list(csv.DictReader(out.open(encoding="utf-8")))
    assert {row["found_kind"] for row in rows} == {COORDINATES, GRID_REFERENCE}


def test_a_run_with_no_page_text_says_how_to_get_it(tmp_path):
    run_dir = build_run(tmp_path, [("https://a.example/x", "", "", "", "901")])
    with pytest.raises(SystemExit, match="check-links"):
        measure_run(run_dir)


def test_the_script_runs_on_a_run_folder(tmp_path, capsys):
    import importlib.util
    from pathlib import Path

    run_dir = build_run(tmp_path, [("https://a.example/coords", "Location: -40.88365, 175.02770.", "", "", "901")])
    script = Path(__file__).resolve().parents[1] / "scripts" / "measure_missed_locations.py"
    spec = importlib.util.spec_from_file_location("measure_missed_locations", script)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert module.main(["--run-dir", str(run_dir), "--out", str(tmp_path / "missed.csv")]) == 0
    assert "1 missed page(s) written" in capsys.readouterr().out
