"""The workbook is the only file an SME opens, so its affordances must match the instructions."""
from openpyxl import load_workbook
from openpyxl.utils import get_column_letter

from waste_ai_search.schema import DEFINITION_VALUES, REVIEW_QUEUE_HEADERS
from waste_ai_search.workbook_io import write_review_workbook


def build(tmp_path, queue_rows=None):
    path = write_review_workbook(
        tmp_path / "r_review.xlsx",
        run_config=[{"setting": "run_id", "value": "r"}],
        review_queue=queue_rows
        if queue_rows is not None
        else [{h: "" for h in REVIEW_QUEUE_HEADERS} | {"site_id": "1", "resolution": "Filled empty baseline"}],
        leads=[],
        parse_warnings=[],
    )
    return load_workbook(path)


def test_expected_tabs_exist(tmp_path):
    wb = build(tmp_path)
    assert wb.sheetnames == [
        "ReadMe", "Run_Config", "Review_Queue", "Contradictions", "Leads", "Parse_Warnings", "Definitions",
    ]


def test_only_validation_status_is_editable_via_dropdown(tmp_path):
    """A dropdown on a pipeline-set column reads as permission to change it."""
    ws = build(tmp_path)["Review_Queue"]
    headers = {ws.cell(1, c).value: get_column_letter(c) for c in range(1, ws.max_column + 1)}

    validated_columns = set()
    for dv in ws.data_validations.dataValidation:
        for rng in str(dv.sqref).split():
            validated_columns.add("".join(ch for ch in rng.split(":")[0] if ch.isalpha()))

    assert validated_columns == {headers["validation_status"]}
    assert headers["resolution"] not in validated_columns
    assert headers["winning_source_tier"] not in validated_columns


def test_the_dropdown_offers_only_reviewer_choosable_outcomes(tmp_path):
    wb = build(tmp_path)
    ws = wb["Review_Queue"]
    defs = wb["Definitions"]
    columns = {defs.cell(1, c).value: c for c in range(1, defs.max_column + 1)}

    dv = next(iter(ws.data_validations.dataValidation))
    letter = get_column_letter(columns["review_decision"])
    assert f"${letter}$" in dv.formula1
    assert DEFINITION_VALUES["review_decision"] == ["Validated", "Rejected"]


def test_definitions_tab_publishes_the_tier_scale(tmp_path):
    defs = build(tmp_path)["Definitions"]
    rows = list(defs.values)
    index = rows[0].index("source_tier")
    assert [r[index] for r in rows[1:] if r[index]] == ["Tier 1", "Tier 2", "Tier 3", "Tier 4", "Tier 5"]


def test_readme_tab_documents_every_tab_and_the_csv_sidecars(tmp_path):
    wb = build(tmp_path)
    described = {row[0] for row in list(wb["ReadMe"].values)[1:]}
    for tab in ("Review_Queue", "Leads", "Parse_Warnings", "Definitions"):
        assert tab in described
    assert any(str(name).startswith("(file)") for name in described)
