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

from waste_ai_search.prompt_builder import build_site_prompt, requested_attributes
from waste_ai_search.schema import NOT_A_WASTE_FACILITY


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


def test_corroborated_sites_still_see_exactly_the_four_database_values():
    text = guidance(corroborated_site())
    assert text.startswith(
        "Allowed values only: Sanitary Landfill, Controlled Dumpsite, Dumpsite, Incineration Facility."
    )
    assert NOT_A_WASTE_FACILITY not in text
