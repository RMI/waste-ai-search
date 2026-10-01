"""WP-531 item 4: `found_site_name` is now `found_facility_name`, matching upstream `facility_name`.

Data written before the rename still says `found_site_name`: cached agent responses, pinned seed
snapshots, and the `attribute@dataset` provenance a seed carries. These tests use the OLD name on
purpose, to prove each reader still understands it. Without the alias, re-arbitrating an older run
drops the name as an unsupported attribute and nothing reports it.
"""
from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import pytest

from waste_ai_search.arbitrate import extract_evidence
from waste_ai_search.credibility import ai_filled_tiers, attribute_sources
from waste_ai_search.schema import (
    ATTRIBUTE_TO_STANDARD_COLUMN,
    IDENTITY_ATTRIBUTES,
    REQUESTABLE_ATTRIBUTES,
    canonical_attribute,
)


REGULATOR = {
    "source_title": "Facility register",
    "publisher": "Environment Agency",
    "source_type": "Regulator",
    "publication_date": "2023-01-01",
    "url": "https://agency.example.gov/f/1",
}


def site():
    return {
        "site_id": "1",
        "internal_facility_id": "1",
        "site_name": "Seed Name",
        "country_iso3": "NGA",
        "contributing_data_sources": "osm_2022",
        "reference_year": "2022",
    }


def test_the_new_name_is_the_one_in_use():
    assert "found_facility_name" in IDENTITY_ATTRIBUTES
    assert "found_facility_name" in REQUESTABLE_ATTRIBUTES
    assert "found_site_name" not in REQUESTABLE_ATTRIBUTES
    assert ATTRIBUTE_TO_STANDARD_COLUMN["found_facility_name"] == "facility_name"


@pytest.mark.parametrize("name", ["found_facility_name", "facility_type", "found_latitude", ""])
def test_current_names_pass_through_unchanged(name):
    assert canonical_attribute(name) == name


def test_the_retired_name_maps_to_its_replacement():
    assert canonical_attribute("found_site_name") == "found_facility_name"
    assert canonical_attribute("  found_site_name ") == "found_facility_name"


def test_a_cached_response_using_the_old_name_still_arbitrates():
    payload = {
        "site_id": "1",
        "attributes": [{
            "attribute_name": "found_site_name",
            "value": "Olushosun Landfill",
            "value_basis": "Direct",
            "confidence_score": "High",
            "sources": [REGULATOR],
        }],
    }
    by_attr, evidence, _sources, warnings = extract_evidence(site(), payload, "run", "v", date(2026, 1, 1))

    assert "found_facility_name" in by_attr
    assert evidence[0]["attribute_name"] == "found_facility_name"
    assert not any("Unsupported attribute_name" in w for w in warnings)


def test_seed_provenance_written_with_the_old_name_is_still_read():
    """Pinned seeds and run snapshots carry `found_site_name@dataset`."""
    assert attribute_sources("found_site_name@lmop_2024; facility_status@osm_2022") == {
        "found_facility_name": "lmop_2024",
        "facility_status": "osm_2022",
    }


def test_refreshed_seed_fields_written_with_the_old_name_are_still_read():
    assert ai_filled_tiers("found_site_name@Tier 2") == {"found_facility_name": 2}


def test_real_cached_responses_from_earlier_runs_still_parse():
    """Every cached response on disk that uses the old name must yield the new one."""
    root = Path(__file__).resolve().parents[1] / "outputs" / "runs"
    files = [p for p in root.glob("*/raw_foundry_responses/*.json") if '"found_site_name"' in p.read_text()]
    if not files:
        pytest.skip("no cached responses from before the rename on this machine")

    for path in files:
        data = json.loads(path.read_text())
        payload = data.get("parsed") or data.get("payload") or data
        if not isinstance(payload, dict) or not payload.get("attributes"):
            continue
        by_attr, _evidence, _sources, warnings = extract_evidence(
            site(), payload, "run", "v", date(2026, 1, 1)
        )
        assert not any("found_site_name" in w for w in warnings), path.name
        names = {a.get("attribute_name") for a in payload["attributes"]}
        if "found_site_name" in names and any(s.get("url") for a in payload["attributes"] for s in a.get("sources", [])):
            assert "found_site_name" not in by_attr, path.name
