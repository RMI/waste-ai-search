"""WP-531 item 3: define each facility_type, and classify from engineering, never from the name.

Prompted by pilot site 14146, "DIPOSIT CONTROLAT DE MANRESA (II)". The agent read Catalan "dipòsit
controlat" literally as Controlled Dumpsite; the site is a sanitary landfill.

These tests pin what the prompt says. They cannot prove the agent now classifies 14146 correctly -
that takes a live call - but they do prove every facility asked for a type receives the
definitions, including the 8,564 unconfirmed sites whose facility_type guidance is assembled on a
separate path since WP-525.
"""
from __future__ import annotations

import json

import pytest

from waste_ai_search.prompt_builder import build_site_prompt, requested_attributes
from waste_ai_search.schema import FACILITY_TYPE_OFFERED, NOT_A_WASTE_FACILITY


def guidance(site):
    prompt = build_site_prompt(site, attributes=requested_attributes(site))
    targets = json.loads(prompt.split("Target attributes:")[1].split("Site context:")[0])
    return next(t["guidance"] for t in targets if t["attribute_name"] == "facility_type")


def corroborated_site(**overrides):
    base = {
        "site_id": "14146",
        "internal_facility_id": "14146",
        "site_name": "DIPOSIT CONTROLAT DE MANRESA (II)",
        "country_iso3": "ESP",
        "contributing_data_sources": "eprtr_2022",
        "reference_year": "2022",
    }
    base.update(overrides)
    return base


def unconfirmed_site():
    return corroborated_site(site_id="9", contributing_data_sources="osm_2022")


def test_each_type_is_defined_by_its_enum_name():
    text = guidance(corroborated_site())
    assert "Dumpsite - an open dumpsite: an unmanaged area" in text
    assert "Controlled Dumpsite - a dumpsite with some operational measures" in text
    assert "Sanitary Landfill - a fully engineered facility with liners" in text


def test_the_definitions_carry_the_distinguishing_features():
    text = guidance(corroborated_site())
    assert "without liners or cover systems" in text            # open dumpsite
    assert "compaction, limited soil cover, or restricted access" in text  # controlled
    assert "leachate and groundwater management" in text       # sanitary
    assert "often but not necessarily a landfill gas management system" in text


def test_the_name_is_ruled_out_as_evidence():
    """The instruction that would have stopped the 14146 error."""
    text = guidance(corroborated_site())
    assert "NEVER from words in its name" in text
    assert "'dipòsit controlat'" in text
    assert "'controlled' in a name is not evidence of a Controlled Dumpsite" in text


def test_the_14146_site_itself_receives_the_rule():
    """The facility that prompted this, seeded as it was: the guidance it gets must forbid reading
    its own name as its type."""
    site = corroborated_site()
    assert "facility_type" in requested_attributes(site)
    assert "NEVER from words in its name" in guidance(site)


def test_unconfirmed_sites_get_the_definitions_too():
    """Since WP-525 their facility_type guidance is built on a separate path; it must not skip these."""
    text = guidance(unconfirmed_site())
    assert "Sanitary Landfill - a fully engineered facility with liners" in text
    assert "NEVER from words in its name" in text


def test_the_not_a_waste_facility_guidance_sits_alongside_the_definitions():
    """WP-525's addendum is kept, not replaced, and comes after the definitions."""
    text = guidance(unconfirmed_site())
    assert NOT_A_WASTE_FACILITY in text
    assert "IS STILL A WASTE DISPOSAL" in text
    assert text.index("Definitions:") < text.index("IS STILL A WASTE DISPOSAL")


def test_corroborated_sites_see_the_database_values_and_recycling_center():
    text = guidance(corroborated_site())
    assert text.startswith(f"Allowed values only: {', '.join(FACILITY_TYPE_OFFERED)}.")
    assert {"Transfer Station", "Recycling Center"} <= set(FACILITY_TYPE_OFFERED)
    assert NOT_A_WASTE_FACILITY not in text


def test_a_transfer_station_on_a_closed_landfill_is_still_the_landfill():
    """Buried waste still emits, so the disposal history decides the type, not today's use."""
    text = guidance(corroborated_site())
    assert "Transfer Station - a site where collected waste is unloaded" in text
    assert "built on a closed landfill - classify the disposal site" in text


# --- after the live check on Manresa (18133) --------------------------------------------------
def test_the_definitions_say_what_does_not_distinguish_the_types():
    """The agent found compaction and downgraded an engineered landfill to Controlled Dumpsite."""
    text = guidance(corroborated_site())
    assert "Compaction, soil cover and restricted access occur at BOTH" in text
    assert "What decides it is engineered containment" in text


def test_an_eu_site_gets_the_landfill_directive_prior():
    text = guidance(corroborated_site())  # ESP
    assert "EU Landfill Directive (1999/31/EC)" in text
    assert "is therefore a Sanitary Landfill unless a source says it lacks that engineering" in text


def test_the_directive_prior_carries_its_2009_limit():
    """Existing landfills had until July 2009 to comply or close; an older closed site may never
    have been engineered, so the prior must not be applied to it blindly."""
    text = guidance(corroborated_site())
    assert "accepted waste after July 2009" in text
    assert "closed before July 2009 may predate these requirements" in text


@pytest.mark.parametrize("iso3", ["NOR", "ISL", "LIE"])
def test_eea_states_are_bound_too(iso3):
    assert "EU Landfill Directive" in guidance(corroborated_site(country_iso3=iso3))


@pytest.mark.parametrize("iso3", ["NGA", "MEX", "USA", "GBR", "CHE", "SRB"])
def test_sites_outside_the_directive_do_not_get_the_prior(iso3):
    """GBR applies equivalent rules through retained law but is no longer bound by the Directive;
    CHE and SRB report to E-PRTR without being bound by it."""
    assert "EU Landfill Directive" not in guidance(corroborated_site(country_iso3=iso3))


def test_an_unconfirmed_eu_site_gets_the_prior_and_the_not_a_waste_addendum():
    text = guidance(unconfirmed_site())  # ESP, osm-only
    assert "EU Landfill Directive" in text
    assert NOT_A_WASTE_FACILITY in text


def test_a_combined_transfer_and_recycling_site_is_typed_by_its_mixed_waste():
    """Site 2529 is a "Waste Transfer & Recycling Centre"; the guidance says which type wins."""
    text = guidance(corroborated_site())
    assert "both a transfer station and a recycling centre is a Transfer Station if it takes mixed" in text


def test_the_prompt_prefers_newer_sources_especially_for_status():
    """A 2010 permit saying Active must not outweigh a 2023 closure notice."""
    from waste_ai_search.prompt_builder import ATTRIBUTE_GUIDANCE, build_site_prompt, requested_attributes

    assert "base it on the most recent evidence you can find" in ATTRIBUTE_GUIDANCE["facility_status"]
    site = corroborated_site()
    prompt = build_site_prompt(site, requested_attributes(site))
    assert "- Prefer the most recent source." in prompt
