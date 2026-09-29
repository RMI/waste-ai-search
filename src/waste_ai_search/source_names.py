"""Recover each facility's name as its source originally wrote it.

`consolidated_facility.facility_name` has been through translation, so a Brazilian site reaches
the search as "Headquarters dump" rather than "Lixao Sede". That is the wrong string to hand a web
search: local coverage, permits and news use the original. The translation is still worth keeping
-- it is what makes the site legible to an English-language reviewer -- so both are passed to the
agent and it is told to search both.

The route is `consolidation.value_resolution_ledger`, which records which source supplied each
facility's name and under what id, joined back to that source's `raw_data.raw_*_translated` table.
Four of the eleven sources were translated; the rest are English-language and have no original to
recover, which is why coverage tops out at roughly three quarters of the corpus.
"""
from __future__ import annotations

import ast
import json
from dataclasses import dataclass
from typing import Any, Callable

from .schema import normalize_scalar


LEDGER_NAME_COLUMN = "facility_name"


def _plain(value: Any) -> str:
    """The name is the column value."""
    return normalize_scalar(value)


def _osm_tag_name(value: Any) -> str:
    """OSM stores a blob of tags rather than a bare name, so dig `name` out of it.

    The blob arrives in two spellings: `name_left` is real JSON, while `fixed_name` and
    `translated` are Python dict reprs with single quotes. Both are parsed, and anything that
    parses to neither is skipped rather than guessed at.
    """
    text = normalize_scalar(value)
    if not text:
        return ""
    for parse in (json.loads, ast.literal_eval):
        try:
            parsed = parse(text)
        except (ValueError, SyntaxError):
            continue
        if isinstance(parsed, dict):
            return normalize_scalar(parsed.get("name"))
    return ""


@dataclass(frozen=True)
class RawNameSource:
    """How to reach one data source's untranslated facility name."""

    table: str
    # SQL expression joined against value_resolution_ledger.data_source_facility_id, which is text.
    key_expression: str
    original_column: str
    translated_column: str = "translated"
    language_column: str = "language"
    extract: Callable[[Any], str] = _plain


# Only sources that went through translation appear here. lmop, gpw, waste_atlas, canada_ghgrp,
# usa_ghgrp and manual_entry are English-language, so their stored name IS the original.
RAW_NAME_SOURCES: dict[str, RawNameSource] = {
    "osm_2022": RawNameSource(
        table="raw_osm_translated",
        key_expression="r.id::text",
        original_column="fixed_name",
        extract=_osm_tag_name,
    ),
    "eprtr_2022": RawNameSource(
        table="raw_eprtr_translated",
        key_expression="r.facilityinspireid",
        original_column="facilityname",
    ),
    "mexico_inegi_2016": RawNameSource(
        table="raw_mexico_inegi_translated",
        key_expression="r.sdfn_rsur_cvegeo",
        original_column="sdfn_rsur_nom_rasgo",
    ),
    "sinir_2024": RawNameSource(
        table="raw_sinir_cities_served_by_landfills_translated",
        key_expression="r.facility_code::text",
        original_column="facility_name",
    ),
}


def source_name_sql(spec: RawNameSource) -> str:
    """One source's join from the ledger to its raw table.

    DISTINCT because the ledger carries a row per resolved column, so a facility whose name came
    from a source appears once per column it supplied.
    """
    return (
        "SELECT DISTINCT l.internal_facility_id,\n"
        f"       r.{spec.original_column} AS original,\n"
        f"       r.{spec.translated_column} AS translated,\n"
        f"       r.{spec.language_column} AS language\n"
        "FROM consolidation.value_resolution_ledger l\n"
        f"JOIN raw_data.{spec.table} r ON {spec.key_expression} = l.data_source_facility_id\n"
        "WHERE l.column_name = %s AND l.data_source = %s"
    )


def load_name_provenance(config: Any = None) -> dict[Any, dict[str, str]]:
    """{internal_facility_id: {original_site_name, source_language, name_data_source}}.

    Every facility whose name came from a translated source is included, whether or not a second
    spelling is emitted. The two facts are independent and were conflated in the first version of
    this module:

    - **`source_language`** says the name passed through translation from that language. It is
      recorded for all 14,692 matched facilities, because it tells the agent which language to
      search in even when the name itself came through unchanged.
    - **`original_site_name`** is filled only when the source's spelling actually differs from
      the translation (7,992 facilities). Repeating an identical string costs tokens on every
      search and invites the agent to treat one name as two independent leads.

    Keying the prompt off the name alone mislabelled the 6,700 facilities in the gap as
    untranslated and threw away a known language for the 4,458 of those whose source was not
    already English.

    The difference test is a plain casefold, not an accent-insensitive one. Measured against the
    full corpus, only 22 of 8,511 differing pairs differ by diacritics or punctuation alone, so
    folding them would add a Unicode normalization step to suppress 0.3% of rows -- and those 22
    still carry the source's own spelling, which is the thing being searched.
    """
    from .db import fetch_all

    found: dict[Any, dict[str, str]] = {}
    for data_source, spec in RAW_NAME_SOURCES.items():
        rows = fetch_all(
            source_name_sql(spec), (LEDGER_NAME_COLUMN, data_source), config=config
        )
        for row in rows:
            facility_id = row["internal_facility_id"]
            if facility_id in found:
                continue
            original = spec.extract(row["original"])
            translated = spec.extract(row["translated"])
            differs = bool(original) and original.casefold() != translated.casefold()
            found[facility_id] = {
                "original_site_name": original if differs else "",
                "source_language": normalize_scalar(row["language"]),
                "name_data_source": data_source,
            }
    return found


def attach_name_provenance(
    records: list[dict[str, Any]], provenance: dict[Any, dict[str, str]]
) -> None:
    """Add the name-provenance fields to seed records in place, blank where none was found."""
    from .seed_source import FACILITY_KEY, as_int

    for record in records:
        facility_id = as_int(record.get(FACILITY_KEY)) or as_int(record.get("facility_id"))
        extra = provenance.get(facility_id, {})
        record["original_site_name"] = extra.get("original_site_name", "")
        record["source_language"] = extra.get("source_language", "")
        record["name_data_source"] = extra.get("name_data_source", "")
