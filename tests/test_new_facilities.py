"""SMEs' new facilities join the run's list for ingestion (build step 5 of the source-matching plan).

Arbitration lists the located sources that match none of our sites in new_facility_candidates.csv.
An SME can find another, a value whose source is about a landfill we don't hold, and reject it as
"New facility - not in our database". collect-new-facilities reads the reviewed workbook and adds
those to the same list, once each, and arbitrating the run again keeps them.
"""
from __future__ import annotations

import csv
import json

from waste_ai_search.cli import main
from waste_ai_search.input_loader import write_csv_records
from waste_ai_search.match import NEW_FACILITY_HEADERS
from waste_ai_search.new_facilities import collect_new_facilities, reviewed_workbook, sme_candidates
from waste_ai_search.schema import LOCATION_ATTRIBUTE, NEW_FACILITY_REASON, REVIEW_QUEUE_HEADERS
from waste_ai_search.seed_source import seed_headers
from waste_ai_search.workbook_io import write_review_workbook


def queue_row(attribute, **values):
    return {header: "" for header in REVIEW_QUEUE_HEADERS} | {
        "site_id": "7", "site_name": "domestic landfill", "country_iso3": "CAN", "searched_site_id": "7",
        "attribute_name": attribute, "winning_source_url": "https://council.example/tip",
        "facility_name_in_source": "Hartney Waste Disposal Grounds", "stated_location": "Road 22 N, Hartney",
        "validation_status": "Rejected", "rejection_reason": NEW_FACILITY_REASON,
        "reviewer": "A. Reviewer", "reviewed_date": "2026-11-02", **values,
    }


def reviewed(tmp_path, rows, name="run_review.xlsx"):
    return write_review_workbook(tmp_path / name, run_config=[], review_queue=rows, leads=[], parse_warnings=[])


def listed(run_dir):
    return list(csv.DictReader((run_dir / "new_facility_candidates.csv").open(encoding="utf-8-sig")))


def test_a_source_rejected_as_a_new_facility_is_listed_once_however_many_values(tmp_path):
    rows = [
        queue_row("operator", resolved_value="Municipality of Grassland"),
        queue_row(LOCATION_ATTRIBUTE, resolved_value="49.5612, -100.5501", location_basis="rooftop"),
        queue_row("facility_status", rejection_reason="Wrong value"),
        # An en dash, as Word or Excel may turn a typed hyphen into, reads the same.
        queue_row("opening_year", winning_source_url="https://news.example/hartney",
                  rejection_reason="New facility – not in our database"),
    ]
    first, second = sme_candidates(rows)
    assert (first["found_by"], first["source_urls"], first["facility_names_in_source"]) == (
        "SME", "https://council.example/tip", "Hartney Waste Disposal Grounds",
    )
    assert (first["latitude"], first["longitude"], first["location_basis"]) == ("49.5612", "-100.5501", "rooftop")
    assert (first["stated_locations"], first["searched_site_ids"]) == ("Road 22 N, Hartney", "7")
    assert first["note"] == (
        "Marked a new facility by an SME (A. Reviewer on 2026-11-02) on operator, found_location, filed under "
        "site 7 (domestic landfill)."
    )
    assert second["source_urls"] == "https://news.example/hartney"


def test_collecting_twice_adds_nothing_and_keeps_arbitrations_candidates(tmp_path):
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    arbitrated = {header: "" for header in NEW_FACILITY_HEADERS} | {
        "candidate_id": "WAINEW-000000000001", "found_by": "arbitration", "source_urls": "https://a.example",
    }
    write_csv_records(run_dir / "new_facility_candidates.csv", [arbitrated], NEW_FACILITY_HEADERS)
    workbook = reviewed(tmp_path, [queue_row("operator")])

    assert collect_new_facilities(run_dir, workbook) == 1
    assert collect_new_facilities(run_dir, workbook) == 0
    assert [row["found_by"] for row in listed(run_dir)] == ["arbitration", "SME"]


def test_a_candidate_with_a_point_gets_our_nearest_site_from_the_runs_seed(tmp_path):
    run_dir = tmp_path / "run"
    write_csv_records(run_dir / "seed.csv", [
        {"site_id": "7", "site_name": "domestic landfill", "country_iso3": "CAN", "latitude": "49.60", "longitude": "-100.50"},
        {"site_id": "8", "site_name": "Far Landfill", "country_iso3": "CAN", "latitude": "52.0", "longitude": "-100.0"},
    ], seed_headers())
    workbook = reviewed(tmp_path, [queue_row(LOCATION_ATTRIBUTE, resolved_value="49.5612, -100.5501",
                                             corrected_value="49.5620, -100.5510")])
    collect_new_facilities(run_dir, workbook)
    [row] = listed(run_dir)
    # The SME's own location wins over the source's.
    assert (row["latitude"], row["longitude"], row["location_basis"]) == ("49.5620", "-100.5510", "SME")
    assert (row["nearest_site_id"], row["nearest_site_km"]) == ("7", "5.6")


def test_arbitrating_again_keeps_the_smes_candidates(tmp_path):
    from waste_ai_search.arbitrate import run_arbitration
    from waste_ai_search.run_context import PipelineConfig

    run_dir = tmp_path / "run"
    write_csv_records(run_dir / "seed.csv", [{
        "facility_id": "7", "site_id": "7", "internal_facility_id": "7", "year": "2022", "site_name": "domestic landfill",
        "facility_name": "domestic landfill", "country_iso3": "CAN", "iso3c_plus": "CAN", "latitude": "49.6",
        "longitude": "-100.5", "contributing_data_sources": "osm_2022", "reference_year": "2022",
    }], seed_headers())
    (run_dir / "raw_foundry_responses").mkdir()
    (run_dir / "raw_foundry_responses" / "site_7.json").write_text(json.dumps({"site_id": "7", "sources": []}))
    collect_new_facilities(run_dir, reviewed(tmp_path, [queue_row("operator")]))

    run_arbitration(PipelineConfig(input_csv=None, run_dir=run_dir, run_id="run", dataset_version="v"))
    assert [row["found_by"] for row in listed(run_dir)] == ["SME"]


def test_the_command_reads_the_reviewed_workbook(tmp_path, capsys):
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    workbook = reviewed(tmp_path, [queue_row("operator")], name="mine.xlsx")
    assert main(["collect-new-facilities", "--run-id", "run", "--run-dir", str(run_dir), "--workbook", str(workbook), "--local"]) == 0
    assert "1 new-facility candidate(s) added from mine.xlsx" in capsys.readouterr().out
    assert len(listed(run_dir)) == 1


def test_the_smes_copy_is_found_in_the_review_folder_first(tmp_path):
    run_dir, shared = tmp_path / "run", tmp_path / "SharePoint"
    (shared / "run").mkdir(parents=True)
    assert reviewed_workbook(run_dir, "run", shared) == run_dir / "run_review.xlsx"
    (shared / "run" / "run_review.xlsx").write_bytes(b"")
    assert reviewed_workbook(run_dir, "run", shared) == shared / "run" / "run_review.xlsx"
    assert reviewed_workbook(run_dir, "run", None) == run_dir / "run_review.xlsx"
