"""WP-531 item 5: don't re-search a facility_name a Tier 1-2 source already supplied.

This reverses Q30 - "identity is always requested" - for those facilities only. Their identity
confirmation rests on coordinates, which are still asked of every facility; the Q34 identity gate
uses only coordinates, so it is unaffected.

The rule needs POSITIVE per-attribute provenance. With none, the name is still searched: the
facility-wide composite takes the best tier of everything a facility draws on, and would credit an
OSM name with a regulator's tier.
"""
from __future__ import annotations

import pytest

from waste_ai_search.credibility import trusted_baseline_name
from waste_ai_search.prompt_builder import build_site_prompt, requested_attributes


def site(**overrides):
    base = {
        "site_id": "1",
        "internal_facility_id": "1",
        "site_name": "Olushosun Landfill",
        "country_iso3": "NGA",
        "contributing_data_sources": "lmop_2024+osm_2022",
        "reference_year": "2022",
    }
    base.update(overrides)
    return base


@pytest.mark.parametrize(
    "provenance",
    [
        "found_facility_name@lmop_2024",        # Tier 1
        "found_facility_name@eprtr_2022",       # Tier 1
        "found_facility_name@sinir_2024",       # Tier 1
        "found_facility_name@mexico_inegi_2016",  # Tier 1
    ],
)
def test_a_tier_1_or_2_name_is_not_searched(provenance):
    s = site(attribute_sources=provenance)
    assert trusted_baseline_name(s)
    assert "found_facility_name" not in requested_attributes(s)


@pytest.mark.parametrize(
    "provenance",
    [
        "found_facility_name@waste_atlas_landfills_2013",  # Tier 3
        "found_facility_name@osm_2022",                    # Tier 4
        "found_facility_name@gpw_2021",                    # Tier 4
    ],
)
def test_a_lower_tier_name_is_still_searched(provenance):
    s = site(attribute_sources=provenance)
    assert not trusted_baseline_name(s)
    assert "found_facility_name" in requested_attributes(s)


def test_no_per_attribute_provenance_means_the_name_is_still_searched():
    """The composite here includes lmop_2024 (Tier 1) but says nothing about who supplied the name."""
    s = site(attribute_sources="facility_status@lmop_2024")
    assert not trusted_baseline_name(s)
    assert "found_facility_name" in requested_attributes(s)


def test_a_blank_name_is_never_trusted():
    assert not trusted_baseline_name(site(site_name="", attribute_sources="found_facility_name@lmop_2024"))


def test_a_name_a_previous_pass_supplied_is_tiered_by_its_own_source():
    """ai_filled_fields outranks the ledger: it names the source that actually supplied the value."""
    assert trusted_baseline_name(site(ai_filled_fields="found_facility_name@Tier 2",
                                      attribute_sources="found_facility_name@osm_2022"))
    assert not trusted_baseline_name(site(ai_filled_fields="found_facility_name@Tier 3",
                                          attribute_sources="found_facility_name@lmop_2024"))


def test_provenance_written_before_the_rename_still_counts():
    """A pinned seed from before WP-531 says found_site_name@...; the alias carries it."""
    assert trusted_baseline_name(site(attribute_sources="found_site_name@lmop_2024"))


def test_coordinates_are_still_requested_when_the_name_is_skipped():
    attributes = requested_attributes(site(attribute_sources="found_facility_name@lmop_2024"))
    assert "found_latitude" in attributes
    assert "found_longitude" in attributes


def test_a_trusted_name_prompt_does_not_ask_for_a_name():
    s = site(attribute_sources="found_facility_name@lmop_2024")
    prompt = build_site_prompt(s, attributes=requested_attributes(s))

    assert "Return found_facility_name" not in prompt
    assert "found_facility_name, found_latitude" not in prompt
    assert "found_latitude and found_longitude establish that you found the RIGHT facility" in prompt
    assert "The name is already confirmed." in prompt


def test_an_untrusted_name_prompt_is_unchanged():
    s = site(attribute_sources="found_facility_name@osm_2022")
    prompt = build_site_prompt(s, attributes=requested_attributes(s))

    assert "Return found_facility_name exactly as your source spells it" in prompt
    assert "found_facility_name, found_latitude and found_longitude establish" in prompt


def test_arbitration_records_no_name_row_for_a_skipped_name():
    """No request means no 'Not found' row: arbitration resolves only what was asked or returned."""
    from datetime import date

    from waste_ai_search.arbitrate import extract_evidence
    from waste_ai_search.arbitration import resolve

    s = site(attribute_sources="found_facility_name@lmop_2024")
    by_attr, _e, _s, _w = extract_evidence(s, {"site_id": "1", "attributes": []}, "r", "v", date(2026, 1, 1))
    names = list(requested_attributes(s)) + [n for n in by_attr if n not in requested_attributes(s)]
    resolved = [resolve(s, n, by_attr.get(n, [])) for n in names]

    assert "found_facility_name" not in {r["attribute_name"] for r in resolved}
