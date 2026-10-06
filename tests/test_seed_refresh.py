"""The two-pass loop that makes gating GCCS on known gas collection lossless."""
from waste_ai_search.credibility import ai_filled_tiers
from waste_ai_search.arbitration import resolve
from waste_ai_search.prompt_builder import has_gas_collection, requested_attributes
from waste_ai_search.schema import GCCS_ATTRIBUTES, UNSEARCHED_ATTRIBUTES
from waste_ai_search.seed_refresh import REFRESH_COLUMNS, refresh_sites, refreshed_headers


def site(**overrides):
    base = {
        "site_id": "1",
        "site_name": "Test Landfill",
        "country_iso3": "NGA",
        "contributing_data_sources": "osm_2022",
        "reference_year": "2022",
        "has_landfill_gas_collection": "",
        "facility_status": "",
        "facility_type": "",
    }
    base.update(overrides)
    return base


def resolved(attribute, value, resolution="Filled empty baseline", tier="Tier 1"):
    return {
        "site_id": "1",
        "attribute_name": attribute,
        "resolution": resolution,
        "resolved_value": value,
        "winning_source_tier": tier,
    }


def test_gccs_is_not_requested_until_gas_collection_is_known():
    assert not has_gas_collection(site())
    assert not [a for a in requested_attributes(site()) if a.startswith("gccs")]


def test_discovered_gas_collection_unlocks_gccs_on_the_next_pass():
    pass1 = site()
    assert not set(requested_attributes(pass1)) & GCCS_ATTRIBUTES

    refreshed, stats = refresh_sites([pass1], [resolved("has_landfill_gas_collection", "TRUE")], "run1")

    assert stats["gas_collection_discovered"] == 1
    pass2 = refreshed[0]
    assert has_gas_collection(pass2)
    assert set(requested_attributes(pass2)) & GCCS_ATTRIBUTES == GCCS_ATTRIBUTES - UNSEARCHED_ATTRIBUTES


def test_answered_attributes_are_not_asked_again():
    refreshed, _stats = refresh_sites(
        [site()],
        [resolved("facility_status", "Active"), resolved("facility_type", "Dumpsite")],
        "run1",
    )

    second = requested_attributes(refreshed[0])
    assert "facility_status" not in second
    assert "facility_type" not in second


def test_unreviewed_conflicts_are_not_merged():
    """Only promoted values carry forward; a conflict awaiting a human must not become baseline."""
    refreshed, stats = refresh_sites(
        [site()],
        [resolved("facility_status", "Active", resolution="Conflict - needs review")],
        "run1",
    )

    assert stats["values_merged"] == 0
    assert refreshed[0]["facility_status"] == ""


def test_lead_routed_values_are_not_merged():
    refreshed, stats = refresh_sites(
        [site()],
        [resolved("facility_status", "Active", resolution="Conflict - lower credibility")],
        "run1",
    )
    assert stats["values_merged"] == 0


def test_merged_values_record_the_tier_that_supplied_them():
    refreshed, _stats = refresh_sites(
        [site()],
        [resolved("facility_status", "Active", tier="Tier 3")],
        "run1",
    )

    assert ai_filled_tiers(refreshed[0]["ai_filled_fields"]) == {"facility_status": 3}
    assert refreshed[0]["ai_filled_run_ids"] == "run1"


def test_next_pass_tiers_an_ai_filled_baseline_by_its_own_source():
    """Otherwise pass 2 would judge new evidence against the seed datasets' tier, not the value's."""
    refreshed, _stats = refresh_sites([site()], [resolved("facility_status", "Active", tier="Tier 3")], "run1")
    pass2 = refreshed[0]

    # A Tier 1 regulator now contradicts a Tier 3 AI-filled baseline and should win.
    row = resolve(pass2, "facility_status", [{"value": "Inactive", "tier": 1, "order": 0, "evidence_id": "E", "url": "u"}])
    assert row["baseline_tier"] == "Tier 3"
    assert "previous ai_search pass" in row["baseline_staleness_note"]
    assert row["resolution"] == "Overrides baseline"

    # An equally credible Tier 3 source does not.
    row = resolve(pass2, "facility_status", [{"value": "Inactive", "tier": 3, "order": 0, "evidence_id": "E", "url": "u"}])
    assert row["resolution"] == "Conflict - needs review"


def test_identity_is_only_merged_when_asked():
    rows = [resolved("found_facility_name", "Real Name Landfill")]

    without, _ = refresh_sites([site()], rows, "run1")
    assert without[0]["site_name"] == "Test Landfill"

    with_identity, _ = refresh_sites([site()], rows, "run1", merge_identity=True)
    assert with_identity[0]["site_name"] == "Real Name Landfill"


def test_refresh_columns_are_appended_once():
    headers = refreshed_headers(["site_id", "site_name"])
    assert headers[-2:] == REFRESH_COLUMNS
    assert refreshed_headers(headers) == headers


def test_repeated_refreshes_accumulate_run_ids_without_duplicating_fields():
    first, _ = refresh_sites([site()], [resolved("facility_status", "Active", tier="Tier 3")], "run1")
    second, _ = refresh_sites(first, [resolved("facility_type", "Dumpsite", tier="Tier 1")], "run2")

    tiers = ai_filled_tiers(second[0]["ai_filled_fields"])
    assert tiers == {"facility_status": 3, "facility_type": 1}
    assert second[0]["ai_filled_run_ids"] == "run1; run2"


def test_values_with_no_database_column_survive_the_refreshed_seed(tmp_path):
    """A database seed has no column for these, so writing dropped them and pass 2 asked again."""
    from waste_ai_search.input_loader import load_sites
    from waste_ai_search.seed_refresh import write_refreshed_seed

    found = {
        "bulk_waste_type": "inert waste",
        "operator": "Waste Authority",
        "has_landfill_gas_collection": "TRUE",
        "has_flare": "TRUE",
        "flare_efficiency": "0.98",
    }
    refreshed, _stats = refresh_sites([site()], [resolved(a, v) for a, v in found.items()], "run1")
    database_headers = list(site())  # no column for operator, bulk_waste_type or the flare pair
    path = write_refreshed_seed(tmp_path / "refreshed_seed.csv", refreshed, database_headers)

    reloaded, _headers = load_sites(path)
    assert {a: reloaded[0][a] for a in found} == found
    assert not set(found) & set(requested_attributes(reloaded[0]))
