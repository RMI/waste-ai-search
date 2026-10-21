"""The prompt asks for sources first (build step 4 of the source-matching plan).

Each source reports the facility it describes - its name and location as the source writes them -
and the values it gives. The agent no longer decides whether a source is about this site: it
reports what each source says, and code decides. Our coordinates go in rounded to about 1 km, so
there is nothing worth copying.
"""
from __future__ import annotations

import json
import re

from waste_ai_search.prompt_builder import build_site_prompt, requested_attributes
from waste_ai_search.search import select_sites
from waste_ai_search.run_context import PipelineConfig


THUNDER_BAY = {
    "site_id": "13755",
    "site_name": "Thunder Bay Landfill",
    "country_iso3": "CAN",
    "latitude": "48.843368376131856",
    "longitude": "-87.42962607259884",
    "municipality": "Thunder Bay",
    "admin1": "Ontario",
    "admin2": "Thunder Bay District",
    "formatted_address": "1 Landfill Road, Thunder Bay, ON",
    "contributing_data_sources": "osm_2022",
}


def prompt_for(site, attributes=None):
    return build_site_prompt(site, attributes if attributes is not None else requested_attributes(site), max_web_searches=6)


def section(prompt, title):
    return prompt.split(f"\n{title}:\n", 1)[1].split("\n\n", 1)[0]


def contract(prompt):
    return json.loads(prompt.split("JSON output contract:\n", 1)[1])


def test_the_contract_is_sources_first():
    output = contract(prompt_for(THUNDER_BAY))
    assert set(output) == {"site_id", "sources", "unverified_leads", "search_notes"}
    (source,) = output["sources"]
    assert list(source) == [
        "url", "source_title", "publisher", "source_type", "publication_date", "language", "paywall_flag",
        "facility_name_in_source", "facility_name_english", "stated_location", "stated_location_kind", "values",
    ]
    assert list(source["values"][0]) == [
        "attribute_name", "value", "unit", "value_basis", "confidence_score", "value_date",
        "evidence_summary", "search_terms_used", "quoted_evidence_short",
    ]
    assert source["stated_location_kind"] == "coordinates / grid reference / street address / place description / none"


def test_our_coordinates_are_rounded_to_about_a_kilometre_with_the_place_names():
    prompt = prompt_for(THUNDER_BAY)
    context = json.loads(section(prompt, "Site context"))
    assert (context["latitude"], context["longitude"]) == (48.84, -87.43)
    assert (context["municipality"], context["admin1"], context["admin2"]) == ("Thunder Bay", "Ontario", "Thunder Bay District")
    assert "48.8433" not in prompt and "87.4296" not in prompt
    # A reverse-geocoded street address would leak the point just as well.
    assert "1 Landfill Road" not in prompt


def test_coordinates_are_no_longer_asked_for_as_values():
    prompt = prompt_for(THUNDER_BAY)
    assert "found_latitude" not in prompt and "found_longitude" not in prompt
    targets = json.loads(section(prompt, "Target attributes"))
    assert "found_location" not in {target["attribute_name"] for target in targets}
    assert "found_facility_name" in {target["attribute_name"] for target in targets}
    assert "Where this facility is matters too" in prompt


def test_the_agent_reports_what_each_source_says_and_code_decides():
    prompt = " ".join(prompt_for(THUNDER_BAY).split())
    assert "Identity first" not in prompt
    assert "If you cannot confirm you found this specific facility" not in prompt
    assert "Research only this site." in prompt
    assert "Our code decides whether that facility is this site" in prompt
    assert "still report it, with that facility's name and location, but do not research the other facility further" in prompt
    assert "report only the entry you used" in prompt
    assert "is still reported, with an empty values list" in prompt


def test_a_location_is_copied_never_worked_out():
    prompt = " ".join(prompt_for(THUNDER_BAY).split())
    assert "copied exactly as written" in prompt
    assert "Never work a location out" in prompt
    assert "never use the coordinates in the site context" in prompt
    assert "not the operator's office" in prompt
    assert "Leave it blank if the source gives none" in prompt
    assert 'stated_location_kind "coordinates | street address"' in prompt


def test_the_found_name_keeps_its_specificity_rule():
    prompt = prompt_for(THUNDER_BAY)
    targets = {target["attribute_name"]: target for target in json.loads(section(prompt, "Target attributes"))}
    assert "Bass Coast Shire Waste Management Facility" in targets["found_facility_name"]["guidance"]


def test_a_coordinates_only_site_is_searched_for_sources_that_say_where_it_is(tmp_path):
    bra = {"site_id": "7", "site_name": "Lixão de Exemplo", "country_iso3": "BRA", "is_location_exact": "FALSE",
           "latitude": "-10.123456", "longitude": "-48.654321", "municipality": "Palmas", "admin1": "TO"}
    assert requested_attributes(bra) == ["found_location"]
    prompt = prompt_for(bra)
    assert json.loads(section(prompt, "Target attributes")) == []
    assert "No attribute values are wanted for this site, only where it is" in prompt
    assert "Only the attributes listed below are wanted" not in prompt

    # select_sites drops a site with nothing to ask, so this one must still be selected.
    seed = tmp_path / "seed.csv"
    seed.write_text(
        "site_id,site_name,country_iso3,is_location_exact,latitude,longitude\n"
        "7,Lixão de Exemplo,BRA,FALSE,-10.123456,-48.654321\n",
        encoding="utf-8",
    )
    from waste_ai_search.input_loader import load_sites

    sites, _headers = load_sites(seed)
    config = PipelineConfig(input_csv=seed, run_dir=tmp_path / "run", run_id="t", iso3=["BRA"])
    assert [site["site_id"] for site in select_sites(sites, config)] == ["7"]


def test_the_search_cap_and_map_site_guidance_are_unchanged():
    prompt = " ".join(prompt_for(THUNDER_BAY).split())
    assert "Use at most 6 web searches in total for this site" in prompt
    assert "Never cite mapcarta.com or maptons.com" in prompt
    assert "wastebook.co.uk copies the Environment Agency register" in prompt


def test_the_prompt_stays_compact():
    """About 7% longer than the attribute-first prompt it replaced (16.5k characters for this site)."""
    assert len(prompt_for(THUNDER_BAY)) < 18_500
    assert not re.search(r"\n{4,}", prompt_for(THUNDER_BAY))
