"""Recovering the source's own facility name, before translation.

`consolidated_facility.facility_name` is translated, so a search on it alone looks for a string no
local source ever wrote. The clearest case in the corpus: the Mexican site 'DELICIAS' -- a city in
Chihuahua -- reaches the pipeline as 'DELICACIES'. Searching that finds nothing.

These are offline, and they cannot see a typo in any of the five table and column names each
source spec carries -- those are plain strings, so a misspelling passes every assertion here and
then fails during seed generation. `scripts/check_db_schema.py` runs each join against the live
database for exactly that reason, and fails both when the SQL is invalid and when it is valid but
matches nothing, which is what a wrong join key looks like.

What is pinned here is the extraction, the differs-from-translation rule, and the prompt wiring.
"""
from __future__ import annotations

import pytest

from waste_ai_search.prompt_builder import build_site_prompt
from waste_ai_search.source_names import (
    RAW_NAME_SOURCES,
    _osm_tag_name,
    _plain,
    attach_name_provenance,
    source_name_sql,
)


# --- name extraction ----------------------------------------------------------------------
def test_plain_extractor_returns_the_value():
    assert _plain("Lixao Sede") == "Lixao Sede"
    assert _plain(None) == ""


def test_osm_name_is_dug_out_of_a_python_dict_repr():
    """`fixed_name` and `translated` are Python reprs with single quotes, not JSON."""
    blob = "{'landuse': 'landfill', 'name': 'Basural Municipal', '_osm_type': 'way'}"
    assert _osm_tag_name(blob) == "Basural Municipal"


def test_osm_name_is_dug_out_of_real_json():
    """`name_left` is double-quoted JSON, so both spellings have to parse."""
    blob = '{"landuse": "landfill", "name": "Basural Municipal", "_osm_type": "way"}'
    assert _osm_tag_name(blob) == "Basural Municipal"


@pytest.mark.parametrize(
    "blob",
    [
        "{'landuse': 'landfill', '_osm_type': 'way'}",  # a real tag set with no name at all
        "not a dict",
        "",
        None,
    ],
)
def test_osm_blob_without_a_usable_name_yields_blank(blob):
    """An unparseable or nameless blob must produce nothing rather than a guess."""
    assert _osm_tag_name(blob) == ""


def test_osm_blob_that_parses_to_a_non_dict_yields_blank():
    assert _osm_tag_name("[1, 2, 3]") == ""


# --- the source specs ---------------------------------------------------------------------
def test_only_translated_sources_are_mapped():
    """English-language sources have no original to recover; their stored name IS the original."""
    assert set(RAW_NAME_SOURCES) == {
        "osm_2022",
        "eprtr_2022",
        "mexico_inegi_2016",
        "sinir_2024",
    }
    for data_source in ("lmop_2024", "gpw_2021", "usa_ghgrp_2026", "manual_entry"):
        assert data_source not in RAW_NAME_SOURCES


def test_every_spec_reads_from_a_translated_raw_table():
    for spec in RAW_NAME_SOURCES.values():
        assert spec.table.startswith("raw_")
        assert spec.table.endswith("_translated")


def test_generated_sql_joins_the_ledger_to_the_raw_table():
    sql = source_name_sql(RAW_NAME_SOURCES["osm_2022"])
    assert "consolidation.value_resolution_ledger" in sql
    assert "raw_data.raw_osm_translated" in sql
    assert "r.id::text = l.data_source_facility_id" in sql
    # One ledger row per resolved column means a facility repeats without this.
    assert "SELECT DISTINCT" in sql


# --- attaching to seed records ------------------------------------------------------------
def test_attach_fills_blanks_where_no_original_was_found():
    records = [{"internal_facility_id": "1", "facility_id": "1"}]
    attach_name_provenance(records, {})
    assert records[0]["original_site_name"] == ""
    assert records[0]["source_language"] == ""


def test_attach_matches_on_the_integer_facility_key():
    """Ledger keys are integers; a seed record has already stringified its ids."""
    records = [{"internal_facility_id": "2210", "facility_id": "2210"}]
    attach_name_provenance(
        records,
        {2210: {
            "original_site_name": "Lixao Sede",
            "source_language": "pt",
            "name_data_source": "sinir_2024",
        }},
    )
    assert records[0]["original_site_name"] == "Lixao Sede"
    assert records[0]["source_language"] == "pt"
    assert records[0]["name_data_source"] == "sinir_2024"


# --- the prompt ---------------------------------------------------------------------------
def test_prompt_carries_both_names_and_says_to_search_both():
    prompt = build_site_prompt(
        {
            "site_id": "44",
            "site_name": "DELICACIES",
            "original_site_name": "DELICIAS",
            "source_language": "es",
            "country_iso3": "MEX",
        },
        attributes=["found_facility_name"],
    )
    assert '"site_name": "DELICACIES"' in prompt
    assert '"original_site_name": "DELICIAS"' in prompt
    assert '"source_language": "es"' in prompt
    assert "machine-translated" in prompt
    # Two names for one facility, not two candidate facilities.
    assert "never as two" in prompt


def test_prompt_omits_the_original_name_when_there_is_none():
    """Sites from English-language sources must not carry an empty field into the context."""
    prompt = build_site_prompt(
        {"site_id": "7", "site_name": "Greenview Landfill", "country_iso3": "USA"},
        attributes=["found_facility_name"],
    )
    assert "original_site_name" not in prompt.split("Site context:")[1]


# --- conditional names guidance (Copilot review) --------------------------------------------
def test_english_source_site_is_not_told_its_name_is_a_translation():
    """A site with no translation provenance at all: no original name and no source language.

    That is what an English-language source looks like. It is NOT the only way `original_site_name`
    comes back empty -- a translated source whose name was unchanged also has none, and is covered
    by test_translated_source_keeps_its_language_when_the_name_is_unchanged. The distinguishing
    condition here is the absence of `source_language`, not the absence of a name.
    """
    prompt = build_site_prompt(
        {"site_id": "7", "site_name": "Greenview Landfill", "country_iso3": "CAN"},
        attributes=["found_facility_name"],
    )
    assert "machine-translated" not in prompt
    assert "it has not\n  been translated" in prompt
    # Nothing should reference a field this site does not carry.
    assert "original_site_name" not in prompt


def test_translated_source_site_still_gets_the_two_name_guidance():
    prompt = build_site_prompt(
        {
            "site_id": "4",
            "site_name": "DELICACIES",
            "original_site_name": "DELICIAS",
            "source_language": "es",
            "country_iso3": "MEX",
        },
        attributes=["found_facility_name"],
    )
    assert "machine-translated" in prompt
    assert "Search THIS name in the local language first" in prompt
    assert "never as two" in prompt


def test_blank_original_name_counts_as_absent():
    """A seed CSV round-trip fills the column with "" rather than dropping it."""
    prompt = build_site_prompt(
        {
            "site_id": "9",
            "site_name": "Greenview Landfill",
            "original_site_name": "",
            "source_language": "",
            "country_iso3": "CAN",
        },
        attributes=["found_facility_name"],
    )
    assert "machine-translated" not in prompt


# --- translated source with no second spelling (Copilot review) -----------------------------
def test_translated_source_keeps_its_language_when_the_name_is_unchanged():
    """6,700 facilities come from a translated source whose name translated to itself.

    They have no second spelling, but 4,458 of them have a known non-English source language.
    Treating "no original name" as "not translated" discarded it and told them the wrong thing.
    """
    prompt = build_site_prompt(
        {
            "site_id": "2210",
            "site_name": "Lixao Municipal",
            "original_site_name": "",
            "source_language": "pt",
            "country_iso3": "BRA",
        },
        attributes=["found_facility_name"],
    )
    assert "'pt' (ISO 639-1)" in prompt
    assert "Search it in that language as well as in English" in prompt
    # It is not a translation-mismatch case, and it is not an untranslated-source case.
    assert "machine-translated" not in prompt
    assert "it has not\n  been translated" not in prompt


def test_english_language_translated_source_is_treated_as_untranslated():
    """2,242 matched rows carry language 'en'. There is nothing to translate and no other
    language to search, so they get the plain guidance."""
    prompt = build_site_prompt(
        {
            "site_id": "5",
            "site_name": "City Landfill",
            "original_site_name": "",
            "source_language": "en",
            "country_iso3": "GBR",
        },
        attributes=["found_facility_name"],
    )
    assert "it has not\n  been translated" in prompt
    assert "ISO 639-1" not in prompt


def test_provenance_records_language_even_without_a_second_name():
    """The seed must carry source_language independently of original_site_name."""
    records = [{"internal_facility_id": "2210", "facility_id": "2210"}]
    attach_name_provenance(
        records,
        {2210: {
            "original_site_name": "",
            "source_language": "pt",
            "name_data_source": "sinir_2024",
        }},
    )
    assert records[0]["original_site_name"] == ""
    assert records[0]["source_language"] == "pt"
    assert records[0]["name_data_source"] == "sinir_2024"


# --- load_name_provenance itself (Copilot review) -------------------------------------------
def _run_load(monkeypatch, rows_by_source):
    """Drive load_name_provenance with canned ledger/raw join rows instead of a database."""
    import waste_ai_search.db as db_module
    from waste_ai_search.source_names import load_name_provenance

    def fake_fetch_all(sql, params=None, config=None):
        # params is (column_name, data_source); the second selects which source's rows to serve.
        return rows_by_source.get(params[1], [])

    monkeypatch.setattr(db_module, "fetch_all", fake_fetch_all)
    return load_name_provenance()


def test_load_emits_a_second_name_when_the_spelling_differs(monkeypatch):
    found = _run_load(monkeypatch, {
        "eprtr_2022": [{
            "internal_facility_id": 17924,
            "original": "Burgenlandischer Mullverband",
            "translated": "Burgenland Waste Association",
            "language": "de",
        }],
    })
    assert found[17924]["original_site_name"] == "Burgenlandischer Mullverband"
    assert found[17924]["source_language"] == "de"
    assert found[17924]["name_data_source"] == "eprtr_2022"


def test_load_treats_a_case_only_difference_as_unchanged(monkeypatch):
    """Casefold comparison: 'LA BOCANA' and 'La Bocana' are one name, not two."""
    found = _run_load(monkeypatch, {
        "eprtr_2022": [{
            "internal_facility_id": 43,
            "original": "LA BOCANA",
            "translated": "La Bocana",
            "language": "es",
        }],
    })
    assert found[43]["original_site_name"] == ""
    # The language survives even though no second name is emitted -- the whole point of the fix.
    assert found[43]["source_language"] == "es"


def test_load_keeps_language_for_an_unchanged_non_english_name(monkeypatch):
    """4,458 facilities in the corpus look like this: translated source, identical spelling."""
    found = _run_load(monkeypatch, {
        "sinir_2024": [{
            "internal_facility_id": 2210,
            "original": "Dourados engenharia ambienta ltda",
            "translated": "Dourados engenharia ambienta ltda",
            "language": "pt",
        }],
    })
    assert found[2210] == {
        "original_site_name": "",
        "source_language": "pt",
        "name_data_source": "sinir_2024",
    }


def test_load_digs_the_name_out_of_an_osm_blob(monkeypatch):
    found = _run_load(monkeypatch, {
        "osm_2022": [{
            "internal_facility_id": 30,
            "original": "{'landuse': 'landfill', 'name': 'Basural Municipal'}",
            "translated": "{'landuse': 'landfill', 'name': 'Municipal Garbage Dump'}",
            "language": "es",
        }],
    })
    assert found[30]["original_site_name"] == "Basural Municipal"


def test_load_records_an_unnameable_osm_row_as_language_only(monkeypatch):
    """A tag blob with no `name` yields no spelling, but the source language is still known."""
    found = _run_load(monkeypatch, {
        "osm_2022": [{
            "internal_facility_id": 29,
            "original": "{'landuse': 'landfill'}",
            "translated": "{'landuse': 'landfill'}",
            "language": "es",
        }],
    })
    assert found[29]["original_site_name"] == ""
    assert found[29]["source_language"] == "es"


def test_load_keeps_the_first_source_when_a_facility_matches_two(monkeypatch):
    """A facility can join more than one raw table; the first source wins, deterministically."""
    found = _run_load(monkeypatch, {
        "osm_2022": [{
            "internal_facility_id": 77,
            "original": "{'name': 'Basural'}",
            "translated": "{'name': 'Garbage dump'}",
            "language": "es",
        }],
        "eprtr_2022": [{
            "internal_facility_id": 77,
            "original": "Something Else",
            "translated": "Other",
            "language": "de",
        }],
    })
    assert found[77]["name_data_source"] == "osm_2022"
    assert found[77]["original_site_name"] == "Basural"


# --- SINIR municipality (WP-543) ----------------------------------------------------------------
def test_sinir_municipality_reaches_the_seed_and_the_prompt(monkeypatch):
    """Site 1116 is "Aterro Sanitário" in Araguari; without the town the agent searched Goiânia."""
    import waste_ai_search.db as db_module
    from waste_ai_search.prompt_builder import build_site_prompt
    from waste_ai_search.source_names import attach_municipalities, load_sinir_municipalities

    rows = [{"internal_facility_id": 1116, "city_name": "Araguari", "state_name": "Minas Gerais"}]
    monkeypatch.setattr(db_module, "fetch_all", lambda sql, params=None, config=None: rows)
    records = [
        {"internal_facility_id": "1116", "site_id": "1116", "site_name": "Sanitary ware",
         "original_site_name": "Atero Sanitário", "country_iso3": "BRA", "municipality": "", "admin1": ""},
        {"internal_facility_id": "9", "site_id": "9", "site_name": "Other", "country_iso3": "BRA",
         "municipality": "", "admin1": ""},
    ]
    attach_municipalities(records, load_sinir_municipalities())

    assert (records[0]["municipality"], records[0]["admin1"]) == ("Araguari", "Minas Gerais")
    assert (records[1]["municipality"], records[1]["admin1"]) == ("", "")
    prompt = build_site_prompt(records[0], ["found_latitude", "found_longitude"])
    assert '"municipality": "Araguari"' in prompt and '"admin1": "Minas Gerais"' in prompt
