"""Recovering the source's own facility name, before translation.

`consolidated_facility.facility_name` is translated, so a search on it alone looks for a string no
local source ever wrote. The clearest case in the corpus: the Mexican site 'DELICIAS' -- a city in
Chihuahua -- reaches the pipeline as 'DELICACIES'. Searching that finds nothing.

These are offline. The joins themselves are exercised by scripts/check_db_schema.py against the
live database; what is pinned here is the extraction, the differs-from-translation rule, and the
prompt wiring.
"""
from __future__ import annotations

import pytest

from waste_ai_search.prompt_builder import build_site_prompt
from waste_ai_search.source_names import (
    RAW_NAME_SOURCES,
    _osm_tag_name,
    _plain,
    attach_original_names,
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
    attach_original_names(records, {})
    assert records[0]["original_site_name"] == ""
    assert records[0]["source_language"] == ""


def test_attach_matches_on_the_integer_facility_key():
    """Ledger keys are integers; a seed record has already stringified its ids."""
    records = [{"internal_facility_id": "2210", "facility_id": "2210"}]
    attach_original_names(
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
        attributes=["found_site_name"],
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
        attributes=["found_site_name"],
    )
    assert "original_site_name" not in prompt.split("Site context:")[1]
