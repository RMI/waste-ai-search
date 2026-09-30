from __future__ import annotations

from datetime import date
from pathlib import Path
from typing import Any

from openpyxl import Workbook
from openpyxl.formatting.rule import FormulaRule
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.worksheet.datavalidation import DataValidation
from openpyxl.utils import get_column_letter

from .schema import (
    CONTRADICTION_HEADERS,
    DEFINITION_VALUES,
    FIELD_TO_DEFINITION,
    PARSE_WARNING_HEADERS,
    REVIEW_QUEUE_HEADERS,
    SUPPLEMENTARY_LEADS_HEADERS,
)


def style_sheet(ws) -> None:
    header_fill = PatternFill("solid", fgColor="1F4E78")
    for cell in ws[1]:
        cell.font = Font(bold=True, color="FFFFFF")
        cell.fill = header_fill
        cell.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)
    ws.freeze_panes = "A2"
    ws.sheet_view.showGridLines = False
    for row in ws.iter_rows(min_row=2):
        for cell in row:
            cell.alignment = Alignment(vertical="top", wrap_text=True)
            if isinstance(cell.value, date):
                cell.number_format = "yyyy-mm-dd"
            header = ws.cell(1, cell.column).value
            if header and "url" in str(header).lower() and cell.value:
                cell.hyperlink = str(cell.value)
                cell.style = "Hyperlink"
    for idx, col in enumerate(ws.columns, start=1):
        header = ws.cell(1, idx).value or ""
        max_len = len(str(header))
        for cell in list(col)[1:80]:
            if cell.value is not None:
                max_len = max(max_len, min(len(str(cell.value)), 80))
        width = min(max(max_len + 2, 10), 54)
        if any(term in str(header).lower() for term in ["summary", "notes", "reason", "url", "address"]):
            width = 54
        ws.column_dimensions[get_column_letter(idx)].width = width
    ws.auto_filter.ref = ws.dimensions


def write_sheet(ws, headers: list[str], rows: list[dict[str, Any]]) -> None:
    ws.append(headers)
    for record in rows:
        ws.append([record.get(header, "") for header in headers])
    style_sheet(ws)



REVIEW_INSTRUCTIONS = [
    {
        "tab_name": "Review_Queue",
        "purpose": (
            "YOUR WORKLIST. A filtered view of resolved.csv containing only rows the pipeline "
            "would not sign off itself: a value from a weak or unclassifiable source, or a "
            "candidate that disagrees with the existing baseline. Everything else was already "
            "auto-validated (reviewer = AI Agent) and is not shown here. Every row here also "
            "exists in resolved.csv - nothing lives only in this tab."
        ),
        "sme_action": (
            "Work top to bottom. Open winning_source_url and check the source says this, about "
            "THIS facility. Set validation_status to Validated or Rejected, put your name in "
            "reviewer and the date in reviewed_date, and say why in researcher_notes. An empty "
            "queue means nothing needs you."
        ),
        "editable": "validation_status, reviewer, reviewed_date, researcher_notes - nothing else",
    },
    {
        "tab_name": "Contradictions",
        "purpose": (
            "Facilities where facility_type came back 'Not a Waste Facility': a source says the "
            "site was never a waste disposal site - a quarry, a mine, a yard that never took "
            "waste. Only facilities known solely from OSM or Global Plastic Watch are offered this "
            "value. A closed, capped or redeveloped landfill is NOT one; closure_also_reported "
            "flags any row where the same run also found the site closed, the likeliest "
            "misreading. The value IS promoted to the standardized table like any facility_type. "
            "An empty tab means no source contradicted any checked site - NOT that they were "
            "confirmed, since absence of coverage is not evidence against a site."
        ),
        "sme_action": (
            "Read only. Each row also appears in Review_Queue; record the decision there. "
            "Reject any row that describes a closed landfill."
        ),
        "editable": "nothing",
    },
    {
        "tab_name": "(file) resolved.csv",
        "purpose": (
            "One row per facility x attribute searched, INCLUDING 'Not found' and auto-validated "
            "rows. This is the complete picture; the Review_Queue tab is the subset of it that "
            "needs you."
        ),
        "sme_action": "Read only. Open it to see what was searched and found nothing.",
        "editable": "nothing",
    },
    {
        "tab_name": "(file) evidence.csv",
        "purpose": "One row per source-backed claim, with the tier rule applied to each source.",
        "sme_action": "Read only. Open it to see why a value won or lost.",
        "editable": "nothing",
    },
    {
        "tab_name": "(file) std_facility_tbl_ai_search_*.csv",
        "purpose": "The 43-column standardized facility table - only promoted values, ready to merge.",
        "sme_action": "Read only.",
        "editable": "nothing",
    },
    {
        "tab_name": "Leads",
        "purpose": "Findings whose only sources fell below the credibility floor, plus the agent's own unverified leads.",
        "sme_action": "Read only. Promote something by hand if you judge the source good enough.",
        "editable": "nothing",
    },
    {
        "tab_name": "Parse_Warnings",
        "purpose": "Values the pipeline dropped or could not map, including unit and enum problems.",
        "sme_action": "Scan for systematic problems worth fixing upstream.",
        "editable": "nothing",
    },
    {
        "tab_name": "Definitions",
        "purpose": "Allowed values, mirrored from the live database enums.",
        "sme_action": "Read only.",
        "editable": "nothing",
    },
]

RESOLUTION_FILLS = {
    "Conflict - needs review": "FFF2CC",
    "Overrides baseline": "FCE4D6",
    "Filled empty baseline": "E2EFDA",
    "Insufficient credibility": "EDEDED",
}


def add_definitions(ws) -> None:
    """One column per vocabulary, used as the source for the dropdowns."""
    names = [
        "review_decision",
        "resolution",
        "validation_status",
        "source_tier",
        "facility_status",
        "facility_type",
        "cover_type",
        "confidence_score",
    ]
    ws.append(names)
    longest = max(len(DEFINITION_VALUES[name]) for name in names)
    for index in range(longest):
        ws.append([
            DEFINITION_VALUES[name][index] if index < len(DEFINITION_VALUES[name]) else ""
            for name in names
        ])
    style_sheet(ws)


def add_validations(wb, ws) -> None:
    """Attach dropdowns to the editable review columns only.

    A dropdown on a pipeline-set column (resolution, tiers) reads as an invitation to change it.
    """
    def_sheet = wb["Definitions"]
    columns = {def_sheet.cell(1, c).value: c for c in range(1, def_sheet.max_column + 1)}
    headers = {ws.cell(1, c).value: c for c in range(1, ws.max_column + 1)}
    last_row = max(ws.max_row, 2)

    for header, definition_name in FIELD_TO_DEFINITION.items():
        if header not in headers or definition_name not in columns:
            continue
        col = columns[definition_name]
        letter = get_column_letter(col)
        count = len(DEFINITION_VALUES[definition_name])
        formula = f"=Definitions!${letter}$2:${letter}${count + 1}"
        validation = DataValidation(type="list", formula1=formula, allow_blank=True, showDropDown=False)
        ws.add_data_validation(validation)
        target = get_column_letter(headers[header])
        validation.add(f"{target}2:{target}{last_row}")


def add_conditionals(ws) -> None:
    """Shade each row by its resolution so the queue reads at a glance."""
    headers = {ws.cell(1, c).value: c for c in range(1, ws.max_column + 1)}
    if "resolution" not in headers:
        return
    letter = get_column_letter(headers["resolution"])
    span = f"A2:{get_column_letter(ws.max_column)}{max(ws.max_row, 2)}"
    for value, color in RESOLUTION_FILLS.items():
        ws.conditional_formatting.add(
            span,
            FormulaRule(formula=[f'$%s2="%s"' % (letter, value)], fill=PatternFill("solid", fgColor=color)),
        )


def write_review_workbook(
    path: Path,
    *,
    run_config: list[dict[str, Any]],
    review_queue: list[dict[str, Any]],
    leads: list[dict[str, Any]],
    contradictions: list[dict[str, Any]] | None = None,
    parse_warnings: list[dict[str, Any]],
) -> Path:
    """The SME-facing artifact: only rows that need a human decision (Q31).

    Evidence, Resolved and the standardized table stay as CSV sidecars for audit. They are far too
    large to review by hand at full scale, and nothing in them requires a decision.
    """
    wb = Workbook()
    wb.remove(wb.active)

    ws_readme = wb.create_sheet("ReadMe")
    write_sheet(ws_readme, ["tab_name", "purpose", "sme_action", "editable"], REVIEW_INSTRUCTIONS)

    ws_config = wb.create_sheet("Run_Config")
    write_sheet(ws_config, ["setting", "value"], run_config)

    ws_queue = wb.create_sheet("Review_Queue")
    write_sheet(ws_queue, REVIEW_QUEUE_HEADERS, review_queue)

    ws_contradictions = wb.create_sheet("Contradictions")
    write_sheet(ws_contradictions, CONTRADICTION_HEADERS, contradictions or [])

    ws_leads = wb.create_sheet("Leads")
    write_sheet(ws_leads, SUPPLEMENTARY_LEADS_HEADERS, leads)

    ws_warnings = wb.create_sheet("Parse_Warnings")
    write_sheet(ws_warnings, PARSE_WARNING_HEADERS, parse_warnings)

    ws_defs = wb.create_sheet("Definitions")
    add_definitions(ws_defs)

    add_validations(wb, ws_queue)
    add_conditionals(ws_queue)

    path.parent.mkdir(parents=True, exist_ok=True)
    wb.save(path)
    return path
