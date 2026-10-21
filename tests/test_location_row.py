"""A found location is one value: its two coordinates are accepted, reviewed and loaded together."""
from __future__ import annotations

import csv
import json

from openpyxl import load_workbook

from waste_ai_search.arbitrate import auto_validated_rows, combine_location, run_arbitration, settle_near_coordinates
from waste_ai_search.input_loader import write_csv_records
from waste_ai_search.run_context import PipelineConfig
from waste_ai_search.schema import LOCATION_ATTRIBUTE, REVIEW_QUEUE_HEADERS
from waste_ai_search.seed_refresh import refresh_sites
from waste_ai_search.workbook_io import REVIEW_INSTRUCTIONS

REGISTRY = "https://registry.example/site-21"


def half(attribute, value, resolution, status, url=REGISTRY, date="2024", rule=None, baseline=""):
    """One coordinate's resolved row, as arbitration.resolve leaves it."""
    found = bool(value)
    return {
        "site_id": "21",
        "internal_facility_id": "21",
        "site_name": "EL HONGO",
        "country_iso3": "MEX",
        "attribute_name": attribute,
        "baseline_value": baseline,
        "baseline_source": "mexico_inegi_2016" if baseline else "",
        "baseline_tier": "Tier 2" if baseline else "",
        "resolved_value": value,
        "resolved_unit": "decimal degrees" if found else "",
        "resolution": resolution,
        "resolution_rule": rule or f"{attribute} {resolution}.",
        "winning_evidence_id": f"{attribute}-1" if found else "",
        "winning_source_tier": "Tier 2" if found else "",
        "winning_source_url": url if found else "",
        "winning_link_status": "OK" if found else "",
        "best_tier_available": "Tier 2" if found else "",
        "agreeing_source_count": 1 if found else 0,
        "dissenting_source_count": 0,
        "value_date": date if found else "",
        "confidence_score": "High" if found else "",
        "validation_status": status,
        "reviewer": "AI Agent" if status == "Auto-validated" else "",
        "reviewed_date": "2026-10-06" if status == "Auto-validated" else "",
        "researcher_notes": "",
    }


def only_location(resolved):
    rows = [row for row in resolved if row["attribute_name"] == LOCATION_ATTRIBUTE]
    assert len(rows) == 1
    assert not [row for row in resolved if row["attribute_name"] in {"found_latitude", "found_longitude"}]
    return rows[0]


def test_two_accepted_halves_are_one_accepted_location():
    resolved = [
        half("found_latitude", "32.51", "Confirmed baseline", "Auto-validated", baseline="32.51"),
        half("found_longitude", "-116.21", "Overrides baseline", "Auto-validated", baseline="-116.2", date="2025"),
    ]

    assert combine_location(resolved) is None
    location = only_location(resolved)
    assert location["resolved_value"] == "32.51, -116.21"
    assert location["baseline_value"] == "32.51, -116.2"
    # The pair changes the baseline, so the change describes it.
    assert location["resolution"] == "Overrides baseline"
    assert location["validation_status"] == "Auto-validated"
    assert location["reviewer"] == "AI Agent"
    assert location["value_date"] == "2025"


def test_a_location_goes_to_review_when_either_half_does():
    resolved = [
        half("found_latitude", "32.51", "Confirmed baseline", "Auto-validated", baseline="32.51"),
        half("found_longitude", "-116.25", "Conflict - needs review", "Needs review", baseline="-116.2"),
    ]

    combine_location(resolved)
    location = only_location(resolved)
    assert (location["resolution"], location["validation_status"]) == ("Conflict - needs review", "Needs review")
    assert (location["reviewer"], location["reviewed_date"]) == ("", "")
    assert location["resolution_rule"] == (
        "Latitude: found_latitude Confirmed baseline. Longitude: found_longitude Conflict - needs review."
    )


def test_a_location_near_the_seeds_confirms_it_and_keeps_the_seeds_point():
    # The longitude is about 0.5 km from the seed's, under the review threshold: the same place.
    # Left as a conflict, the auto-validated location matched neither resolved.csv nor the queue.
    site = {"latitude": "32.51", "longitude": "-116.2"}
    resolved = [
        half("found_latitude", "32.51", "Confirmed baseline", "Auto-validated", baseline="32.51"),
        half("found_longitude", "-116.205", "Conflict - needs review", "Needs review", baseline="-116.2"),
    ]

    assert settle_near_coordinates(site, resolved) == 1
    combine_location(resolved)
    location = only_location(resolved)
    assert (location["resolution"], location["validation_status"]) == ("Confirmed baseline", "Auto-validated")
    # A confirmation keeps the seed's point, so nothing moves.
    assert location["resolved_value"] == "32.51, -116.2"
    assert "Found 32.51, -116.205, which confirms the seed's location" in location["resolution_rule"]
    assert auto_validated_rows(resolved) == [location]


def test_a_coordinate_more_than_half_a_km_away_still_goes_to_review():
    # About 0.7 km from the seed's: farther than the review threshold, so an SME decides.
    site = {"latitude": "32.51", "longitude": "-116.2"}
    resolved = [half("found_longitude", "-116.2075", "Conflict - needs review", "Needs review", baseline="-116.2")]

    assert settle_near_coordinates(site, resolved) == 0
    assert (resolved[0]["resolution"], resolved[0]["validation_status"]) == ("Conflict - needs review", "Needs review")


def test_a_location_from_two_tiers_shows_the_weaker():
    resolved = [
        half("found_latitude", "32.51", "Filled empty baseline", "Auto-validated"),
        {**half("found_longitude", "-116.21", "Filled empty baseline", "Needs review", url="https://other.example"),
         "winning_source_tier": "Tier 3"},
    ]

    combine_location(resolved)
    location = only_location(resolved)
    assert location["winning_source_tier"] == "Tier 3"
    assert "Latitude's source is Tier 2, longitude's Tier 3." in location["resolution_rule"]


def test_a_location_the_baseline_outranks_goes_to_leads():
    # Batch 1's usual split: one coordinate matched the seed's, the other lost to it.
    resolved = [
        half("found_latitude", "32.51", "Confirmed baseline", "Auto-validated", baseline="32.51"),
        half("found_longitude", "-116.9", "Conflict - lower credibility", "Routed to leads", baseline="-116.2"),
    ]

    combine_location(resolved)
    location = only_location(resolved)
    assert (location["resolution"], location["validation_status"]) == ("Conflict - lower credibility", "Routed to leads")
    assert location["resolved_value"] == "32.51, -116.9"


def test_coordinates_from_two_sources_go_to_review():
    resolved = [
        half("found_latitude", "32.51", "Filled empty baseline", "Auto-validated"),
        half("found_longitude", "-116.21", "Filled empty baseline", "Auto-validated", url="https://other.example"),
    ]

    combine_location(resolved)
    location = only_location(resolved)
    assert location["validation_status"] == "Needs review"
    assert "come from different sources (longitude: https://other.example)" in location["resolution_rule"]


def test_a_lone_coordinate_is_not_a_location_but_is_a_lead():
    resolved = [
        half("found_latitude", "32.51", "Filled empty baseline", "Auto-validated"),
        half("found_longitude", "", "Not found", "Auto-validated", rule="No source-backed evidence returned."),
    ]

    lone = combine_location(resolved)
    location = only_location(resolved)
    assert (location["resolved_value"], location["resolution"]) == ("", "Not found")
    assert location["resolution_rule"] == "Only the latitude was found, and a location needs both, so neither is used."
    assert lone["attribute_name"] == "found_latitude" and lone["resolved_value"] == "32.51"


def test_a_lone_coordinate_that_only_confirms_the_seed_is_not_a_lead():
    resolved = [
        half("found_latitude", "32.51", "Confirmed baseline", "Auto-validated", baseline="32.51"),
        half("found_longitude", "", "Conflict - lower credibility", "Routed to leads", baseline="-116.2"),
    ]

    assert combine_location(resolved) is None
    # The weaker longitude still reaches the leads sheet, as it did before the halves were joined.
    location = only_location(resolved)
    assert (location["resolution"], location["validation_status"]) == ("Conflict - lower credibility", "Routed to leads")


def test_a_site_with_no_coordinates_is_untouched():
    resolved = [half("operator", "Ecoserv", "Filled empty baseline", "Auto-validated")]

    assert combine_location(resolved) is None
    assert [row["attribute_name"] for row in resolved] == ["operator"]


def test_refresh_seed_merges_a_found_location_into_both_coordinates():
    site = {"site_id": "21", "latitude": "32.0", "longitude": "-116.0", "ai_filled_fields": ""}
    location = {
        **half("found_latitude", "", "Overrides baseline", "Auto-validated", baseline="32.0"),
        "attribute_name": LOCATION_ATTRIBUTE,
        "resolved_value": "32.51, -116.21",
        "winning_source_tier": "Tier 2",
    }

    (refreshed,), stats = refresh_sites([site], [location], "run", merge_identity=True)
    assert (refreshed["latitude"], refreshed["longitude"]) == ("32.51", "-116.21")
    # Stamped per coordinate, the way the next pass reads them.
    assert refreshed["ai_filled_fields"] == "found_latitude@Tier 2; found_longitude@Tier 2"
    assert stats["values_merged"] == 2

    (unchanged,), _ = refresh_sites([site], [location], "run")
    assert (unchanged["latitude"], unchanged["longitude"]) == ("32.0", "-116.0")


def test_a_correction_can_carry_its_own_date():
    assert REVIEW_QUEUE_HEADERS.index("corrected_value_date") == REVIEW_QUEUE_HEADERS.index("corrected_value") + 1
    queue = next(row for row in REVIEW_INSTRUCTIONS if row["tab_name"] == "Review_Queue")
    assert "corrected_value_date" in queue["editable"]


# --- end to end -----------------------------------------------------------------------------
NEWS = {
    "source_title": "El Hongo dump to close",
    "publisher": "Diario local",
    "source_type": "News report",
    "publication_date": "2023-05-01",
    "url": "https://news.example.mx/el-hongo",
}


def arbitrate_el_hongo(tmp_path, coordinates, source=NEWS):
    """Arbitrate one site, with no seed coordinates, whose agent response reports `coordinates`."""
    from waste_ai_search.seed_source import seed_headers

    run_dir = tmp_path / "run"
    site = {
        "facility_id": "21", "site_id": "21", "internal_facility_id": "21", "year": "2016",
        "site_name": "EL HONGO", "facility_name": "EL HONGO", "country_iso3": "MEX", "iso3c_plus": "MEX",
        "contributing_data_sources": "osm_2022", "reference_year": "2016",
    }
    write_csv_records(run_dir / "seed.csv", [site], seed_headers())

    response = {
        "site_id": "21",
        "attributes": [
            {
                "attribute_name": name,
                "value": value,
                "value_basis": "Direct",
                "confidence_score": "High",
                "value_date": "2023",
                "evidence_summary": "The article gives the site's coordinates.",
                "sources": [source],
            }
            for name, value in coordinates
        ],
        "unverified_leads": [],
    }
    raw = run_dir / "raw_foundry_responses"
    raw.mkdir(parents=True)
    (raw / "site_21.json").write_text(json.dumps(response))

    run_arbitration(PipelineConfig(input_csv=None, run_dir=run_dir, run_id="run", dataset_version="v"))
    return run_dir


def test_a_real_arbitration_writes_one_location_row(tmp_path):
    run_dir = arbitrate_el_hongo(tmp_path, [("found_latitude", "32.51"), ("found_longitude", "-116.21")])

    # A Tier 3 source fills an empty location, so the pair waits for review as one row.
    queue = list(csv.DictReader((run_dir / "review_queue.csv").open()))
    assert [(row["attribute_name"], row["resolved_value"]) for row in queue] == [(LOCATION_ATTRIBUTE, "32.51, -116.21")]

    ws = load_workbook(run_dir / "run_review.xlsx")["Review_Queue"]
    header = [cell.value for cell in ws[1]]
    assert header.index("corrected_value_date") == header.index("corrected_value") + 1

    standardized = next(csv.DictReader((run_dir / "std_facility_tbl_ai_search_run.csv").open()))
    assert (standardized["latitude"], standardized["longitude"]) == ("32.51", "-116.21")


def test_a_lone_low_tier_coordinate_is_listed_as_a_lead_once(tmp_path):
    # A Tier 4 source may fill an empty baseline, so its latitude wins while sitting below the
    # promotion floor. It is half a location, listed as such, and not again as a weak claim.
    wiki = {**NEWS, "publisher": "Wikimapia", "source_type": "Wiki or aggregator", "url": "https://wiki.example/el-hongo"}
    run_dir = arbitrate_el_hongo(tmp_path, [("found_latitude", "32.51")], source=wiki)

    leads = [row for row in csv.DictReader((run_dir / "supplementary_leads.csv").open()) if row["attribute_name"] == "found_latitude"]
    assert [row["exclusion_reason"] for row in leads] == ["Half a location: the other coordinate was not found."]
