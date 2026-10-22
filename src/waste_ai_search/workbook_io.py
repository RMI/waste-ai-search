from __future__ import annotations

from datetime import date
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlsplit, urlunsplit

from openpyxl import Workbook
from openpyxl.cell.cell import ILLEGAL_CHARACTERS_RE
from openpyxl.formatting.rule import FormulaRule
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.worksheet.datavalidation import DataValidation
from openpyxl.utils import get_column_letter

from .schema import (
    DEFINITION_VALUES,
    FIELD_TO_DEFINITION,
    PARSE_WARNING_HEADERS,
    SITE_REVIEW_EDITABLE,
    SITE_REVIEW_HEADERS,
)
from .site_review import FAR_AWAY_FLAG, NOT_A_FACILITY_FLAG, SITE_REVIEW_SHEET


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
        "tab_name": SITE_REVIEW_SHEET,
        "purpose": (
            "YOUR WORKLIST. Each row is a source the search found for one of our sites, whose values "
            "would fill fields that are empty in our database for that site. The question for each row "
            "is whether the source is about this site of ours. Rows are grouped by our site, with "
            "flagged sources first. A Tier 1-2 source (a regulator, permit or operator; government "
            "statistics, peer-reviewed or intergovernmental data) is approved without review and isn't "
            "listed, unless the same page and name are cited for another site of ours, it places the "
            "facility more than 5 km from ours, or it says the site is Not a Waste Facility. Your "
            "decisions are read back from this tab."
        ),
        "sme_action": (
            "Work top to bottom, one row at a time.\n"
            "1. Our site: site_name is our database's name for it, machine-translated into English; "
            "original_site_name is the same name before translation. Paste our_coordinates into Google "
            "Maps to see where our database puts it.\n"
            "2. The source: open source_url. facility_name_in_source, source_address and "
            "stated_coordinates are the facility it describes, as the source writes them; "
            "facility_name_english is that name in English.\n"
            "3. Paste source_coordinates, where the source places the facility, into Google Maps beside "
            "our_coordinates. coordinates_from says what they come from: coordinates the source states, "
            "a grid reference, or its address, placed on the building or only on the street (a street "
            "point can be a kilometre or more off). distance_km is how far that is from our site. Where "
            "source_coordinates is blank but the source states a location, location_note says why we "
            "couldn't place it - too coarse, or its address and coordinates disagree - and distance_km, "
            "if filled, is roughly how far it is.\n"
            "4. Read flags, shaded where there are any:\n"
            "- \"not found\" or \"only partly found on the page\": our automated check couldn't find "
            "the quote, or the address or coordinates, on the source's page. Look yourself: the check "
            "can't read images or maps, and it makes mistakes.\n"
            "- \"same page and name also cited for site\": searches for other sites of ours cited this "
            "page under the same facility name. It is about one of them at most, unless they are one "
            "facility recorded twice.\n"
            f"- \"{FAR_AWAY_FLAG}\": where the source places the facility, as in step 3.\n"
            f"- \"{NOT_A_FACILITY_FLAG}\": approving loads that verdict. Check the source says the "
            "site was never a waste site: a closed landfill still is one.\n"
            "- \"an auto-approved source gives ..., which loads instead\": a source approved "
            "automatically, which isn't on this tab, gives a different value for that field, and its "
            "better tier wins, so approving this row won't load that line. \"approving replaces ...\" "
            "is the other way round. If you think this row's value is right, say so in notes.\n"
            "5. would_load is what approving loads for this site of ours: attribute = value, with the "
            "year for a yearly attribute. One value loads per site, attribute and year, the best tier "
            "first, so a line can lose to a better value approved for the same field; the flags say "
            "where an auto-approved one would.\n"
            "6. Pick decision:\n"
            "- Correct site: the source is about this site of ours. Its values load here.\n"
            "- Different site: it is about another site of ours. Put that site's id in other_site_id, "
            "and its values load there, where that site's fields are empty. A Different site with no "
            "other_site_id can't be read back: if you can't tell which of our sites it is about, leave "
            "decision blank and say so in notes.\n"
            "- New site: it is about a landfill that isn't in our database. Nothing loads, and it is "
            "listed for ingestion as a new facility.\n"
            "- Junk: the source is no use - it isn't about one facility, or you can't tell which. "
            "Nothing loads.\n"
            "Nothing from a row loads while its decision is blank.\n"
            "7. Put your name in reviewer, the date in reviewed_date, and anything worth knowing in "
            "notes.\n"
            "An empty tab means nothing needs you."
        ),
        "editable": f"{', '.join(SITE_REVIEW_EDITABLE)} - nothing else",
    },
    {
        "tab_name": "(file) found_values.csv",
        "purpose": (
            "Every value the search found, one row per site, year, source and attribute. Every source "
            "is filed under the site that was searched, and status says what becomes of each value. "
            "auto-approved: a Tier 1-2 source filling a field that is empty in our database. SME "
            "review: a Tier 3-5 source filling an empty field, or a Tier 1-2 source with an exception - "
            "the same page and facility name are cited for another of our sites, the source places the "
            "facility more than 5 km from our site, or it says the site is Not a Waste Facility. These "
            "load once the source is approved on Site_Review. not used: it confirms or would change a "
            "value we hold, its link is dead, or it could not be converted or mapped. status_reason "
            "says why. review_id names the site-source pair, as on Site_Review. Once your decisions "
            "are read back, sme_decision gives the pair's decision, and loaded says whether the value "
            "loads: yes, or no where another value was picked for the same site, attribute and year."
        ),
        "sme_action": "Read only. Open it to see each value's quote, and why it has its status.",
        "editable": "nothing",
    },
    {
        "tab_name": "(file) accepted_values.csv",
        "purpose": (
            "What loads into our database, once your decisions are read back: one value per site, "
            "attribute and, for a yearly attribute, year, from the auto-approved sources and those you "
            "approve. Where approved values disagree, the best tier wins, then the newest, then the "
            "value most sources give. approved_by is auto, or the reviewer. For ingestion."
        ),
        "sme_action": "Read only.",
        "editable": "nothing",
    },
    {
        "tab_name": "(file) new_facility_candidates.csv",
        "purpose": "Landfills none of our sites is, from the sources SMEs mark New site on Site_Review. For ingestion.",
        "sme_action": "Read only.",
        "editable": "nothing",
    },
    {
        "tab_name": "(file) possible_duplicates.csv",
        "purpose": (
            "Pairs of our sites whose searches cited the same page under the same facility name: perhaps "
            "one facility recorded twice, or one search claiming another site's source. The page is sent "
            "to Site_Review under each site. For ingestion."
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
        "purpose": "The decisions Site_Review offers, the tiers, and allowed values, mirrored from the live database enums.",
        "sme_action": "Read only.",
        "editable": "nothing",
    },
]


def add_definitions(ws) -> None:
    """One column per vocabulary, used as the source for the dropdowns."""
    names = [
        "decision",
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

    A dropdown on a pipeline-set column (tiers, flags) reads as an invitation to change it. Only a
    listed choice is accepted, since a decision is read back as typed.
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
        validation = DataValidation(
            type="list", formula1=formula, allow_blank=True, showDropDown=False, showErrorMessage=True,
            errorTitle=f"Pick a {header}", error=f"Pick one of: {', '.join(DEFINITION_VALUES[definition_name])}.",
        )
        ws.add_data_validation(validation)
        target = get_column_letter(headers[header])
        validation.add(f"{target}2:{target}{last_row}")


# A flags cell with anything in it: a reason to look at the pair twice.
FLAG_FILL = "F8CBAD"
# The rule drawn above the first pair of each of our sites, so a site's pairs read as one group.
SITE_RULE = Border(top=Side(style="medium", color="1F4E78"))


def add_conditionals(ws) -> None:
    """Shade a flags cell that has a flag in it."""
    headers = {ws.cell(1, c).value: c for c in range(1, ws.max_column + 1)}
    if "flags" not in headers:
        return
    letter = get_column_letter(headers["flags"])
    ws.conditional_formatting.add(
        f"{letter}2:{letter}{max(ws.max_row, 2)}",
        FormulaRule(formula=[f'LEN(${letter}2)>0'], fill=PatternFill("solid", fgColor=FLAG_FILL)),
    )


def mark_sites(ws) -> None:
    """Draw a rule above each of our sites' first row after the first."""
    headers = {ws.cell(1, c).value: c for c in range(1, ws.max_column + 1)}
    if "site_id" not in headers:
        return
    previous = None
    for r in range(2, ws.max_row + 1):
        site_id = ws.cell(r, headers["site_id"]).value
        if previous is not None and site_id != previous:
            for c in range(1, ws.max_column + 1):
                ws.cell(r, c).border = SITE_RULE
        previous = site_id


def write_review_workbook(
    path: Path,
    *,
    run_config: list[dict[str, Any]],
    site_review: list[dict[str, Any]],
    parse_warnings: list[dict[str, Any]],
) -> Path:
    """The SME-facing artifact: the site-source pairs an SME decides (site_review.py). Every value
    found, the sources and the lists for ingestion stay CSV sidecars: nothing in them requires a
    decision.
    """
    wb = Workbook()
    wb.remove(wb.active)

    ws_readme = wb.create_sheet("ReadMe")
    write_sheet(ws_readme, ["tab_name", "purpose", "sme_action", "editable"], REVIEW_INSTRUCTIONS)

    ws_config = wb.create_sheet("Run_Config")
    write_sheet(ws_config, ["setting", "value"], run_config)

    ws_review = wb.create_sheet(SITE_REVIEW_SHEET)
    write_sheet(ws_review, SITE_REVIEW_HEADERS, site_review)

    ws_warnings = wb.create_sheet("Parse_Warnings")
    write_sheet(ws_warnings, PARSE_WARNING_HEADERS, parse_warnings)

    ws_defs = wb.create_sheet("Definitions")
    add_definitions(ws_defs)

    add_validations(wb, ws_review)
    add_conditionals(ws_review)
    mark_sites(ws_review)

    path.parent.mkdir(parents=True, exist_ok=True)
    wb.save(path)
    return path
