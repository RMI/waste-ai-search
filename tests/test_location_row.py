"""Where a source places the facility checks the match, and is no value of its own.

Every source states the facility's location as it writes it, and the locate step turns that into a
point (locate.py). Until step 2 of the approve-sources plan, a site's found_location was resolved
from those points and reviewed like any other value, and a source was filed under the site it was
placed at. Both are gone on purpose: a source's name and location never replace ours, and every
source stays with the site searched. What is left is on each of its values in found_values.csv:
the address and coordinates as written, the point read from them, and how far that is from our
site, which the 5 km check goes by (test_found_values.py). A cached attribute-first response still
arbitrates: its found coordinates become the location stated by the sources cited for both.
"""
from __future__ import annotations

import csv
import json

from waste_ai_search.arbitrate import run_arbitration
from waste_ai_search.input_loader import write_csv_records
from waste_ai_search.run_context import PipelineConfig


# --- end to end -----------------------------------------------------------------------------
NEWS = {
    "source_title": "El Hongo dump to close",
    "publisher": "Diario local",
    "source_type": "News report",
    "publication_date": "2023-05-01",
    "url": "https://news.example.mx/el-hongo",
}
REGULATOR = {**NEWS, "publisher": "SEMARNAT", "source_type": "Regulator", "url": "https://gob.example.mx/el-hongo"}


def arbitrate_el_hongo(tmp_path, response, **seed):
    """Arbitrate one site, at OSM's point unless given another, against one cached response."""
    from waste_ai_search.seed_source import seed_headers

    run_dir = tmp_path / "run"
    site = {
        "facility_id": "21", "site_id": "21", "internal_facility_id": "21", "year": "2016",
        "site_name": "EL HONGO", "facility_name": "EL HONGO", "country_iso3": "MEX", "iso3c_plus": "MEX",
        "contributing_data_sources": "osm_2022", "reference_year": "2016",
        "latitude": "32.5163", "longitude": "-116.2141", **seed,
    }
    write_csv_records(run_dir / "seed.csv", [site], seed_headers())
    raw = run_dir / "raw_foundry_responses"
    raw.mkdir(parents=True)
    (raw / "site_21.json").write_text(json.dumps({"site_id": "21", **response}))

    run_arbitration(PipelineConfig(input_csv=None, run_dir=run_dir, run_id="run", dataset_version="v"))
    return run_dir


def old_response(coordinates, source=NEWS):
    """An attribute-first response, as batch 1 cached them."""
    return {"attributes": [
        {"attribute_name": name, "value": value, "unit": "decimal degrees", "value_basis": "Direct",
         "confidence_score": "High", "value_date": "2023", "evidence_summary": "The article gives the site's coordinates.",
         "sources": [source]}
        for name, value in coordinates
    ]}


def stated(*sources):
    """A source-first response: each (source, the coordinates or grid reference it states), with
    no values."""
    return {"sources": [
        {**source, "stated_address": "", "stated_coordinates": coordinates, "values": []}
        for source, coordinates in sources
    ]}


def rows(run_dir, name):
    return list(csv.DictReader((run_dir / name).open(encoding="utf-8-sig")))


def found(run_dir):
    return rows(run_dir, "found_values.csv")


def operator(source):
    """`source` giving the operator."""
    return {**source, "values": [{"attribute_name": "operator", "value": "Ayuntamiento de Tecate", "unit": "",
                                  "value_basis": "Direct", "confidence_score": "High", "value_date": "2023",
                                  "evidence_summary": "The permit names the operator."}]}


def test_an_older_responses_coordinates_are_where_its_source_places_the_facility(tmp_path):
    """Batch 1's found latitude and longitude are read as the location its source states: they check
    the match, and are no value."""
    response = old_response([("found_latitude", "32.5122"), ("found_longitude", "-116.2134")])
    response["attributes"].append({
        "attribute_name": "operator", "value": "Ayuntamiento de Tecate", "unit": "", "value_basis": "Direct",
        "confidence_score": "High", "value_date": "2023", "evidence_summary": "It names the operator.", "sources": [NEWS],
    })
    [row] = found(arbitrate_el_hongo(tmp_path, response))
    assert row["attribute_name"] == "operator"
    assert (row["stated_coordinates"], row["source_address"], row["source_coordinates"], row["coordinates_from"]) == (
        "32.5122, -116.2134", "", "32.5122, -116.2134", "stated",
    )
    assert row["distance_km"] == "0.46"


def test_coarse_coordinates_are_a_hint_that_places_nothing_but_is_still_measured(tmp_path):
    """Two decimals are about a kilometre: too coarse to show as where the source places the facility,
    but not to say it is 14 km from ours. The 5 km check measures them, allowing for how coarse they
    are; it once skipped them, and a Tier 1 source 100 km away was approved unseen."""
    near = operator({**REGULATOR, "stated_coordinates": "32.51, -116.22"})
    [row] = found(arbitrate_el_hongo(tmp_path / "near", {"sources": [near]}))
    assert (row["source_coordinates"], row["coordinates_from"], row["distance_km"]) == ("", "", "0.89")
    assert "fewer than 3 decimals" in row["location_note"] and row["status"] == "SME review"

    far = operator({**REGULATOR, "stated_coordinates": "32.61, -116.31"})
    [row] = found(arbitrate_el_hongo(tmp_path / "far", {"sources": [far]}))
    assert (row["source_coordinates"], row["distance_km"], row["status"]) == ("", "13.76", "SME review")
    assert row["status_reason"] == "Tier 1 source, filling an empty field; it places the facility roughly 14 km from our site."


def test_half_a_location_is_no_location(tmp_path):
    """Reversed on purpose: a lone coordinate used to be listed under Leads as half a location."""
    wiki = {**NEWS, "publisher": "Wikimapia", "source_type": "Wiki or aggregator", "url": "https://wiki.example/el-hongo"}
    run_dir = arbitrate_el_hongo(tmp_path, old_response([("found_latitude", "32.5122")], source=wiki))
    assert found(run_dir) == []


def test_a_source_with_only_a_location_gives_no_value_but_is_a_source(tmp_path):
    """A found location is no longer a value (step 2 of the approve-sources plan): a source's location
    only checks that it is about the site searched."""
    run_dir = arbitrate_el_hongo(tmp_path, stated((REGULATOR, "32.5122, -116.2000")))
    assert found(run_dir) == []
    [source] = rows(run_dir, "sources.csv")
    assert source["url"] == REGULATOR["url"]


def test_a_value_row_carries_its_sources_facility_and_location(tmp_path):
    source = operator({**REGULATOR, "facility_name_in_source": "Relleno Sanitario El Hongo",
                       "facility_name_english": "El Hongo Sanitary Landfill",
                       "stated_address": "Carretera Tecate-Ensenada km 10, Tecate", "stated_coordinates": "32.5122, -116.2000"})
    [row] = found(arbitrate_el_hongo(tmp_path, {"sources": [source]}))
    assert (row["facility_name_in_source"], row["facility_name_english"]) == ("Relleno Sanitario El Hongo", "El Hongo Sanitary Landfill")
    # The address and coordinates it states, each as written; the coordinates win.
    assert (row["source_address"], row["stated_coordinates"]) == ("Carretera Tecate-Ensenada km 10, Tecate", "32.5122, -116.2000")
    assert (row["source_coordinates"], row["coordinates_from"]) == ("32.5122, -116.2", "stated")


def test_a_coordinates_only_sites_older_response_loads_nothing(tmp_path):
    """Brazil's inexact SINIR sites were searched only for where the dump is. A location is no value
    now, so a response cached then gives nothing to load, and the prompt asks such a site nothing."""
    from waste_ai_search.seed_source import seed_headers

    run_dir = tmp_path / "run"
    name = "Lixão de Porto Nacional"
    site = {"facility_id": "9", "site_id": "9", "internal_facility_id": "9", "year": "2024", "site_name": name,
            "facility_name": name, "country_iso3": "BRA", "iso3c_plus": "BRA", "is_location_exact": "FALSE",
            "contributing_data_sources": "sinir_2024", "reference_year": "2024",
            "latitude": "-10.1800", "longitude": "-48.3300"}
    write_csv_records(run_dir / "seed.csv", [site], seed_headers())
    raw = run_dir / "raw_foundry_responses"
    raw.mkdir(parents=True)
    permit = {**REGULATOR, "publisher": "NATURATINS", "url": "https://naturatins.example.br/licenca/123",
              "facility_name_in_source": "Lixão Municipal de Porto Nacional"}
    (raw / "site_9.json").write_text(json.dumps({"site_id": "9", **stated((permit, "-10.2481, -48.3612"))}))
    run_arbitration(PipelineConfig(input_csv=None, run_dir=run_dir, run_id="run", dataset_version="v"))
    assert found(run_dir) == []
