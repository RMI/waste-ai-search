"""WP-531 item 5: a facility_name a Tier 1-2 source already supplied is trusted.

Since step 1 of the approve-sources plan no site's name is asked for as a value: each source gives
its own name for the facility, only to check that it is about this site. A trusted name is called
confirmed in the prompt, and arbitration records the names an older response returned for such a
site rather than queueing them. These tests were rewritten on purpose for that change.

The rule needs POSITIVE per-attribute provenance. With none, the name is not trusted: the
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
def test_a_tier_1_or_2_name_is_trusted(provenance):
    s = site(attribute_sources=provenance)
    assert trusted_baseline_name(s)
    assert "found_facility_name" not in requested_attributes(s)
    assert "The name is already confirmed by an authoritative source." in " ".join(build_site_prompt(s).split())


@pytest.mark.parametrize(
    "provenance",
    [
        "found_facility_name@waste_atlas_landfills_2013",  # Tier 3
        "found_facility_name@osm_2022",                    # Tier 4
        "found_facility_name@gpw_2021",                    # Tier 4
    ],
)
def test_a_lower_tier_name_is_not_trusted_nor_asked_for(provenance):
    s = site(attribute_sources=provenance)
    assert not trusted_baseline_name(s)
    assert "found_facility_name" not in requested_attributes(s)
    assert "already confirmed" not in build_site_prompt(s)


def test_no_per_attribute_provenance_means_the_name_is_not_trusted():
    """The composite here includes lmop_2024 (Tier 1) but says nothing about who supplied the name."""
    s = site(attribute_sources="facility_status@lmop_2024")
    assert not trusted_baseline_name(s)
    assert "already confirmed" not in build_site_prompt(s)


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


def test_no_prompt_asks_for_a_name_but_every_source_gives_its_own():
    """Changed on purpose for step 1 of the approve-sources plan: the name is never a value asked
    for. Each source still gives its own name for the facility, to check the match."""
    for provenance in ("found_facility_name@lmop_2024", "found_facility_name@osm_2022"):
        s = site(attribute_sources=provenance)
        prompt = build_site_prompt(s, attributes=requested_attributes(s))
        assert "found_facility_name" not in prompt
        assert "Give each source's own name for the facility as facility_name_in_source" in " ".join(prompt.split())


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

