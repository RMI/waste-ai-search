"""The workbook is the only file an SME opens, so its affordances must match the instructions.

Since step 2 of the approve-sources plan it shows found_values.csv, read-only, in place of the
Review_Queue and Leads tabs; the tests of the queue's dropdowns (validation_status,
rejection_reason, corrected_value, corrected_site_id) and of its match column went with it. The
Site_Review tab, where SMEs decide each site-source pair, is step 3.
"""
from openpyxl import load_workbook
from openpyxl.utils import get_column_letter

from waste_ai_search.schema import FOUND_VALUES_HEADERS
from waste_ai_search.workbook_io import STATUS_FILLS, write_review_workbook


def build(tmp_path, found_rows=None):
    path = write_review_workbook(
        tmp_path / "r_review.xlsx",
        run_config=[{"setting": "run_id", "value": "r"}],
        found_values=found_rows
        if found_rows is not None
        else [{h: "" for h in FOUND_VALUES_HEADERS} | {"site_id": "1", "status": "SME review"}],
        parse_warnings=[],
    )
    return load_workbook(path)


def test_expected_tabs_exist(tmp_path):
    wb = build(tmp_path)
    assert wb.sheetnames == ["ReadMe", "Run_Config", "Found_Values", "Parse_Warnings", "Definitions"]


def test_found_values_is_read_only(tmp_path):
    """A dropdown on a pipeline-set column reads as permission to change it."""
    ws = build(tmp_path)["Found_Values"]
    assert list(ws.data_validations.dataValidation) == []
    readme = {row[0]: row for row in list(build(tmp_path)["ReadMe"].values)[1:]}
    assert readme["Found_Values"][3] == "nothing"


def test_definitions_tab_publishes_the_tier_scale(tmp_path):
    defs = build(tmp_path)["Definitions"]
    rows = list(defs.values)
    index = rows[0].index("source_tier")
    assert [r[index] for r in rows[1:] if r[index]] == ["Tier 1", "Tier 2", "Tier 3", "Tier 4", "Tier 5"]


def test_readme_tab_documents_every_tab_and_the_csv_sidecars(tmp_path):
    wb = build(tmp_path)
    described = {row[0] for row in list(wb["ReadMe"].values)[1:]}
    for tab in ("Found_Values", "Parse_Warnings", "Definitions"):
        assert tab in described
    assert any(str(name).startswith("(file)") for name in described)


def test_the_readme_says_what_each_status_means(tmp_path):
    purpose = {row[0]: row[1] for row in list(build(tmp_path)["ReadMe"].values)[1:]}["Found_Values"]
    for words in ("auto-approved", "SME review", "not used", "more than 5 km", "Not a Waste Facility", "review_id"):
        assert words in purpose


def test_the_readme_describes_the_lists_for_ingestion(tmp_path):
    described = {row[0]: row[1] for row in list(build(tmp_path)["ReadMe"].values)[1:]}
    for name in ("new_facility_candidates.csv", "possible_duplicates.csv", "datasets_found.csv"):
        assert f"(file) {name}" in described
    assert "(file) outside_run_matches.csv" not in described
    assert "same page under the same facility name" in described["(file) possible_duplicates.csv"]


def test_each_row_is_shaded_by_its_status_and_a_check_the_page_fails(tmp_path):
    ws = build(tmp_path)["Found_Values"]
    letters = {ws.cell(1, c).value: get_column_letter(c) for c in range(1, ws.max_column + 1)}
    formulas = [rule.formula[0] for rules in ws.conditional_formatting for rule in rules.rules]
    for status in STATUS_FILLS:
        assert f'${letters["status"]}2="{status}"' in formulas
    quote = letters["quote_on_page"]
    assert f'OR(${quote}2="not found",${quote}2="partly found")' in formulas


def test_baseline_coordinates_paste_straight_into_google_maps():
    """SMEs copy the seed location into Google Maps' search box, which takes "lat, lon"."""
    from waste_ai_search.arbitrate import baseline_coordinates

    assert baseline_coordinates({"latitude": "45.51234", "longitude": "-73.55432"}) == "45.512340, -73.554320"
    assert baseline_coordinates({"latitude": "", "longitude": "-73.5"}) == ""
