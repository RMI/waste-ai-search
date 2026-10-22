from __future__ import annotations

from datetime import date
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlsplit, urlunsplit

from openpyxl import Workbook
from openpyxl.cell.cell import ILLEGAL_CHARACTERS_RE
from openpyxl.formatting.rule import FormulaRule
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.worksheet.datavalidation import DataValidation
from openpyxl.utils import get_column_letter

from .found_values import AUTO_APPROVED, NOT_USED, SME_REVIEW
from .on_page import NOT_FOUND, PARTLY
from .schema import (
    DEFINITION_VALUES,
    FIELD_TO_DEFINITION,
    FOUND_VALUES_HEADERS,
    PARSE_WARNING_HEADERS,
)


# Excel will not follow a hyperlink longer than this; the text stays, the link is not made.
MAX_HYPERLINK_LENGTH = 2079

# Only these become clickable. Agent output is untrusted, and a `javascript:` or `file:` link must
# never turn into something an SME can click in the review workbook.
HYPERLINK_SCHEMES = {"http", "https"}

# Characters left as-is when encoding a link target. `%` is among them on purpose: existing escapes
# are never decoded or re-encoded. The sibling refining-ai-search repo (RDP-63) found that a
# blanket decode breaks links that work - most percent-encoded URLs there served fine as given.
_SAFE_PATH = "/:@!$&'()*+,;=%-._~"
_SAFE_QUERY = _SAFE_PATH + "?"


def clean_cell_text(value: Any) -> Any:
    """Make a value safe to write to a worksheet.

    openpyxl refuses control characters (vertical tab, form feed and the like) with
    IllegalCharacterError, and the error aborts the WHOLE workbook - every row of the run, after
    every agent call has been paid for. Quotes copied out of PDFs are where they come from. They are
    replaced with a space rather than dropped, so words either side do not run together.
    """
    if isinstance(value, str):
        return ILLEGAL_CHARACTERS_RE.sub(" ", value)
    return value


def hyperlink_target(url: Any) -> str | None:
    """The link target to attach to a URL cell, or None if it should not be clickable.

    The cell keeps showing exactly what the source said; only the target is made safe. Raw spaces
    and non-ASCII characters are percent-encoded as UTF-8, and a non-ASCII host is converted to its
    ASCII (IDNA) form, which is what a browser does when the link is followed.
    """
    text = clean_cell_text(str(url).strip()) if url is not None else ""
    if not text or len(text) > MAX_HYPERLINK_LENGTH:
        return None
    try:
        parts = urlsplit(text)
    except ValueError:
        return None
    if parts.scheme.lower() not in HYPERLINK_SCHEMES or not parts.netloc:
        return None

    # Only the host is IDNA-encoded; user info and port are kept, and a bracketed IPv6 literal is
    # left whole rather than split on its colons.
    userinfo, at, hostport = parts.netloc.rpartition("@")
    if hostport.startswith("["):
        host, colon, port = hostport, "", ""
    else:
        host, colon, port = hostport.partition(":")
    if not host.isascii():
        try:
            host = host.encode("idna").decode("ascii")
        except UnicodeError:
            return None
    netloc = f"{userinfo}{at}{host}{colon}{port}"

    target = urlunsplit((
        parts.scheme,
        netloc,
        quote(parts.path, safe=_SAFE_PATH),
        quote(parts.query, safe=_SAFE_QUERY),
        quote(parts.fragment, safe=_SAFE_QUERY),
    ))
    return target if len(target) <= MAX_HYPERLINK_LENGTH else None


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
                target = hyperlink_target(cell.value)
                if target:
                    cell.hyperlink = target
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
        ws.append([clean_cell_text(record.get(header, "")) for header in headers])
        # openpyxl stores any string starting with "=" as a formula, and Excel evaluates it when the
        # workbook opens. Every value here is agent or source text, so none of it may be a formula:
        # "=HYPERLINK(...)" in a quote must be shown, not run.
        for cell in ws[ws.max_row]:
            if cell.data_type == "f":
                cell.data_type = "s"
    style_sheet(ws)



REVIEW_INSTRUCTIONS = [
    {
        "tab_name": "Found_Values",
        "purpose": (
            "Every value the search found, one row per site, year, source and attribute, as "
            "found_values.csv holds them. Every source is filed under the site that was searched, and "
            "status says what becomes of each value. auto-approved: a Tier 1-2 source filling a field "
            "that is empty in our database. SME review: a Tier 3-5 source filling an empty field, or a "
            "Tier 1-2 source with an exception - the same page and facility name are cited for another "
            "of our sites, the source places the facility more than 5 km from our site, or it says the "
            "site is Not a Waste Facility. not used: it confirms or would change a value we hold, its "
            "link is dead, or it could not be converted or mapped. status_reason says why. A status is "
            "decided for a site and a source together, and review_id names that pair, so every value "
            "a source gives one site has the same status. source_coordinates is where the source "
            "places the facility (coordinates_from says from what), and distance_km how far that is "
            "from our site."
        ),
        "sme_action": "Read only. SME decisions are not recorded in this workbook yet.",
        "editable": "nothing",
    },
    {
        "tab_name": "(file) new_facility_candidates.csv",
        "purpose": "Landfills none of our sites is, as SMEs have marked them. For ingestion.",
        "sme_action": "Read only.",
        "editable": "nothing",
    },
    {
        "tab_name": "(file) possible_duplicates.csv",
        "purpose": (
            "Pairs of our sites whose searches cited the same page under the same facility name: perhaps "
            "one facility recorded twice, or one search claiming another site's source. Their values are "
            "sent to SME review. For ingestion."
        ),
        "sme_action": "Read only.",
        "editable": "nothing",
    },
    {
        "tab_name": "(file) datasets_found.csv",
        "purpose": (
            "Pages that look like datasets, such as a regulator's list of its landfills: naming or placing "
            "2 or more facilities, or from a publisher of datasets and found by 3 or more searches. A search "
            "reads only the entry it used, so ingestion can load the rest of the page once, as a source of its own."
        ),
        "sme_action": "Read only.",
        "editable": "nothing",
    },
    {
        "tab_name": "Parse_Warnings",
        "purpose": (
            "Values the pipeline dropped or could not map, including unit and enum problems, and "
            "citations of the two map sites that are never used as sources."
        ),
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

# Each found value's row is shaded by its status, so the tab reads at a glance.
STATUS_FILLS = {
    AUTO_APPROVED: "E2EFDA",
    SME_REVIEW: "FFF2CC",
    NOT_USED: "EDEDED",
}


def add_definitions(ws) -> None:
    """One column per vocabulary, used as the source for the dropdowns."""
    names = [
        "review_decision",
        "rejection_reason",
        "resolution",
        "validation_status",
        "source_tier",
        "facility_status",
        "facility_type",
        "cover_type",
        "confidence_score",
        "bulk_waste_type",
        "waste_depth",
        "true_false",
        "gccs_energy_project_type",
        "gccs_current_project_status",
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

    A dropdown on a pipeline-set column (status, tiers) reads as an invitation to change it.
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


# A quote_on_page or location_on_page result saying the source's own page doesn't bear it out.
NOT_ON_PAGE_FILL = "F8CBAD"


def add_conditionals(ws) -> None:
    """Shade each row by its status, and a page check the source's page doesn't bear out."""
    headers = {ws.cell(1, c).value: c for c in range(1, ws.max_column + 1)}
    last = max(ws.max_row, 2)
    for header in ("quote_on_page", "location_on_page"):
        if header not in headers:
            continue
        # Added first, so on these columns they win over the row's status colour.
        letter = get_column_letter(headers[header])
        ws.conditional_formatting.add(
            f"{letter}2:{letter}{last}",
            FormulaRule(
                formula=[f'OR(${letter}2="{NOT_FOUND}",${letter}2="{PARTLY}")'],
                fill=PatternFill("solid", fgColor=NOT_ON_PAGE_FILL),
            ),
        )
    if "status" not in headers:
        return
    letter = get_column_letter(headers["status"])
    span = f"A2:{get_column_letter(ws.max_column)}{last}"
    for value, color in STATUS_FILLS.items():
        ws.conditional_formatting.add(
            span,
            FormulaRule(formula=['$%s2="%s"' % (letter, value)], fill=PatternFill("solid", fgColor=color)),
        )


def write_review_workbook(
    path: Path,
    *,
    run_config: list[dict[str, Any]],
    found_values: list[dict[str, Any]],
    parse_warnings: list[dict[str, Any]],
) -> Path:
    """The SME-facing artifact. Sources stay a CSV sidecar for audit, and the lists for ingestion
    too: nothing in them requires a decision.
    """
    wb = Workbook()
    wb.remove(wb.active)

    ws_readme = wb.create_sheet("ReadMe")
    write_sheet(ws_readme, ["tab_name", "purpose", "sme_action", "editable"], REVIEW_INSTRUCTIONS)

    ws_config = wb.create_sheet("Run_Config")
    write_sheet(ws_config, ["setting", "value"], run_config)

    ws_found = wb.create_sheet("Found_Values")
    write_sheet(ws_found, FOUND_VALUES_HEADERS, found_values)

    ws_warnings = wb.create_sheet("Parse_Warnings")
    write_sheet(ws_warnings, PARSE_WARNING_HEADERS, parse_warnings)

    ws_defs = wb.create_sheet("Definitions")
    add_definitions(ws_defs)

    add_validations(wb, ws_found)
    add_conditionals(ws_found)

    path.parent.mkdir(parents=True, exist_ok=True)
    wb.save(path)
    return path
