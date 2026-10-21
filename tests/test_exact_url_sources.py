"""Sources are deduped on the exact URL, query string included (build step 1 of the source-matching plan).

Cleaning a URL once dropped its query string, which merged 103 different Natural Resources Wales
documents into one source. Now sources.csv has one row per exact URL, each with a source_id derived
from that URL, and every evidence.csv row carries the id of its source, so the two files join. In
batch 1, 6,641 of 10,485 evidence rows had a source_id missing from sources.csv. Within one
attribute, a URL cited twice is one source when corroboration is counted.
"""
from __future__ import annotations

import csv
import json
import re
from datetime import date

from waste_ai_search.arbitrate import (
    dedupe_sources,
    extract_evidence,
    normalize_url,
    run_arbitration,
    source_id_for,
)
from waste_ai_search.arbitration import resolve
from waste_ai_search.run_context import PipelineConfig


NRW = "https://naturalresources.wales/media/document.ashx?id=PERMIT-"
REGULATOR = {"source_title": "Permit", "publisher": "Natural Resources Wales", "source_type": "Regulator",
             "publication_date": "2023-01-01", "url": NRW + "1"}
NEWS = {"source_title": "Story", "publisher": "Daily News", "source_type": "News report",
        "publication_date": "2024-01-01", "url": "https://news.example.com/story"}


def site(site_id="1"):
    return {"site_id": site_id, "internal_facility_id": site_id, "site_name": f"Landfill {site_id}",
            "country_iso3": "GBR", "latitude": "51.5", "longitude": "-3.2",
            "contributing_data_sources": "osm_2022", "reference_year": "2022"}


def attribute(name, value, *sources):
    return {"attribute_name": name, "value": value, "unit": "", "value_basis": "Direct",
            "confidence_score": "High", "value_date": "2023", "evidence_summary": "s",
            "sources": list(sources)}


def extract(*attributes, site_id="1"):
    return extract_evidence(site(site_id), {"site_id": site_id, "attributes": list(attributes)},
                            "r", "v", date(2026, 1, 1))


# --- the exact URL ----------------------------------------------------------------------------
def test_the_query_string_is_kept():
    assert normalize_url(NRW + "1") != normalize_url(NRW + "2")
    assert normalize_url("https://x.org/doc?ID=AbC&lang=EN") == "https://x.org/doc?ID=AbC&lang=EN"


def test_only_spellings_that_never_change_the_page_are_cleaned():
    exact = "https://example.org/Docs/Permit"
    assert normalize_url("HTTPS://WWW.Example.ORG/Docs/Permit/") == exact
    assert normalize_url("https://example.org/Docs/Permit#page=3") == exact
    assert normalize_url("example.org/Docs/Permit") == exact  # written without a scheme
    assert normalize_url("https://example.org/Docs/Permit/?id=1") == exact + "?id=1"


def test_a_malformed_url_does_not_stop_arbitration():
    assert normalize_url("https://[invalid") == "https://[invalid"


def test_a_source_id_comes_from_the_exact_url():
    source_id = source_id_for(NRW + "1", fallback="unused")
    assert re.fullmatch(r"WAISRC-[0-9a-f]{12}", source_id)
    assert source_id_for("HTTPS://WWW.naturalresources.wales/media/document.ashx?id=PERMIT-1", "x") == source_id
    assert source_id_for(NRW + "2", "x") != source_id
    assert source_id_for("", fallback="WAISRC-1-001-001") == "WAISRC-1-001-001"


# --- sources.csv ------------------------------------------------------------------------------
def test_documents_that_differ_only_in_their_query_are_separate_sources():
    _by, _evidence, sources, _warnings = extract(
        attribute("operator", "A Ltd", REGULATOR, dict(REGULATOR, url=NRW + "2")),
    )
    assert len(dedupe_sources(sources)) == 2


def test_one_url_cited_by_two_sites_is_one_source_with_one_id():
    _b1, evidence_1, sources_1, _w1 = extract(attribute("operator", "A Ltd", REGULATOR), site_id="1")
    _b2, evidence_2, sources_2, _w2 = extract(attribute("operator", "A Ltd", REGULATOR), site_id="2")
    assert evidence_1[0]["source_id"] == evidence_2[0]["source_id"]

    deduped = dedupe_sources(sources_1 + sources_2)
    assert len(deduped) == 1
    assert deduped[0]["source_id"] == evidence_1[0]["source_id"]
    assert (deduped[0]["times_cited"], deduped[0]["cited_site_count"]) == (2, 2)


def test_each_citation_keeps_its_own_evidence_id():
    _by, evidence, _sources, _warnings = extract(attribute("operator", "A Ltd", REGULATOR, REGULATOR))
    assert len({row["evidence_id"] for row in evidence}) == 2
    assert len({row["source_id"] for row in evidence}) == 1


def test_a_citation_without_a_url_keeps_a_source_row_of_its_own():
    _by, evidence, sources, _warnings = extract(
        attribute("operator", "A Ltd", dict(REGULATOR, url=""), dict(REGULATOR, url="")),
    )
    ids = [row["source_id"] for row in evidence]
    assert len(set(ids)) == 2
    assert {row["source_id"] for row in dedupe_sources(sources)} == set(ids)


def test_evidence_and_sources_join_in_a_real_run(tmp_path):
    from waste_ai_search.input_loader import write_csv_records
    from waste_ai_search.seed_source import seed_headers

    run_dir = tmp_path / "run"
    seeds = [dict(site(sid), facility_id=sid, year="2022", facility_name=f"Landfill {sid}", iso3c_plus="GBR")
             for sid in ("1", "2")]
    write_csv_records(run_dir / "seed.csv", seeds, seed_headers())
    raw = run_dir / "raw_foundry_responses"
    raw.mkdir(parents=True)
    responses = {
        "1": [attribute("operator", "A Ltd", REGULATOR, dict(REGULATOR, url=NRW + "2")),
              attribute("facility_status", "active", NEWS, dict(NEWS, url="https://WWW.news.example.com/story/"))],
        "2": [attribute("operator", "B Ltd", REGULATOR, NEWS)],
    }
    for sid, attributes in responses.items():
        (raw / f"site_{sid}.json").write_text(json.dumps({"site_id": sid, "attributes": attributes}))

    run_arbitration(PipelineConfig(input_csv=None, run_dir=run_dir, run_id="run", dataset_version="v"))

    evidence = list(csv.DictReader((run_dir / "evidence.csv").open()))
    sources = list(csv.DictReader((run_dir / "sources.csv").open()))
    assert len(evidence) == 6
    assert {row["source_id"] for row in evidence} == {row["source_id"] for row in sources}
    by_url = {row["url_normalized"]: row for row in sources}
    assert sorted(by_url) == sorted([normalize_url(NRW + "1"), normalize_url(NRW + "2"), NEWS["url"]])
    assert by_url[NEWS["url"]]["times_cited"] == "3"
    assert by_url[NEWS["url"]]["cited_site_count"] == "2"


# --- corroboration ----------------------------------------------------------------------------
def test_one_url_cited_twice_for_an_attribute_agrees_once():
    by_attribute, _evidence, _sources, _warnings = extract(
        attribute("facility_status", "active", NEWS, NEWS),
        attribute("facility_status", "active", dict(NEWS, url="https://news.example.com/story/#top")),
    )
    row = resolve(site(), "facility_status", by_attribute["facility_status"])
    assert row["agreeing_source_count"] == 1


def test_different_urls_still_agree_separately():
    other = dict(NEWS, url="https://trade.example.com/a", source_type="Trade press")
    by_attribute, _evidence, _sources, _warnings = extract(attribute("facility_status", "active", NEWS, other))
    row = resolve(site(), "facility_status", by_attribute["facility_status"])
    assert row["agreeing_source_count"] == 2


def test_a_page_cited_twice_does_not_outvote_two_pages():
    """Corroboration breaks a tie between equally good, equally recent sources: sources count, not citations."""
    second = dict(NEWS, url="https://news.example.com/other")
    third = dict(NEWS, url="https://news.example.com/third")
    by_attribute, _evidence, _sources, _warnings = extract(
        attribute("facility_status", "active", NEWS, NEWS),
        attribute("facility_status", "inactive", second, third),
    )
    row = resolve(site(), "facility_status", by_attribute["facility_status"])
    assert row["resolved_value"] == "Inactive"
    assert (row["agreeing_source_count"], row["dissenting_source_count"]) == (2, 1)
