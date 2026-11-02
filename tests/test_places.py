"""Our site's municipality and state, and the source's, compared (Mexico test, 30 October).

Site 6086, "BASURERO MUNICIPAL" in San Sebastián Tecomaxtlahuaca, Oaxaca, was matched to a Tier 1
page about the dump in Tlapa de Comonfort, Guerrero, about 60 km away. No source placed it on a
map, so the 5 km check never ran. Each prompt now names our site's municipality and state, looked
up from our coordinates where the seed has none; each source gives the town and state it puts the
facility in; and Site_Review shows both and flags a source in another state. Since 1 November it
also asks an SME to check a source in another municipality of our state, and one naming no facility.
"""
from __future__ import annotations

import csv
import json

from openpyxl import load_workbook

from waste_ai_search.arbitrate import run_arbitration
from waste_ai_search.found_values import place_words
from waste_ai_search.geocoder import (
    SITE_PLACES_FILE,
    load_geocode_cache,
    look_up_places,
    place_text,
    save_site_places,
    with_places,
)
from waste_ai_search.input_loader import write_csv_records
from waste_ai_search.locate import STATE_NAMES_FILE, canonical_state, load_locations, nominatim_place, same_state
from waste_ai_search.run_context import PipelineConfig
from waste_ai_search.search import _CountingGeocoder, locate_run
from waste_ai_search.seed_source import seed_headers


# --- our site's place -----------------------------------------------------------------------------
def test_a_mexican_municipio_comes_back_as_the_county():
    address = {"county": "San Sebastián Tecomaxtlahuaca", "state": "Oaxaca", "country": "México"}
    assert nominatim_place(address) == {"municipality": "San Sebastián Tecomaxtlahuaca", "admin1": "Oaxaca", "admin2": ""}


def test_a_city_is_named_before_its_county():
    address = {"city": "Texcoco de Mora", "county": "Texcoco", "state": "Estado de México"}
    assert nominatim_place(address)["municipality"] == "Texcoco de Mora"


def test_a_point_nowhere_has_no_place():
    assert nominatim_place({"country": "México"}) == {}


class Places:
    """A geocoder that looks points up, counting each lookup."""

    name = "nominatim"

    def __init__(self):
        self.asked = []

    def reverse(self, latitude, longitude):
        self.asked.append((latitude, longitude))
        return {"municipality": "San Sebastián Tecomaxtlahuaca", "admin1": "Oaxaca", "admin2": ""}

    def state(self, text, iso2):
        self.asked.append(text)
        return {
            "Gro.": "Guerrero", "Guerrero": "Guerrero", "Oaxaca": "Oaxaca", "Oax.": "Oaxaca", "Veracruz": "Veracruz",
            "Ver.": "Veracruz", "Estado de México": "Estado de México", "Chihuahua": "Chihuahua",
        }.get(text, "")


def site(site_id="6086", **extra):
    return {"site_id": site_id, "site_name": "BASURERO MUNICIPAL", "country_iso3": "MEX",
            "latitude": "17.38", "longitude": "-98.021", **extra}


def test_a_site_with_no_place_is_looked_up_once_into_the_cache(tmp_path):
    cache = tmp_path / "geocode_cache.jsonl"
    geocoder = Places()
    look_up_places([site()], cache, geocoder)
    look_up_places([site()], cache, geocoder)
    assert geocoder.asked == [(17.38, -98.021)]
    record = load_geocode_cache(cache)["site:6086"]
    assert (record["municipality"], record["admin1"], record["geocoder_provider"]) == (
        "San Sebastián Tecomaxtlahuaca", "Oaxaca", "nominatim",
    )
    # Only a municipality and a state: nothing that would place our point more closely.
    assert record["formatted_address"] == ""


def test_a_site_whose_seed_has_its_place_is_not_looked_up(tmp_path):
    geocoder = Places()
    look_up_places([site(municipality="Brasília", admin1="DF")], tmp_path / "cache.jsonl", geocoder)
    assert geocoder.asked == []


def test_a_geocoder_that_cant_look_points_up_leaves_the_sites_alone(tmp_path):
    class Addresses:
        name = "azure-maps"

    sites = [site()]
    assert look_up_places(sites, tmp_path / "cache.jsonl", Addresses()) == sites
    assert not (tmp_path / "cache.jsonl").exists()


def test_the_run_keeps_the_place_each_prompt_named(tmp_path):
    save_site_places(tmp_path, [site(municipality="San Sebastián Tecomaxtlahuaca", admin1="Oaxaca")])
    save_site_places(tmp_path, [site("1", municipality="Puebla", admin1="Puebla")])
    [ours, other] = with_places([site(), site("1", admin1="Puebla de Zaragoza")], tmp_path)
    assert place_text(ours) == "San Sebastián Tecomaxtlahuaca, Oaxaca"
    # The seed's own place wins over what was looked up.
    assert other["admin1"] == "Puebla de Zaragoza"
    assert set(json.loads((tmp_path / SITE_PLACES_FILE).read_text())) == {"1", "6086"}


# --- states -------------------------------------------------------------------------------------
def test_a_state_however_written_is_named_as_the_geocoder_names_it():
    cache, geocoder = {}, Places()
    assert canonical_state("Gro.", "MEX", cache, geocoder, live=True) == "Guerrero"
    assert canonical_state("Gro.", "MEX", cache, geocoder, live=True) == "Guerrero"
    assert geocoder.asked == ["Gro."]
    # Arbitration reads the cache, and asks nothing.
    assert canonical_state("Gro.", "MEX", cache, None, live=False) == "Guerrero"
    assert canonical_state("Guerrero", "MEX", cache, None, live=False) == ""


def test_states_compare_without_case_or_accents_mattering():
    assert same_state("Nuevo León", "nuevo leon")
    assert not same_state("Guerrero", "Oaxaca")


def test_the_locate_step_counts_state_lookups_only_where_the_geocoder_has_them():
    assert _CountingGeocoder(Places()).state("Gro.", "mx") == "Guerrero"

    class Addresses:
        name = "azure-maps"

        def geocode(self, address, iso2):
            return None

    assert not hasattr(_CountingGeocoder(Addresses()), "state")


# --- a run ----------------------------------------------------------------------------------------
def seed_row(site_id, name, lat, lon):
    return {
        "facility_id": site_id, "site_id": site_id, "internal_facility_id": site_id, "year": "2022",
        "site_name": name, "facility_name": name, "country_iso3": "MEX", "iso3c_plus": "MEX",
        "latitude": str(lat), "longitude": str(lon), "contributing_data_sources": "osm_2022",
        "reference_year": "2022",
    }


def source(url, town, state, name="basurero municipal"):
    return {
        "url": url, "source_title": "Page", "publisher": "Congreso", "source_type": "Legislative record",
        "publication_date": "2024-03-01", "facility_name_in_source": name,
        "facility_name_english": "municipal dump" if name else "", "stated_address": "", "stated_coordinates": "",
        "stated_municipality": town, "stated_state": state,
        "values": [{"attribute_name": "operator", "value": "Ayuntamiento", "unit": "", "value_basis": "Direct",
                    "confidence_score": "High", "value_date": "2024", "evidence_summary": "Says so.",
                    "quoted_evidence_short": "Lo dice."}],
    }


def run(tmp_path, sources, ours=None, place=("San Sebastián Tecomaxtlahuaca", "Oaxaca")):
    """Arbitrate one site of ours, 6086 unless `ours` is another seed row, in `place` (its
    municipality and state), against the sources its search cited."""
    ours = ours or seed_row("6086", "BASURERO MUNICIPAL", 17.38, -98.021)
    run_dir = tmp_path / "run"
    write_csv_records(run_dir / "seed.csv", [ours], seed_headers())
    raw = run_dir / "raw_foundry_responses"
    raw.mkdir(parents=True)
    (raw / f"site_{ours['site_id']}.json").write_text(json.dumps({"site_id": ours["site_id"], "sources": sources}))
    save_site_places(run_dir, [site(ours["site_id"], municipality=place[0], admin1=place[1])])
    config = PipelineConfig(input_csv=None, run_dir=run_dir, run_id="run", dataset_version="v")
    locate_run(config, geocoder=Places())
    run_arbitration(config)
    return run_dir


def site_review(run_dir):
    ws = load_workbook(run_dir / "run_review.xlsx")["Site_Review"]
    headers = [cell.value for cell in ws[1]]
    return [dict(zip(headers, row)) for row in ws.iter_rows(min_row=2, values_only=True)]


def test_a_source_in_another_state_is_flagged_beside_both_places(tmp_path):
    run_dir = run(tmp_path, [
        source("https://congreso.example/tlapa", "Tlapa de Comonfort", "Gro."),
        source("https://oaxaca.example/dump", "Tecomaxtlahuaca", "Oax."),
        source("https://unknown.example/dump", "", "Somewhere"),
    ])
    rows = {row["source_url"]: row for row in site_review(run_dir)}
    tlapa = rows["https://congreso.example/tlapa"]
    assert tlapa["our_place"] == "San Sebastián Tecomaxtlahuaca, Oaxaca"
    assert tlapa["source_place"] == "Tlapa de Comonfort, Gro."
    assert tlapa["flags"] == "in a different state from our site: the source says Guerrero"
    # The same state, however written, and a state the geocoder doesn't know, are not flagged.
    assert not rows["https://oaxaca.example/dump"]["flags"]
    assert not rows["https://unknown.example/dump"]["flags"]
    # The flagged source comes first under our site.
    assert next(iter(rows)) == "https://congreso.example/tlapa"
    # The locate step kept every state it named, ours included.
    assert {entry["text"]: entry["name"] for entry in load_locations(run_dir / STATE_NAMES_FILE).values()} == {
        "Oaxaca": "Oaxaca", "Gro.": "Guerrero", "Oax.": "Oaxaca", "Somewhere": "",
    }
    found = list(csv.DictReader((run_dir / "found_values.csv").open(encoding="utf-8-sig")))
    assert {row["source_place"] for row in found} == {"Tlapa de Comonfort, Gro.", "Tecomaxtlahuaca, Oax.", "Somewhere"}


def test_a_source_in_another_state_says_so_in_its_status_reason(tmp_path):
    """It once read "it says the site is Not a Waste Facility"."""
    run_dir = run(tmp_path, [source("https://congreso.example/tlapa", "Tlapa de Comonfort", "Gro.")])
    [row] = list(csv.DictReader((run_dir / "found_values.csv").open(encoding="utf-8-sig")))
    assert row["status_reason"].endswith("filling an empty field; it puts the facility in another state, Guerrero.")


# --- municipalities and names ---------------------------------------------------------------------
VERACRUZ = seed_row("9233", "VERACRUZ SANITARY LANDFILL", 19.232865, -96.225432)


def test_a_source_in_another_municipality_of_our_state_is_flagged_for_an_sme_to_check(tmp_path):
    """At 9233 gpt-5.4 returned four pages about the El Guayabo landfill in Medellín de Bravo for our
    landfill in Veracruz, judged not ours: the same state, and no address to place. Their
    municipality shares no word with ours or our site's name. A landfill can serve one town from the
    next, so the flag only asks an SME to check; flagged pages come first."""
    run_dir = run(tmp_path, [
        source("https://veracruz.example/relleno", "Veracruz", "Ver.", name="relleno sanitario de Veracruz"),
        source("https://oem.example/el-guayabo", "Medellín de Bravo", "Veracruz", name="relleno sanitario El Guayabo"),
    ], ours=VERACRUZ, place=("Veracruz", "Veracruz"))
    rows = site_review(run_dir)
    assert [(row["source_url"], row["flags"]) for row in rows] == [
        ("https://oem.example/el-guayabo", "the source puts the site in Medellín de Bravo; ours is in Veracruz - check it is our site"),
        ("https://veracruz.example/relleno", None),
    ]
    found = {row["source_url"]: row for row in csv.DictReader((run_dir / "found_values.csv").open(encoding="utf-8-sig"))}
    assert found["https://oem.example/el-guayabo"]["status_reason"].endswith(
        "filling an empty field; it puts the facility in Medellín de Bravo, and our site is in Veracruz."
    )
    config = {row[0]: row[1] for row in load_workbook(run_dir / "run_review.xlsx")["Run_Config"].values}
    assert config["pairs_with_warnings"] == "1: in another municipality 1"


def test_a_source_in_the_municipality_our_sites_name_gives_is_not_flagged(tmp_path):
    """Our point for 3892 geocodes to Acolman, but the site's name says Ecatepec, where its sources
    put it. A source naming no municipality, only our state, isn't compared."""
    chiconautla = seed_row("3892", "SANTA MARIA CHICONAUTLA MUNICIPAL LANDFILL ECATEPEC DE MORELOS", 19.641089, -98.976384)
    run_dir = run(tmp_path, [
        source("https://ecatepec.example/chiconautla", "Ecatepec", "Estado de México", name="vertedero municipal de Chiconautla"),
        source("https://edomex.example/vertederos", "", "Estado de México", name="vertedero de Chiconautla"),
    ], ours=chiconautla, place=("Acolman", "Estado de México"))
    assert [(row["our_place"], row["source_place"], row["flags"]) for row in site_review(run_dir)] == [
        ("Acolman, Estado de México", "Ecatepec, Estado de México", None),
        ("Acolman, Estado de México", "Estado de México", None),
    ]


def test_a_source_naming_no_facility_is_flagged_for_an_sme_to_check(tmp_path):
    """All six of round 2's pages that named no facility were gpt-5.4's, and none was judged ours."""
    bocoyna = seed_row("247", "BOCOYNA LANDFILL", 27.860113, -107.585116)
    run_dir = run(tmp_path, [
        source("https://chihuahua.example/relleno", "Bocoyna", "Chihuahua", name="relleno sanitario de Bocoyna"),
        source("https://congreso.example/iniciativa", "Bocoyna", "Chihuahua", name=""),
    ], ours=bocoyna, place=("Bocoyna", "Chihuahua"))
    assert [(row["source_url"], row["flags"]) for row in site_review(run_dir)] == [
        ("https://congreso.example/iniciativa", "the source names no facility"),
        ("https://chihuahua.example/relleno", None),
    ]


def test_places_are_compared_by_their_words_without_accents_case_or_words_like_ciudad():
    assert place_words("Medellín de Bravo") == {"medellin", "bravo"}
    assert place_words("H. Matamoros") == place_words("Heroica Matamoros") == {"matamoros"}
    assert place_words("Ciudad Obregón") == {"obregon"}
    assert place_words("Municipio de Ecatepec de Morelos") == {"ecatepec", "morelos"}
