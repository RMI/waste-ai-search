"""owner is asked of every site, at Carbon Mapper's request (13 November 2026), and is compared and
doubted as operator is."""
from __future__ import annotations

from waste_ai_search.arbitration import same_value
from waste_ai_search.found_values import needs_value_check
from waste_ai_search.prompt_builder import build_site_prompt, requested_attributes


def test_it_is_asked_of_every_site_that_has_none():
    """No database column holds it, so the seed is always blank and every site is asked."""
    assert "owner" in requested_attributes({"site_id": "1"})
    assert "owner" not in requested_attributes({"site_id": "1", "owner": "Municipio de Mérida"})


def test_the_prompt_keeps_the_owner_apart_from_the_operator():
    prompt = " ".join(build_site_prompt({"site_id": "1", "site_name": "Basurero"}, ["owner"]).split())
    assert "The organisation the source says owns the facility" in prompt
    assert "Not the operator unless the source says it owns the site too" in prompt
    assert "give the owner for the source's value_date" in prompt


def test_two_spellings_of_one_owner_agree():
    assert same_value("PRO-FAJ Hidro Limpieza, SA de CV", "PRO-FAJ Hidro Limpieza S.A. de C.V.", "owner")
    assert not same_value("Municipio de Mérida", "Gobierno del Estado de Yucatán", "owner")


def test_an_owner_the_agent_inferred_is_checked_on_its_own_row_whatever_the_tier():
    """As for an operator: an owner inferred from a "municipal" dump may be wrong under a Tier 1 page."""
    assert needs_value_check("owner", 1, "Inferred")
    assert needs_value_check("owner", 1, "Conflicting")
    assert not needs_value_check("owner", 1, "Direct")
    assert not needs_value_check("owner", 3, "Direct")
