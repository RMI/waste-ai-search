"""resolved.csv keeps only values the rules auto-validated; the rest live in the review queue."""
from __future__ import annotations

from waste_ai_search.arbitrate import auto_validated_rows
from waste_ai_search.cli import read_arbitrated_rows
from waste_ai_search.input_loader import write_csv_records
from waste_ai_search.schema import RESOLVED_HEADERS, REVIEW_QUEUE_HEADERS
from waste_ai_search.seed_refresh import refresh_sites


def row(attribute, resolution, status, value="x"):
    return {
        "site_id": "1", "attribute_name": attribute, "resolution": resolution,
        "validation_status": status, "resolved_value": value, "winning_source_tier": "Tier 3",
    }


def test_only_found_and_auto_validated_values_are_written():
    rows = [
        row("facility_status", "Filled empty baseline", "Auto-validated", "Active"),
        row("waste_depth", "Overrides baseline", "Auto-validated", "Deep"),
        row("operator", "Confirmed baseline", "Auto-validated", "City"),
        row("area_square_meters", "Not found", "Needs review", ""),
        row("opening_year", "Filled empty baseline", "Needs review", "1990"),
        row("closure_year", "Conflict - needs review", "Needs review", "2001"),
        row("capacity", "Conflict - lower credibility", "Routed to leads", "5"),
        row("distance_from_seed_km", "Calculated", "Auto-validated", "0.4"),
    ]
    kept = [r["attribute_name"] for r in auto_validated_rows(rows)]
    assert kept == ["facility_status", "waste_depth", "operator"]


def test_gas_collection_awaiting_review_still_unlocks_the_follow_up(tmp_path):
    # Pass 2 depends on this: a newly found gas collection system is only Tier 3, so it waits for
    # review - and is no longer in resolved.csv - but must still reach the refreshed seed.
    write_csv_records(tmp_path / "resolved.csv", [], RESOLVED_HEADERS)
    queued = row("has_landfill_gas_collection", "Filled empty baseline", "Needs review", "TRUE")
    write_csv_records(tmp_path / "review_queue.csv", [queued], REVIEW_QUEUE_HEADERS)

    sites = [{"site_id": "1", "has_landfill_gas_collection": ""}]
    refreshed, stats = refresh_sites(sites, read_arbitrated_rows(tmp_path), "run")

    assert refreshed[0]["has_landfill_gas_collection"] == "TRUE"
    assert stats["gas_collection_discovered"] == 1
