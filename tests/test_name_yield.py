"""Step 1 of docs/plans/2026-10-15-skip-unsearchable-names.md: measure what searching each class of
site name found, before any class is skipped. Offline: a fake run folder, a fake blob client."""
from __future__ import annotations

import csv
import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from waste_ai_search import name_yield as ny
from waste_ai_search.searchable_names import GENERIC, MINING, NAMED, NO_NAME, NOT_A_NAME, name_class


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("OpenStreetMap Landfill", NO_NAME),  # ingestion's filler for an OSM polygon with no name
        ("", NO_NAME),
        ("update the tag landuse after survey", NOT_A_NAME),
        ("https://www.dissercat.com/content/inzhenerno", NOT_A_NAME),
        ("https://openstreetcam.org/details", NOT_A_NAME),
        ("4000", NOT_A_NAME),
        ("domestic landfill", GENERIC),
        ("Landfill", GENERIC),
        ("Municipal Garbage Dump", GENERIC),
        ("Hartland Landfill", NAMED),
        ("Route 4000 landfill", NAMED),
        ("Blade", MINING),  # Russian отвал, a spoil heap, machine-translated
        ("Overburden Blade", MINING),
        ("Blade No5", MINING),
        ("Western Blades", MINING),
        ("Terrikon", MINING),
        ("Overburden Dump", MINING),
        ("Spoil heap", MINING),
        ("Tailings Pond", MINING),
        ("Tailings Landfill", NAMED),  # Brazil's rejeitos: leftover household waste, not mine tailings
        ("Controlled Tailings Landfill", NAMED),
        ("Hanson Quarry Landfill", NAMED),  # landfills in old quarries are real landfills
        ("Mine Road Landfill", NAMED),
        ("Bladensburg Landfill", NAMED),  # a whole word only
    ],
)
def test_names_fall_into_four_classes(name, expected):
    assert name_class(name) == expected


def _write(path: Path, rows: list[dict], headers: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=headers)
        writer.writeheader()
        writer.writerows(rows)


OSM = "osm_2022"


def _fake_run(run_dir: Path, manifest: dict | None = None) -> Path:
    _write(run_dir / "seed.csv", [
        {"site_id": "1", "site_name": "OpenStreetMap Landfill", "original_site_name": "", "country_iso3": "CAN", "contributing_data_sources": OSM},
        {"site_id": "2", "site_name": "Hartland Landfill", "original_site_name": "", "country_iso3": "CAN", "contributing_data_sources": OSM},
        {"site_id": "3", "site_name": "domestic landfill", "original_site_name": "", "country_iso3": "CAN", "contributing_data_sources": "osm_2022+gpw_2021"},
        {"site_id": "4", "site_name": "Brady Road Landfill", "original_site_name": "", "country_iso3": "CAN", "contributing_data_sources": "osm_2022+canada_ghgrp"},
        {"site_id": "5", "site_name": "Never searched", "original_site_name": "", "country_iso3": "CAN", "contributing_data_sources": OSM},
    ], ["site_id", "site_name", "original_site_name", "country_iso3", "contributing_data_sources"])
    _write(run_dir / "foundry_run_log.csv", [
        {"site_id": "1", "site_name": "OpenStreetMap Landfill", "country_iso3": "CAN", "status": "Succeeded", "web_searches": "21"},
        {"site_id": "2", "site_name": "Hartland Landfill", "country_iso3": "CAN", "status": "Succeeded after 1 tool retry(s)", "web_searches": "30"},
        {"site_id": "3", "site_name": "domestic landfill", "country_iso3": "CAN", "status": "Failed", "web_searches": "4"},
        {"site_id": "4", "site_name": "Brady Road Landfill", "country_iso3": "CAN", "status": "Skipped existing response", "web_searches": ""},
    ], ["site_id", "site_name", "country_iso3", "status", "web_searches"])
    _write(run_dir / "resolved.csv", [
        # a confirmation adds nothing new; a fill does
        {"site_id": "2", "attribute_name": "facility_status", "resolution": "Filled empty baseline", "resolved_value": "Active", "researcher_notes": ""},
        {"site_id": "2", "attribute_name": "found_latitude", "resolution": "Confirmed baseline", "resolved_value": "48.5", "researcher_notes": ""},
        {"site_id": "4", "attribute_name": "found_latitude", "resolution": "Confirmed baseline", "resolved_value": "49.8", "researcher_notes": ""},
    ], ["site_id", "attribute_name", "resolution", "resolved_value", "researcher_notes"])
    _write(run_dir / "review_queue.csv", [
        {"site_id": "1", "searched_site_id": "1", "attribute_name": "found_facility_name", "resolution": "Conflict - needs review",
         "resolved_value": "Smalltown Landfill", "researcher_notes": ""},
        {"site_id": "1", "searched_site_id": "1", "attribute_name": "facility_type", "resolution": "Filled empty baseline",
         "resolved_value": "Not a Waste Facility", "researcher_notes": ""},
        {"site_id": "2", "searched_site_id": "2", "attribute_name": "found_longitude", "resolution": "Confirmed baseline",
         "resolved_value": "-123", "researcher_notes": ""},
        # Site 1's search found site 4's operator: credited to site 1, and filed under site 4.
        {"site_id": "4", "searched_site_id": "1", "attribute_name": "operator", "resolution": "Filled empty baseline",
         "resolved_value": "City of Brady", "researcher_notes": ""},
    ], ["site_id", "searched_site_id", "attribute_name", "resolution", "resolved_value", "researcher_notes"])
    if manifest is not None:
        folder = run_dir / "consolidation_snapshot"
        folder.mkdir(parents=True)
        (folder / "manifest.json").write_text(json.dumps(manifest))
    return run_dir


def test_each_searched_site_says_what_its_search_found(tmp_path):
    run = ny.load_run("batch1", ny.local_reader(_fake_run(tmp_path / "batch1")))
    rows = {row["site_id"]: row for row in ny.site_rows(run)}

    assert set(rows) == {"1", "2", "3", "4"}  # site 5 was never searched
    placeholder = rows["1"]
    assert (placeholder["name_class"], placeholder["source_group"]) == (NO_NAME, ny.MAP_ONLY)
    assert (placeholder["new_values_accepted"], placeholder["new_values_to_review"]) == (0, 3)
    assert placeholder["found_name"] and placeholder["filed_under_another_site"] == 1
    assert placeholder["not_a_waste_facility"]
    assert placeholder["web_searches"] == 21

    named = rows["2"]
    assert (named["new_values_accepted"], named["new_values_to_review"]) == (1, 0)  # confirmations don't count
    assert named["filed_under_another_site"] == 0
    assert rows["3"]["source_group"] == ny.MAP_ONLY and not rows["3"]["answered"]
    assert rows["4"]["source_group"] == ny.OTHER_SOURCES and rows["4"]["answered"]
    assert rows["4"]["web_searches"] == 0
    assert rows["4"]["new_values_to_review"] == 0  # its operator row was found by site 1's search


def test_a_search_is_credited_with_the_values_it_found_wherever_they_are_filed(tmp_path):
    """Replaces the identity gate's count (build step 5): a search that found another site's values
    is no longer flagged by a distance, so name_yield counts the values filed under another site.

    End to end: a "domestic landfill" search finds a permit for Brady Road Landfill, 40 km away,
    which states its coordinates. The permit is filed under Brady Road; the search gets the credit.
    """
    from waste_ai_search.arbitrate import run_arbitration
    from waste_ai_search.input_loader import write_csv_records
    from waste_ai_search.run_context import PipelineConfig
    from waste_ai_search.seed_source import seed_headers

    run_dir = tmp_path / "run"

    def seed(sid, name, lat, lon):
        return {"facility_id": sid, "site_id": sid, "internal_facility_id": sid, "year": "2022", "site_name": name,
                "facility_name": name, "country_iso3": "CAN", "iso3c_plus": "CAN", "latitude": lat, "longitude": lon,
                "contributing_data_sources": "osm_2022", "reference_year": "2022"}

    write_csv_records(run_dir / "seed.csv", [
        seed("7", "domestic landfill", "50.2", "-97.5"), seed("8", "Brady Road Landfill", "49.7", "-97.2"),
    ], seed_headers())
    _write(run_dir / "foundry_run_log.csv", [
        {"site_id": "7", "site_name": "domestic landfill", "country_iso3": "CAN", "status": "Succeeded", "web_searches": "4"},
    ], ["site_id", "site_name", "country_iso3", "status", "web_searches"])
    permit = {"url": "https://winnipeg.example.ca/brady-road", "source_title": "Brady Road permit",
              "publisher": "Manitoba Environment", "source_type": "Permit or registry", "publication_date": "2024-03-01",
              "facility_name_in_source": "Brady Road Resource Management Facility",
              "stated_location": "49.7012, -97.2034", "stated_location_kind": "coordinates",
              "values": [{"attribute_name": "operator", "value": "City of Winnipeg", "value_basis": "Direct",
                          "confidence_score": "High", "value_date": "2024", "evidence_summary": "Names the operator."}]}
    raw = run_dir / "raw_foundry_responses"
    raw.mkdir()
    (raw / "site_7.json").write_text(json.dumps({"site_id": "7", "sources": [permit]}))
    run_arbitration(PipelineConfig(input_csv=None, run_dir=run_dir, run_id="run", dataset_version="v"))

    run = ny.load_run("run", ny.local_reader(run_dir))
    assert sorted((row["site_id"], row["searched_site_id"], row["attribute_name"]) for row in run.review) == [
        ("8", "7", "found_location"), ("8", "7", "operator"),
    ]
    [row] = ny.site_rows(run)
    assert row["site_id"] == "7"
    assert (row["new_values_to_review"], row["filed_under_another_site"]) == (2, 2)


def test_the_summary_compares_each_class_with_named_sites_from_the_same_sources(tmp_path):
    run = ny.load_run("batch1", ny.local_reader(_fake_run(tmp_path / "batch1")))
    summary = {(row["source_group"], row["name_class"]): row for row in ny.summarize(ny.site_rows(run))}

    no_name = summary[(ny.MAP_ONLY, NO_NAME)]
    assert (no_name["sites_searched"], no_name["sites_with_new_value"], no_name["new_value_rate"]) == (1, 1, 1.0)
    assert no_name["queries_per_new_value"] == 7.0  # 21 queries, 3 new values
    assert no_name["rate_vs_named"] == 1.0  # named map-only site 2 also found something
    assert no_name["verdict"] == "too few to judge"
    generic = summary[(ny.MAP_ONLY, GENERIC)]
    assert (generic["sites_searched"], generic["sites_answered"], generic["new_value_rate"]) == (1, 0, None)
    assert summary[(ny.MAP_ONLY, NAMED)]["verdict"] == "comparison"
    assert (ny.OTHER_SOURCES, NO_NAME) not in summary  # empty groups are left out


def _stats(answered=40, rate=0.1, validated=0, rejected=0, wrong=0):
    return {"sites_answered": answered, "new_value_rate": rate, "sme_validated": validated,
            "sme_rejected": rejected, "sme_wrong_facility": wrong}


def test_the_bar():
    assert ny.verdict(_stats(rate=0.09), named_rate=0.2, is_named=False) == "below the bar"
    assert ny.verdict(_stats(rate=0.10), named_rate=0.2, is_named=False) == "clears the bar"
    assert ny.verdict(_stats(answered=29, rate=0.0), named_rate=0.2, is_named=False) == "too few to judge"
    wrong = ny.verdict(_stats(rate=0.3, validated=2, rejected=5, wrong=4), named_rate=0.2, is_named=False)
    assert wrong.startswith("below the bar: SMEs rejected most")
    assert ny.verdict(_stats(rate=0.3), named_rate=None, is_named=False) == "no named sites to compare with"


def test_sme_decisions_come_from_the_reviewed_workbook(tmp_path):
    from openpyxl import Workbook

    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "Review_Queue"
    sheet.append(["site_id", "searched_site_id", "attribute_name", "validation_status", "rejection_reason"])
    sheet.append(["1", "1", "found_facility_name", "Rejected", "Wrong facility"])
    sheet.append(["1", "1", "facility_type", "Validated", ""])
    sheet.append(["2", "2", "found_longitude", "Needs review", ""])  # undecided
    # Since build step 5 a wrong facility says which kind; both count. Credited to the search.
    sheet.append(["4", "1", "operator", "Rejected", "Wrong facility - another site in our database"])
    sheet.append([None, None, None, None, None])
    path = tmp_path / "batch1_review.xlsx"
    workbook.save(path)

    run = ny.load_run("batch1", ny.local_reader(_fake_run(tmp_path / "batch1")))
    run.sme = ny.read_review_workbook(path)
    rows = {row["site_id"]: row for row in ny.site_rows(run)}

    assert len(run.sme) == 4
    assert (rows["1"]["sme_validated"], rows["1"]["sme_rejected"], rows["1"]["sme_wrong_facility"]) == (1, 2, 2)
    assert rows["4"]["sme_rejected"] == 0
    assert (rows["2"]["sme_validated"], rows["2"]["sme_rejected"]) == (0, 0)
    assert ny.read_review_workbook(tmp_path / "missing.xlsx") is None


def test_a_run_that_was_never_arbitrated_is_refused(tmp_path):
    run_dir = _fake_run(tmp_path / "r")
    (run_dir / "resolved.csv").unlink()
    with pytest.raises(FileNotFoundError, match="resolved.csv"):
        ny.load_run("r", ny.local_reader(run_dir))


def test_the_run_reports_whether_its_seed_blended_consolidation_runs(tmp_path):
    blended = ny.load_run("b", ny.local_reader(_fake_run(tmp_path / "b", {"consolidation_run_ids": ["x", "y"]})))
    assert "a blend of 2 runs: x, y" in ny.describe_run(blended)
    unrecorded = ny.load_run("c", ny.local_reader(_fake_run(tmp_path / "c")))
    assert "not recorded" in ny.describe_run(unrecorded)


class _Blob:
    def __init__(self, data):
        self.data = data

    def readall(self):
        return self.data


def test_blob_runs_are_listed_by_folder_and_read_into_memory():
    from azure.core.exceptions import ResourceNotFoundError

    class _Client:
        def walk_blobs(self, name_starts_with, delimiter):
            assert (name_starts_with, delimiter) == ("outputs/runs/", "/")
            return [SimpleNamespace(name="outputs/runs/batch1/"), SimpleNamespace(name="outputs/runs/pilot10/")]

        def download_blob(self, name):
            if name == "outputs/runs/batch1/seed.csv":
                return _Blob(b"site_id\n1\n")
            raise ResourceNotFoundError("missing")

    client = _Client()
    assert ny.list_blob_runs(client) == ["batch1", "pilot10"]
    read = ny.blob_reader(client, "batch1")
    assert read("seed.csv") == b"site_id\n1\n"
    assert read("resolved.csv") is None


def test_the_forecast_selects_sites_as_each_batch_would():
    sites = [
        {"site_id": "1", "site_name": "OpenStreetMap Landfill", "country_iso3": "CAN"},
        {"site_id": "2", "site_name": "Hartland Landfill", "country_iso3": "CAN"},
        {"site_id": "3", "site_name": "OpenStreetMap Landfill", "country_iso3": "MEX"},
        {"site_id": "4", "site_name": "OpenStreetMap Landfill", "country_iso3": "USA"},  # excluded by default
        {"site_id": "5", "site_name": "domestic landfill", "country_iso3": "UKR"},  # the last batch
    ]
    rows = ny.forecast(sites, {NO_NAME: 10.0}, fallback=20.0, cost_per_1000_queries=14.0)
    table = {(row["batch"], row["name_class"]): row for row in rows}

    assert table[(1, NO_NAME)]["sites"] == 1 and table[(1, NAMED)]["sites"] == 1
    assert table[(3, NO_NAME)]["sites"] == 1
    assert sum(row["sites"] for row in rows if row["batch"] == 4) == 0  # USA is in no batch's search
    assert table[(6, GENERIC)]["sites"] == 1
    assert table[(1, NO_NAME)]["est_bing_queries"] == 10
    assert table[(6, GENERIC)]["est_bing_queries"] == 20  # no measurement for the class: the average
    assert table[(6, GENERIC)]["est_cost_usd"] == 0.28


def test_the_script_measures_a_run_folder_and_writes_its_csvs(tmp_path, capsys):
    spec = importlib.util.spec_from_file_location(
        "measure_name_yield", Path(__file__).resolve().parents[1] / "scripts" / "measure_name_yield.py"
    )
    script = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(script)

    run_dir = _fake_run(tmp_path / "runs" / "batch1", {"consolidation_run_ids": ["x"]})
    out = tmp_path / "out"
    code = script.main(["--run-dir", str(run_dir), "--no-forecast", "--review-dir", str(tmp_path), "--out-dir", str(out)])

    assert code == 0
    printed = capsys.readouterr().out
    assert "Run batch1: 4 sites searched; no reviewed workbook found." in printed
    assert "Consolidation run its seed read: x (the only run in the table)." in printed
    assert "Sites from OSM or Global Plastic Watch only" in printed
    assert (out / "batch1_sites.csv").exists()
    with (out / "summary.csv").open(newline="", encoding="utf-8") as handle:
        summary = list(csv.DictReader(handle))
    assert {row["name_class"] for row in summary} == {NO_NAME, GENERIC, NAMED}


@pytest.mark.parametrize(
    ("name", "reason"),
    [
        ("", "empty name"),
        ("OpenStreetMap Landfill", "ingestion's filler for an unnamed OSM polygon"),
        ("https://openstreetcam.org/details", "a URL"),
        ("4000", "a bare number"),
        ("update the tag landuse after survey", "a survey note"),
        ("domestic landfill", "a listed generic phrase"),
        ("Garbage dump", "only words like landfill, dump, waste, garbage, rubbish, tip"),
        ("Municipal Landfill", ""),  # "municipal" counts as an identifying word: named
    ],
)
def test_each_skippable_name_says_which_rule_it_tripped(name, reason):
    from waste_ai_search.searchable_names import name_reason

    assert name_reason(name)[1] == reason


def test_the_listing_names_every_site_a_batch_would_skip():
    sites = [
        {"site_id": "1", "site_name": "OpenStreetMap Landfill", "country_iso3": "CAN", "latitude": "49.1", "longitude": "-123.2"},
        {"site_id": "2", "site_name": "Hartland Landfill", "country_iso3": "CAN"},
        {"site_id": "3", "site_name": "domestic landfill", "country_iso3": "CAN"},
        {"site_id": "4", "site_name": "OpenStreetMap Landfill", "country_iso3": "USA"},  # never searched
        {"site_id": "5", "site_name": "4000", "country_iso3": "SVK"},
    ]
    selections = ny.batch_selections(sites)
    rows = ny.skip_candidates(selections)

    assert [(row["batch"], row["site_id"], row["name_class"]) for row in rows] == [
        (1, "1", NO_NAME), (1, "3", GENERIC), (6, "5", NOT_A_NAME),
    ]
    assert rows[0]["map"] == "https://www.google.com/maps/search/?api=1&query=49.1,-123.2"
    assert rows[1]["map"] == ""
    text = ny.format_skip_candidates(selections, rows)
    assert "Most common no name names (1 sites, 1 different names):" in text
    assert "domestic landfill" in text


@pytest.mark.parametrize(
    ("name", "original", "expected"),
    [
        ("Northern dump", "Отвал Северный", MINING),  # the translation hid it; the original didn't
        ("Waste heap", "Відвал шахти 5", MINING),
        ("Northern dump", "Свалка Северная", NAMED),  # свалка is a household-waste dump: kept
        ("Polygon TBO", "", NAMED),
    ],
)
def test_the_original_name_is_checked_for_mining_words(name, original, expected):
    assert name_class(name, original) == expected
