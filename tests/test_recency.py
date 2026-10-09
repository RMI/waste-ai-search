"""WP-558: within a tier the newer source wins, and for facility_status a much newer source may
beat a better-tier one - always for review. Every other attribute is still decided by tier."""
from __future__ import annotations

from waste_ai_search.arbitration import needs_review, resolve
from waste_ai_search.credibility import TIER_1, TIER_2, TIER_3, TIER_4


def site(**overrides):
    base = {"site_id": "1", "internal_facility_id": "1", "site_name": "Test Landfill", "country_iso3": "AUS",
            "contributing_data_sources": "osm_2022", "reference_year": "2012"}
    return base | overrides


def ev(value, tier, year, order):
    return {"evidence_id": f"e{order}", "attribute_name": "x", "value": value, "unit": "", "tier": tier,
            "order": order, "url": f"https://example.org/{order}", "link_status": "OK",
            "value_date": str(year) if year else "", "confidence": "High", "evidence_summary": ""}


def test_within_a_tier_the_newer_source_wins_and_says_so():
    row = resolve(site(), "facility_status", [ev("Active", TIER_1, 2015, 0), ev("Inactive", TIER_1, 2023, 1)])
    assert row["resolved_value"] == "Inactive"
    assert "Newest of the disagreeing Tier 1 sources (dated 2023) chosen." in row["resolution_rule"]


def test_an_old_permit_loses_status_to_much_newer_news_and_is_reviewed():
    row = resolve(site(), "facility_status", [ev("Active", TIER_1, 2012, 0), ev("Inactive", TIER_3, 2023, 1)])
    assert row["resolved_value"] == "Inactive"
    assert needs_review(row)
    assert "Tier 3 source dated 2023 replaces the Tier 1 source dated 2012" in row["resolution_rule"]


def test_a_newer_lower_tier_source_needs_three_years():
    row = resolve(site(), "facility_status", [ev("Active", TIER_1, 2021, 0), ev("Inactive", TIER_3, 2023, 1)])
    assert row["resolved_value"] == "Active"


def test_below_the_promotion_floor_never_wins_on_date():
    row = resolve(site(), "facility_status", [ev("Active", TIER_1, 2012, 0), ev("Inactive", TIER_4, 2024, 1)])
    assert row["resolved_value"] == "Active"


def test_other_attributes_are_still_decided_by_tier():
    row = resolve(site(), "facility_type", [ev("Sanitary Landfill", TIER_1, 2012, 0), ev("Dumpsite", TIER_3, 2024, 1)])
    assert row["resolved_value"] == "Sanitary Landfill"


def test_a_much_newer_status_may_replace_an_old_baseline_for_review():
    """Baseline "Active" from a 2012 Tier 1 dataset; 2023 news reports closure."""
    s = site(facility_status="Active", attribute_sources="facility_status@eprtr_2022", reference_year="2012")
    row = resolve(s, "facility_status", [ev("Inactive", TIER_3, 2023, 0)])
    assert row["resolution"] == "Overrides baseline"
    assert needs_review(row)
    assert "years newer than the" in row["resolution_rule"]


def test_a_lower_tier_status_close_in_date_still_goes_to_leads():
    s = site(facility_status="Active", attribute_sources="facility_status@eprtr_2022", reference_year="2021")
    row = resolve(s, "facility_status", [ev("Inactive", TIER_3, 2022, 0)])
    assert row["resolution"] == "Conflict - lower credibility"


def test_the_newest_eligible_source_wins_not_the_first_lower_tier_one():
    """Copilot review: Tier 1/2010, Tier 2/2013, Tier 3/2024 must pick 2024, not 2013."""
    row = resolve(site(), "facility_status", [
        ev("Active", TIER_1, 2010, 0), ev("Inactive", TIER_2, 2013, 1), ev("Inactive", TIER_3, 2024, 2),
    ])
    assert row["value_date"] == "2024"
    assert "Tier 3 source dated 2024 replaces the Tier 1 source dated 2010" in row["resolution_rule"]
    assert needs_review(row)


def test_same_year_sources_are_ordered_by_full_date():
    """Copilot review: 2024-12-31 is newer than 2024-01-01, even though the year ties."""
    row = resolve(site(), "facility_status", [ev("Active", TIER_1, "2024-01-01", 0), ev("Inactive", TIER_1, "2024-12-31", 1)])
    assert row["resolved_value"] == "Inactive"
    assert "(dated 2024-12-31) chosen" in row["resolution_rule"]


def test_a_newer_agreeing_source_stops_an_older_disagreement_from_winning():
    """Copilot review: Tier 1/2010 Active, Tier 2/2020 Inactive, Tier 3/2024 Active stays Active."""
    row = resolve(site(), "facility_status", [
        ev("Active", TIER_1, 2010, 0), ev("Inactive", TIER_2, 2020, 1), ev("Active", TIER_3, 2024, 2),
    ])
    assert row["resolved_value"] == "Active"
    assert "replaces" not in row["resolution_rule"]
