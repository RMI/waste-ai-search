"""WP-531 item 2: search for the facility operator, in the review layer only.

The upstream standardized spec has no operator column yet, so operator reaches resolved.csv and the
review queue but never the standardized table. Promotion is a separate follow-up once upstream adds
the column; these tests pin today's boundary so that change is deliberate.
"""
from __future__ import annotations

from datetime import date

from waste_ai_search.arbitrate import extract_evidence
from waste_ai_search.arbitration import resolve
from waste_ai_search.prompt_builder import build_site_prompt, requested_attributes
from waste_ai_search.schema import (
    ATTRIBUTE_TO_STANDARD_COLUMN,
    GAP_FILL_ATTRIBUTES,
    REQUESTABLE_ATTRIBUTES,
    STANDARDIZED_FACILITY_COLUMNS,
)


NEWS = {
    "source_title": "City awards landfill contract",
    "publisher": "Daily News",
    "source_type": "News report",
    "publication_date": "2024-02-02",
    "url": "https://news.example.com/landfill-contract",
}


def site(**overrides):
    base = {
        "site_id": "1",
        "internal_facility_id": "1",
        "site_name": "Olushosun Landfill",
        "country_iso3": "NGA",
        "contributing_data_sources": "osm_2022",
        "reference_year": "2022",
    }
    base.update(overrides)
    return base


def resolve_operator(value, sources):
    s = site()
    payload = {"site_id": "1", "attributes": [{
        "attribute_name": "operator",
        "value": value,
        "value_basis": "Direct",
        "confidence_score": "High",
        "value_date": "2024",
        "evidence_summary": "Contract awarded.",
        "sources": sources,
    }]}
    by_attr, _evidence, _sources, _warn = extract_evidence(s, payload, "run", "v", date(2026, 1, 1))
    return s, resolve(s, "operator", by_attr.get("operator", []))


def test_operator_is_requestable_and_gap_filled():
    assert "operator" in REQUESTABLE_ATTRIBUTES
    assert "operator" in GAP_FILL_ATTRIBUTES


def test_operator_is_asked_when_the_baseline_has_none():
    """consolidated_facility carries no operator, so every searched facility is asked."""
    assert "operator" in requested_attributes(site())


def test_operator_is_not_asked_when_the_seed_already_has_one():
    assert "operator" not in requested_attributes(site(operator="Lagos Waste Management Authority"))


def test_coordinates_only_countries_are_not_asked_for_an_operator():
    assert "operator" not in requested_attributes(site(country_iso3="BRA", is_location_exact="FALSE"))


def test_the_prompt_asks_for_the_operator_not_the_owner_or_regulator():
    prompt = build_site_prompt(site(), attributes=requested_attributes(site()))
    assert '"attribute_name": "operator"' in prompt
    assert "not the owner, landlord or regulator" in prompt


def test_a_found_operator_is_resolved_as_text_with_its_source_and_tier():
    _s, row = resolve_operator("Lagos Waste Management Authority", [NEWS])
    assert row["resolved_value"] == "Lagos Waste Management Authority"
    assert row["resolution"] == "Filled empty baseline"
    assert row["winning_source_tier"] == "Tier 3"
    assert row["winning_source_url"] == NEWS["url"]


def test_a_tier_3_operator_reaches_the_review_queue():
    from waste_ai_search.arbitration import needs_review

    _s, row = resolve_operator("Lagos Waste Management Authority", [NEWS])
    assert needs_review(row)


def test_operator_never_reaches_the_standardized_table():
    """Review layer only until upstream adds a column. Pinned so promoting it is deliberate."""
    from waste_ai_search.standardized import build_standard_record

    s, row = resolve_operator("Lagos Waste Management Authority", [NEWS])
    record, _notes = build_standard_record(s, [row], sha="deadbeef")

    assert "operator" not in ATTRIBUTE_TO_STANDARD_COLUMN
    assert "operator" not in STANDARDIZED_FACILITY_COLUMNS
    assert record is None or "operator" not in record


def test_a_found_operator_is_carried_into_the_next_pass_seed():
    """A gap-fill attribute: once found, a later pass does not ask again."""
    from waste_ai_search.seed_refresh import refresh_sites

    s, row = resolve_operator("Lagos Waste Management Authority", [NEWS])
    [refreshed], _stats = refresh_sites([s], [row], "run")

    assert refreshed["operator"] == "Lagos Waste Management Authority"
    assert "operator" not in requested_attributes(refreshed)
