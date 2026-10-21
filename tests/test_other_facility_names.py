"""A value whose winning source names another facility says so (build step 4 of the source-matching plan).

The agent now reports a source about a different facility rather than leaving it out, and until
build step 5 matches each source to the site it is about, every source is still filed under the
site searched. The 5 km check flags a source that places the facility elsewhere. One that only
names another facility, with no location, is flagged by name: its value carries a researcher note.
"""
from __future__ import annotations

import csv
import json

import pytest

from waste_ai_search.arbitrate import names_another_facility, run_arbitration
from waste_ai_search.input_loader import write_csv_records
from waste_ai_search.run_context import PipelineConfig


@pytest.mark.parametrize(
    ("theirs", "ours"),
    [
        (["Beta Landfill"], ["Alpha Landfill"]),
        (["Beta Landfill", "Beta Landfill"], ["Alpha Landfill", "Décharge Alpha"]),
        (["North Landfill"], ["South Landfill"]),
    ],
)
def test_a_name_sharing_no_distinctive_word_is_another_facility(theirs, ours):
    assert names_another_facility(theirs, ours)


@pytest.mark.parametrize(
    ("theirs", "ours"),
    [
        (["Thunder Bay Solid Waste & Recycling Facility"], ["Thunder Bay Landfill"]),
        (["Seaton Meadows Landfil"], ["Seaton Medows"]),  # a typo on either side
        (["Lixão da Sede", "Sede Dump"], ["Headquarters dump", "Lixão Sede"]),  # the original name
        (["Relleno Sanitario Tijuana"], ["Municipal Landfill of Tijuana"]),
        # Nothing to judge by: a generic name, no name, or another script.
        (["Landfill Site"], ["Alpha Landfill"]),
        (["Beta Landfill"], ["domestic landfill"]),
        (["", ""], ["Alpha Landfill"]),
        (["Полигон ТБО Красноярск", ""], ["Krasnoyarsk Solid Waste Landfill"]),
        # Sharing only the kind of road says nothing, but here the name is shared too.
        (["Brady Road Resource Management Facility"], ["Brady Road Landfill"]),
    ],
)
def test_a_name_sharing_a_word_or_that_cannot_be_judged_is_not_flagged(theirs, ours):
    assert not names_another_facility(theirs, ours)


REGULATOR = {
    "url": "https://reg.example/alpha", "source_title": "Alpha permit", "publisher": "Ministry of Environment",
    "source_type": "Regulator", "publication_date": "2020-01-01",
}
NEWS = {
    "url": "https://news.example/beta", "source_title": "Beta closes", "publisher": "Daily News",
    "source_type": "News report", "publication_date": "2024-03-01",
}


def value(name, found, date):
    return {"attribute_name": name, "value": found, "value_basis": "Direct", "confidence_score": "High",
            "value_date": date, "evidence_summary": "...", "quoted_evidence_short": "..."}


def test_a_value_won_by_a_source_naming_another_facility_carries_a_note(tmp_path):
    """The reviewer's case: a newer news story about Beta Landfill, with no location, wins Alpha's status."""
    from waste_ai_search.seed_source import seed_headers

    run_dir = tmp_path / "run"
    site = {
        "facility_id": "1", "site_id": "1", "internal_facility_id": "1", "site_name": "Alpha Landfill",
        "facility_name": "Alpha Landfill", "country_iso3": "CAN", "iso3c_plus": "CAN", "latitude": "45.0",
        "longitude": "-75.0", "contributing_data_sources": "osm_2022", "reference_year": "2022", "year": "2022",
    }
    write_csv_records(run_dir / "seed.csv", [site], seed_headers())
    raw = run_dir / "raw_foundry_responses"
    raw.mkdir(parents=True)
    response = {"site_id": "1", "sources": [
        {**REGULATOR, "facility_name_in_source": "Alpha Landfill", "values": [value("facility_status", "Active", "2020")]},
        {**NEWS, "facility_name_in_source": "Beta Landfill", "facility_name_english": "Beta Landfill", "values": [
            value("facility_status", "Inactive", "2024"), value("closing_year", "2023", "2024"),
        ]},
    ], "unverified_leads": [], "search_notes": ""}
    (raw / "site_1.json").write_text(json.dumps(response))
    run_arbitration(PipelineConfig(input_csv=None, run_dir=run_dir, run_id="run", dataset_version="v"))

    queue = {row["attribute_name"]: row for row in csv.DictReader((run_dir / "review_queue.csv").open(encoding="utf-8-sig"))}
    note = "The winning source names Beta Landfill; check it is about this facility."
    for name in ("facility_status", "closing_year"):
        assert (queue[name]["winning_source_url"], queue[name]["researcher_notes"]) == (NEWS["url"], note)
