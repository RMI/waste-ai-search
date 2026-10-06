"""F29: don't re-search coordinates a Tier 1-2 source already supplied, to cut search cost.

Like the trusted name (WP-531), the rule needs POSITIVE per-attribute provenance, for both axes.
Without found coordinates the Q34 identity gate cannot run for the site - an accepted risk.
"""
from __future__ import annotations

from waste_ai_search.credibility import trusted_baseline_coordinates
from waste_ai_search.prompt_builder import build_site_prompt, requested_attributes

TRUSTED = "found_latitude@eprtr_2022; found_longitude@eprtr_2022"


def site(**overrides):
    base = {
        "site_id": "1",
        "internal_facility_id": "1",
        "site_name": "Deponie Nord",
        "country_iso3": "DEU",
        "latitude": "52.5",
        "longitude": "13.4",
        "is_location_exact": "TRUE",
        "reference_year": "2022",
    }
    base.update(overrides)
    return base


def test_tier_1_2_coordinates_are_not_searched():
    attributes = requested_attributes(site(attribute_sources=TRUSTED))
    assert "found_latitude" not in attributes
    assert "found_longitude" not in attributes


def test_coordinates_are_searched_without_trusted_provenance_for_both_axes():
    for provenance in (
        "",                                                      # none
        "found_latitude@eprtr_2022",                             # one axis only
        "found_latitude@osm_2022; found_longitude@osm_2022",     # below Tier 2
    ):
        attributes = requested_attributes(site(attribute_sources=provenance))
        assert {"found_latitude", "found_longitude"} <= set(attributes), provenance


def test_missing_or_inexact_coordinates_are_still_searched():
    assert not trusted_baseline_coordinates(site(attribute_sources=TRUSTED, longitude=""))
    attributes = requested_attributes(site(attribute_sources=TRUSTED, is_location_exact="FALSE"))
    assert "found_latitude" in attributes


def test_a_previous_pass_is_tiered_by_its_own_source():
    ai = "found_latitude@Tier 2; found_longitude@Tier 2"
    assert trusted_baseline_coordinates(site(ai_filled_fields=ai))
    ai = "found_latitude@Tier 3; found_longitude@Tier 3"
    assert not trusted_baseline_coordinates(site(ai_filled_fields=ai, attribute_sources=TRUSTED))


def test_the_prompt_asks_for_neither_name_nor_coordinates_when_both_are_trusted():
    s = site(attribute_sources=f"found_facility_name@eprtr_2022; {TRUSTED}")
    prompt = build_site_prompt(s, attributes=requested_attributes(s))
    assert "The name and coordinates are already confirmed" in prompt
    assert "found_latitude and found_longitude establish" not in prompt
    assert '"latitude": "52.5"' in prompt  # the seed location still steers the search


def test_the_prompt_asks_only_for_the_name_when_only_coordinates_are_trusted():
    s = site(attribute_sources=TRUSTED)
    prompt = build_site_prompt(s, attributes=requested_attributes(s))
    assert "found_facility_name establishes that you found the RIGHT facility" in prompt
    assert "do not return coordinates" in prompt


def test_the_gas_capture_follow_up_does_not_call_untrusted_identity_confirmed():
    """Pass 2 asks for GCCS alone, so identity is absent from the request without being trusted."""
    s = site(attribute_sources="found_latitude@osm_2022; found_longitude@osm_2022", has_landfill_gas_collection="TRUE")
    follow_up = [a for a in requested_attributes(s) if a.startswith("gccs") or "flare" in a]
    prompt = build_site_prompt(s, attributes=follow_up)
    assert "already confirmed" not in prompt
    assert "The name and coordinates are not asked for here" in prompt
    assert "Use site_name to search; do not return a facility name." in prompt


def test_a_trusted_identity_is_still_confirmed_in_the_follow_up():
    s = site(attribute_sources=f"found_facility_name@eprtr_2022; {TRUSTED}", has_landfill_gas_collection="TRUE")
    follow_up = [a for a in requested_attributes(s) if a.startswith("gccs")]
    assert "The name and coordinates are already confirmed" in build_site_prompt(s, attributes=follow_up)
