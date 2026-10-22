"""WP-531 item 1: every SME-facing output shows the translated and original site names together.

So a reviewer can check results against the source's own spelling. The review queue's editable
`translation_note` went with the queue (step 2 of the approve-sources plan): found_values.csv is
read-only.

The end-to-end test builds a run directory - a pinned seed and one cached agent response - and runs
the real arbitration over it. It is offline: the seed snapshot means no database is touched.
"""
from __future__ import annotations

import csv
import json

import pytest
from openpyxl import load_workbook

from waste_ai_search.arbitrate import attach_site_names, run_arbitration
from waste_ai_search.input_loader import write_csv_records
from waste_ai_search.run_context import PipelineConfig
from waste_ai_search.schema import FOUND_VALUES_HEADERS, SITE_NAME_COLUMNS


ALL_OUTPUTS = {
    "found_values.csv": FOUND_VALUES_HEADERS,
}


# --- headers --------------------------------------------------------------------------------
@pytest.mark.parametrize("output,headers", ALL_OUTPUTS.items())
def test_every_output_carries_the_three_name_columns_side_by_side(output, headers):
    start = headers.index("site_name")
    assert headers[start:start + 3] == SITE_NAME_COLUMNS, output


# --- filling the values ---------------------------------------------------------------------
SITES = {
    "21": {"site_id": "21", "site_name": "THE FUNGUS", "original_site_name": "EL HONGO", "source_language": "es"},
    "7": {"site_id": "7", "site_name": "Greenview Landfill", "original_site_name": "", "source_language": ""},
}


def test_names_are_filled_from_the_seed_by_site():
    rows = [{"site_id": "21"}, {"site_id": "7"}]
    attach_site_names(rows, SITES)
    assert rows[0] == {"site_id": "21", "site_name": "THE FUNGUS", "original_site_name": "EL HONGO", "source_language": "es"}
    assert rows[1]["original_site_name"] == ""  # untranslated source: blank, not invented


def test_an_existing_site_name_is_not_overwritten():
    rows = [{"site_id": "21", "site_name": "Already set"}]
    attach_site_names(rows, SITES)
    assert rows[0]["site_name"] == "Already set"
    assert rows[0]["original_site_name"] == "EL HONGO"


def test_a_row_for_an_unknown_site_gets_blanks_rather_than_failing():
    rows = [{"site_id": "999"}]
    attach_site_names(rows, SITES)
    assert rows[0]["original_site_name"] == "" and rows[0]["source_language"] == ""


# --- end to end -----------------------------------------------------------------------------
def test_a_real_arbitration_writes_both_names_everywhere(tmp_path):
    from waste_ai_search.seed_source import seed_headers

    run_dir = tmp_path / "run"
    site = {
        "facility_id": "21", "site_id": "21", "internal_facility_id": "21", "year": "2016",
        "site_name": "THE FUNGUS", "facility_name": "THE FUNGUS",
        "original_site_name": "EL HONGO", "source_language": "es", "name_data_source": "mexico_inegi_2016",
        "country_iso3": "MEX", "iso3c_plus": "MEX", "latitude": "32.5", "longitude": "-116.2",
        "contributing_data_sources": "mexico_inegi_2016", "reference_year": "2016",
    }
    write_csv_records(run_dir / "seed.csv", [site], seed_headers())

    response = {
        "site_id": "21",
        "attributes": [{
            "attribute_name": "facility_status",
            "value": "Inactive",
            "value_basis": "Direct",
            "confidence_score": "High",
            "value_date": "2023",
            "evidence_summary": "Clandestine dump closed by the municipality.",
            "sources": [{
                "source_title": "Cierran basurero clandestino de El Hongo",
                "publisher": "Diario local",
                "source_type": "News report",
                "publication_date": "2023-05-01",
                "url": "https://news.example.mx/el-hongo",
            }],
        }],
        "unverified_leads": [],
    }
    raw = run_dir / "raw_foundry_responses"
    raw.mkdir(parents=True)
    (raw / "site_21.json").write_text(json.dumps(response))

    config = PipelineConfig(input_csv=None, run_dir=run_dir, run_id="run", dataset_version="v")
    run_arbitration(config)

    row = next(csv.DictReader((run_dir / "found_values.csv").open()))
    assert (row["site_name"], row["original_site_name"], row["source_language"]) == ("THE FUNGUS", "EL HONGO", "es")

    ws = load_workbook(run_dir / "run_review.xlsx")["Found_Values"]
    header = [c.value for c in ws[1]]
    first = {h: ws.cell(2, i + 1).value for i, h in enumerate(header)}
    assert (first["site_name"], first["original_site_name"], first["source_language"]) == ("THE FUNGUS", "EL HONGO", "es")
