"""The prompt asks for sources first (build step 4 of the source-matching plan), and only for
sources about this site (step 1 of the approve-sources plan).

Each source reports the facility it describes - its name, address and coordinates as the source
writes them - and the values it gives. Batch 1's rule is back: the agent decides whether a source
is about this site, returns only those, and nothing where it can't confirm it. #31's "report
sources about other facilities too, and our code decides" is reversed. The name and location a
source gives are only used to check the match, so neither is asked for as a value. Our coordinates
go in rounded to about 1 km, so there is nothing worth copying.
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
        "facility_name_in_source", "facility_name_english", "stated_address", "stated_coordinates",
            "stated_municipality", "stated_state", "values",
    ]
    assert list(source["values"][0]) == [
        "attribute_name", "value", "unit", "value_basis", "confidence_score", "value_date",
        "evidence_summary", "search_terms_used", "quoted_evidence_short", "planned", "not_municipal",
    ]
    assert "stated_location" not in prompt_for(THUNDER_BAY)


def test_value_date_is_the_date_the_value_is_true_for():
    """A yearly value loads under the year its value_date starts with. In the 31 October Mexico
    tests, 20,075 t a year from a 2022-2024 municipal plan was dated 2024-06-30, the article's date,
    and a gas project's Planned status 2015, its target year, from a 2014 page."""
    (source,) = contract(prompt_for(THUNDER_BAY))["sources"]
    assert source["values"][0]["value_date"] == (
        "The date the value is true for (a figure's own year, never a target year; else the source's date): "
        "YYYY-MM-DD, YYYY-MM, or YYYY"
    )


def test_our_coordinates_are_rounded_to_about_a_kilometre_with_the_place_names():
    prompt = prompt_for(THUNDER_BAY)
    context = json.loads(section(prompt, "Site context"))
    assert (context["latitude"], context["longitude"]) == (48.84, -87.43)
    assert (context["municipality"], context["admin1"], context["admin2"]) == ("Thunder Bay", "Ontario", "Thunder Bay District")
    assert "48.8433" not in prompt and "87.4296" not in prompt
    # A reverse-geocoded street address would leak the point just as well.
    assert "1 Landfill Road" not in prompt


def test_neither_the_name_nor_the_location_is_asked_for_as_a_value():
    prompt = prompt_for(THUNDER_BAY)
    assert "found_latitude" not in prompt and "found_longitude" not in prompt
    targets = {target["attribute_name"] for target in json.loads(section(prompt, "Target attributes"))}
    assert not targets & {"found_location", "found_facility_name"}
    assert "found_facility_name" not in prompt
    assert "Where this facility is matters too" not in prompt
    for provenance in ("", "found_facility_name@lmop_2024", "found_latitude@eprtr_2022; found_longitude@eprtr_2022"):
        attributes = requested_attributes({**THUNDER_BAY, "attribute_sources": provenance})
        assert not set(attributes) & {"found_location", "found_facility_name"}, provenance


def test_a_page_about_the_town_or_another_of_its_sites_is_not_about_this_one():
    """In test10-mex-sample2 the first search gave Las Varas pages about Madera's new landfill,
    which none named Las Varas, and the second declined them (Hugh, 30 October)."""
    prompt = " ".join(prompt_for(THUNDER_BAY).split())
    assert (
        "A town can have waste sites we don't hold, such as a new landfill that replaced an old dump. A page "
        "about the town's waste or another of its sites is not about this facility unless it also describes "
        "this one: its name, place or history. From such a page use only what it says about this one. A "
        'generic phrase such as "municipal dump" or "basurero municipal" names no particular site.'
    ) in prompt
    # Thunder Bay has none of our other sites near it, so the prompt names none.
    assert "our_other_sites_nearby" not in prompt and "our_records_within_1_km" not in prompt


def test_our_other_sites_nearby_come_under_the_same_rule():
    """A page about one of them that describes this site too is still about this site."""
    nearby = ["BASURERO NICOLAS BRAVO (Madera, 18 km away)", 'unnamed site, recorded as "BASURERO MUNICIPAL" (Madera, 31 km away)']
    prompt = prompt_for({**THUNDER_BAY, "our_other_sites_nearby": nearby})
    assert json.loads(section(prompt, "Site context"))["our_other_sites_nearby"] == nearby
    assert (
        "A page about the town's waste, another of its sites, or one of our_other_sites_nearby (our other sites "
        "in this municipality or within 15 km) is not about this facility unless it also describes this one"
    ) in " ".join(prompt.split())


def test_our_records_within_1_km_may_be_this_site_recorded_twice():
    twins = ["BASURERO SAN JUAN DEL RIO (San Juan del Río, under 1 km away)"]
    prompt = prompt_for({**THUNDER_BAY, "our_records_within_1_km": twins})
    assert json.loads(section(prompt, "Site context"))["our_records_within_1_km"] == twins
    assert (
        "our_records_within_1_km may be this facility recorded twice: a page about one of them may be about "
        "this one."
    ) in " ".join(prompt.split())
    assert "our_other_sites_nearby" not in prompt


def test_the_agent_returns_only_sources_about_this_site_as_in_batch_1():
    prompt = " ".join(prompt_for(THUNDER_BAY).split())
    assert "Identity first: - Return only sources about THIS facility." in prompt
    assert "Use the names, approximate coordinates, municipality and admin names in the site context to check that each source describes it." in prompt
    assert (
        "If you cannot confirm you found this specific facility, return an empty sources list and explain why in "
        "search_notes. Do not return data for a different site."
    ) in prompt
    assert "From a page listing many facilities, such as a registry, use only this facility's entry." in prompt
    # #31's rule, reversed.
    assert "Our code decides" not in prompt
    assert "still report it" not in prompt
    assert "with an empty values list" not in prompt


def test_each_source_states_the_facilitys_name_address_and_coordinates_as_written():
    prompt = " ".join(prompt_for(THUNDER_BAY).split())
    assert (
        "Report each source you use once, with the facility as that source describes it: facility_name_in_source "
        "and facility_name_english, stated_address and stated_coordinates, stated_municipality and stated_state."
    ) in prompt
    assert "stated_address is the site's own address, copied exactly as written: never the operator's office." in prompt
    assert "stated_coordinates is the site's coordinates or grid reference, copied exactly as written." in prompt
    assert "Give both fields where the source gives both." in prompt
    assert "Never work out an address or coordinates" in prompt
    # The town and state are checked against ours, so a town's state may be given.
    assert "for a town alone, the state it is in" in prompt
    assert "A source in another state than admin1 is very likely about a different facility." in prompt
    assert "never use the coordinates in the site context" in prompt
    assert "leave a field blank if the source gives none" in prompt
    # Optional in the plan: on gpt-5.4, 5 of 51 pages gave an address the agent didn't copy.
    assert "A page often gives it in a contact box or footer, so look there too." in prompt


def test_a_trusted_name_or_location_is_called_confirmed_and_only_then():
    trusted = "found_facility_name@lmop_2024; found_latitude@eprtr_2022; found_longitude@eprtr_2022"
    cases = {
        trusted: "The name and coordinates are already confirmed by an authoritative source.",
        "found_facility_name@lmop_2024": "The name is already confirmed by an authoritative source.",
        "found_latitude@eprtr_2022; found_longitude@eprtr_2022": "The coordinates are already confirmed by an authoritative source.",
    }
    for provenance, said in cases.items():
        assert said in " ".join(prompt_for({**THUNDER_BAY, "attribute_sources": provenance}).split()), provenance
    assert "already confirmed" not in prompt_for(THUNDER_BAY)
    # Coordinates flagged inexact are never called confirmed, whoever supplied them.
    inexact = {**THUNDER_BAY, "attribute_sources": trusted, "is_location_exact": "FALSE"}
    assert "The name is already confirmed" in " ".join(prompt_for(inexact).split())


def test_a_coordinates_only_site_has_nothing_to_ask_and_is_not_searched(tmp_path):
    """Changed on purpose for step 1 of the approve-sources plan. Brazil was searched only for
    sources stating where an inexact site is, and a location is no longer asked for, so a
    coordinates-only search would load nothing."""
    bra = {"site_id": "7", "site_name": "Lixão de Exemplo", "country_iso3": "BRA", "is_location_exact": "FALSE",
           "latitude": "-10.123456", "longitude": "-48.654321", "municipality": "Palmas", "admin1": "TO"}
    assert requested_attributes(bra) == []

    seed = tmp_path / "seed.csv"
    seed.write_text(
        "site_id,site_name,country_iso3,is_location_exact,latitude,longitude\n"
        "7,Lixão de Exemplo,BRA,FALSE,-10.123456,-48.654321\n",
        encoding="utf-8",
    )
    from waste_ai_search.input_loader import load_sites

    sites, _headers = load_sites(seed)
    config = PipelineConfig(input_csv=seed, run_dir=tmp_path / "run", run_id="t", iso3=["BRA"])
    assert select_sites(sites, config) == []


def test_the_search_cap_and_map_site_guidance_are_unchanged():
    prompt = " ".join(prompt_for(THUNDER_BAY).split())
    assert "Use at most 6 web searches in total for this site" in prompt
    assert "Never cite mapcarta.com or maptons.com" in prompt
    assert "wastebook.co.uk copies the Environment Agency register" in prompt


def test_only_actual_figures_dates_and_features_are_asked_for():
    """In the 10-site tests most wrong values from the right facility were planned, permitted or
    design figures read as actual - a licensed 217,000 t/yr at a site that never took waste - and
    permit dates read as opening years. In the 31 October Mexico tests, features a regulator ordered
    or a proposed project described were read as the site's own: Sanitary Landfill from monitoring
    wells Profepa ordered, a flare from a proposed CDM project's design."""
    prompt = " ".join(prompt_for(THUNDER_BAY).split())
    assert (
        "Return only actual figures, dates and features: what the facility is, has, did or does. Leave out "
        "planned, proposed, ordered or required, permitted or licensed, design and projected ones"
    ) in prompt
    assert "a permitted capacity or annual limit is not waste received" in prompt
    assert "A gas project's status may still be Planned or Under Construction" in prompt
    assert "closed or is expected to close" not in prompt
    assert "Not the date of a permit or licence, or of a later stage, cell or extension opening" in prompt
    assert "not a permitted or licensed limit or a design capacity" in prompt


def test_one_cells_area_or_cover_is_not_the_whole_sites():
    """In the 31 October Mexico tests, 6,000 m2 of a new cell's geomembrane was given as the site's
    area, and tepetate used only on a COVID clinical-waste cell as its cover. Figures and cover
    only: Sanitary Landfill read from the cell in use was judged supported 4 times."""
    targets = {target["attribute_name"]: target["guidance"] for target in json.loads(section(prompt_for(THUNDER_BAY), "Target attributes"))}
    assert targets["area_square_meters"].endswith(
        "This is the waste footprint, not the whole property or parcel, nor one cell or extension."
    )
    assert targets["cover_types"].endswith("other soil mixture. The cover used across the site, not one cell's.")
    assert "one cell" not in targets["facility_type"]


def test_a_planned_closing_year_is_asked_for_and_marked():
    """Hugh keeps expected closures such as Moose Jaw's 2028 (key-values plan): the agent returns
    them marked planned, and SMEs check them from a Tier 3-5 source. No other future year."""
    prompt = " ".join(prompt_for(THUNDER_BAY).split())
    assert "Never return a future year, except a planned closing_year (planned = true)" in prompt
    assert "return that year with planned = true; an actual closure is planned = false" in prompt
    assert "Not a planned or expected closure" not in prompt


def test_a_closure_that_has_not_happened_or_did_not_last_does_not_count():
    """In the 31 October Mexico tests, 15 statuses and closing years took a closure that had not
    happened, or did not last, as done: Inactive from a closure programme Profepa approved, 2024
    from "Para septiembre de 2024 quedará clausurado" with planned not set, 2006 from a replacement
    landfill's opening, a 'clausura temporal' with 15 days to comply, 2018 from the article's date."""
    prompt = " ".join(prompt_for(THUNDER_BAY).split())
    assert (
        "- A closure that is only ordered, recommended, planned, funded or expected, is temporary (clausura "
        "temporal) or later reversed, or is a replacement site's opening does not make the site Inactive or give "
        "an actual closing_year. closing_year is a year a source gives for the closure, never the article's own "
        "date."
    ) in prompt
    assert "a council resolution, or a closure order that was carried out." in prompt
    # Still a push to look for the year, but no longer for a year no source gives: 8 of the 13
    # closing years flagged were worked out with none stated.
    assert "A closed site's answer is incomplete until you have looked for its closure year." in prompt
    assert "A closed site without a closure year is an incomplete answer" not in prompt
    assert "Status and closure" not in prompt_for(THUNDER_BAY, ["opening_year"])


def test_the_prompt_stays_compact():
    """About as long as the attribute-first prompt it replaced (16.5k characters for this site).
    Raised from 18,500 to 19,000 on 30 October 2026 for the rule on a town's other sites, and from
    19,000 to 20,700 on 1 November 2026 for the rules added before batch 3. Thunder Bay has none of
    our sites near it: where a site has, the first named adds about 175 characters and each further
    one about 65, so ten add about 830."""
    assert len(prompt_for(THUNDER_BAY)) < 20_700
    assert not re.search(r"\n{4,}", prompt_for(THUNDER_BAY))


def test_status_and_closure_is_asked_only_where_status_or_the_closing_year_is_wanted():
    # The gas follow-up asks for the gas attributes alone.
    assert "Status and closure" not in prompt_for(THUNDER_BAY, ["has_flare", "flare_efficiency"])
    known = {**THUNDER_BAY, "facility_status": "Active", "closing_year": "2031"}
    assert "facility_status" not in requested_attributes(known) and "Status and closure" not in prompt_for(known)
    for asked in (["facility_status"], ["closing_year"]):
        assert "return\n  facility_status = Inactive AND search for closing_year" in prompt_for(THUNDER_BAY, asked)


def test_a_translated_name_is_one_facility_and_agreeing_on_it_alone_confirms_nothing():
    """Batch 1's wording, back with its identity rule."""
    mex = {"site_id": "1", "site_name": "Municipal Landfill of Tijuana", "original_site_name": "Relleno Sanitario de Tijuana",
           "source_language": "es", "country_iso3": "MEX", "latitude": "32.5", "longitude": "-117.0"}
    names = " ".join(section(prompt_for(mex), "Names").split())
    assert (
        "Treat them as one facility with two names, never as two candidates: agreeing on the translated name alone "
        "is not identity confirmation."
    ) in names
    assert (
        "Give each source's own name for the facility as facility_name_in_source, exactly as that source spells it, "
        "in whatever script or language it uses. Do not translate it back."
    ) in names
