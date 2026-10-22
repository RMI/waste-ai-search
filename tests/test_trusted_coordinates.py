"""F29: coordinates a Tier 1-2 source already supplied are trusted.

Like the trusted name (WP-531), the rule needs POSITIVE per-attribute provenance, for both axes.
Since step 1 of the approve-sources plan no site's location is asked for: each source states where
it puts the facility, only to check that it is about this site. Trusted coordinates are called
confirmed in the prompt, and arbitration records the sources' locations for such a site rather than
queueing them. These tests were rewritten on purpose for that change.
"""
from __future__ import annotations

from waste_ai_search.arbitrate import location_reviewed
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


def test_tier_1_2_coordinates_leave_the_sources_locations_recorded_not_reviewed():
    assert trusted_baseline_coordinates(site(attribute_sources=TRUSTED))
    assert not location_reviewed(site(attribute_sources=TRUSTED))


def test_a_location_is_never_asked_for_whoever_supplied_ours():
    for provenance in ("", "found_latitude@eprtr_2022", "found_latitude@osm_2022; found_longitude@osm_2022", TRUSTED):
        assert "found_location" not in requested_attributes(site(attribute_sources=provenance)), provenance


def test_untrusted_missing_or_inexact_coordinates_have_the_sources_locations_reviewed():
    for provenance in (
        "",                                                      # none
        "found_latitude@eprtr_2022",                             # one axis only
        "found_latitude@osm_2022; found_longitude@osm_2022",     # below Tier 2
    ):
        assert location_reviewed(site(attribute_sources=provenance)), provenance
    assert not trusted_baseline_coordinates(site(attribute_sources=TRUSTED, longitude=""))
    assert location_reviewed(site(attribute_sources=TRUSTED, is_location_exact="FALSE"))


def test_a_previous_pass_is_tiered_by_its_own_source():
    ai = "found_latitude@Tier 2; found_longitude@Tier 2"
    assert trusted_baseline_coordinates(site(ai_filled_fields=ai))
    ai = "found_latitude@Tier 3; found_longitude@Tier 3"
    assert not trusted_baseline_coordinates(site(ai_filled_fields=ai, attribute_sources=TRUSTED))


def test_the_prompt_calls_trusted_name_and_coordinates_confirmed():
    s = site(attribute_sources=f"found_facility_name@eprtr_2022; {TRUSTED}")
    prompt = " ".join(build_site_prompt(s, attributes=requested_attributes(s)).split())
    assert "The name and coordinates are already confirmed by an authoritative source." in prompt
    assert '"latitude": 52.5,' in build_site_prompt(s)  # the seed location, rounded, still steers the search
    assert "stated_coordinates" in prompt  # and every source still says where it puts the facility


def test_the_gas_capture_follow_up_does_not_call_untrusted_identity_confirmed():
    """Pass 2 asks for GCCS alone. Not asked is not trusted: only provenance makes identity confirmed."""
    s = site(attribute_sources="found_latitude@osm_2022; found_longitude@osm_2022", has_landfill_gas_collection="TRUE")
    follow_up = [a for a in requested_attributes(s) if a.startswith("gccs") or "flare" in a]
    prompt = build_site_prompt(s, attributes=follow_up)
    assert "already confirmed" not in prompt
    assert "found_facility_name" not in prompt


def test_a_trusted_identity_is_still_confirmed_in_the_follow_up():
    s = site(attribute_sources=f"found_facility_name@eprtr_2022; {TRUSTED}", has_landfill_gas_collection="TRUE")
    follow_up = [a for a in requested_attributes(s) if a.startswith("gccs")]
    prompt = " ".join(build_site_prompt(s, attributes=follow_up).split())
    assert "The name and coordinates are already confirmed by an authoritative source." in prompt
