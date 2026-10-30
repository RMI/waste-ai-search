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
    BOOLEAN_TARGET_ATTRIBUTES,
    DEFINITION_VALUES,
    FIELD_TO_DEFINITION,
    JUNK,
    NEW_SITE,
    PARSE_WARNING_HEADERS,
    SITE_REVIEW_EDITABLE,
    SITE_REVIEW_HEADERS,
)
from .site_review import (
    CHECK_BELOW,
    FAR_AWAY_FLAG,
    KIND,
    NOT_A_FACILITY_FLAG,
    PLANNED_FLAG,
    SITE_REVIEW_SHEET,
    VALUE_ROW,
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


# What a value row's source_url cell shows: its link opens the page at the value's quote.
QUOTE_LINK_TEXT = "open the page at the quote"
# A text fragment names the start and end of a long quote, so a quote the page wraps or spaces
# differently in its middle is still found.
FRAGMENT_WORDS = 4


def quote_link(url: Any, passage: Any) -> str | None:
    """A link that opens the page scrolled to the quote, highlighted: a text fragment
    (#:~:text=start,end), which Chrome and Edge follow. A browser that doesn't, or a page the quote
    isn't on, opens the page as usual. A PDF ignores it, so it gets the plain link."""
    target = hyperlink_target(url)
    words = str(passage or "").split()
    if target is None or not words or urlsplit(target).path.lower().endswith(".pdf"):
        return target

    def part(text: str) -> str:
        # "-", "," and "&" mean something in a text fragment, so they are escaped too.
        return quote(text, safe="").replace("-", "%2D")

    if len(words) <= 2 * FRAGMENT_WORDS:
        fragment = part(" ".join(words))
    else:
        fragment = f"{part(' '.join(words[:FRAGMENT_WORDS]))},{part(' '.join(words[-FRAGMENT_WORDS:]))}"
    base, _hash, existing = target.partition("#")
    linked = f"{base}#{existing}:~:text={fragment}"
    return linked if len(linked) <= MAX_HYPERLINK_LENGTH else target


def map_link(point: Any) -> str | None:
    """Google Maps at a "lat, lon" point."""
    text = str(point or "").replace(" ", "")
    if not text or text.count(",") != 1:
        return None
    return f"https://www.google.com/maps/search/?api=1&query={quote(text, safe=',.-')}"


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
            "YOUR WORKLIST. Each source row is a web page the search found for one of our sites, whose "
            "values would fill fields that are empty in our database for that site. The question for "
            "each is whether the source is about this site of ours. Under a source of Tier 3-5 (news, "
            "trade press, NGO reports, directories, blogs, or a page whose kind is unclear) come its "
            "value rows (marked ↳): key values you also check against the page - site type, closure "
            "year, incoming waste, area, gas collection, gas use and collection efficiency. A Tier 1-2 "
            "source (a regulator, permit or operator; government statistics, peer-reviewed or "
            "intergovernmental data) is trusted to have been read correctly, so it has no value rows. "
            "Rows are grouped by our site, with flagged sources first. Your decisions are read back "
            "from this tab."
        ),
        "sme_action": (
            "Work top to bottom. Click the - and + at the left of the sheet to fold value rows away or "
            "open them.\n"
            "FOR EACH SOURCE ROW:\n"
            "1. Our site: site_name is our database's name for it, machine-translated into English; "
            "original_site_name is the same name before translation. Click our_coordinates to see "
            "where our database puts it on Google Maps.\n"
            "2. The source: open source_url. facility_name_in_source, source_address and "
            "stated_coordinates are the facility it describes, as the source writes them; "
            "facility_name_english is that name in English.\n"
            "3. Click source_coordinates to see where the source places the facility. coordinates_from "
            "says what they come from: coordinates the source states, a grid reference, or its "
            "address, placed on the building or only on the street (a street point can be a kilometre "
            "or more off). distance_km is how far that is from our site. Where source_coordinates is "
            "blank but the source states a location, location_note says why we couldn't place it.\n"
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
            "- \"a Tier N source here gives ...\": another source on this tab gives a different value "
            "for the same field. One value loads per field, the best tier first; the flag says which "
            "loads if both sources are approved.\n"
            "5. would_load is what approving loads for this site of ours: attribute = value, with the "
            f"year for a yearly attribute. A line marked {CHECK_BELOW} loads only once you mark its "
            "value row Correct.\n"
            "6. Pick decision:\n"
            "- Correct site: the source is about this site of ours. Its values load here.\n"
            "- Different site: it is about another site of ours. Put that site's id in other_site_id, "
            "and its values load there, where that site's fields are empty. A Different site with no "
            "other_site_id can't be read back: if you can't tell which of our sites it is about, leave "
            "decision blank and say so in notes.\n"
            "- New site: it is about a landfill that isn't in our database. Nothing loads, and it is "
            "listed for ingestion as a new facility.\n"
            "- Junk: the source is no use - it isn't about one facility, or you can't tell which. "
            "Nothing loads, and its value rows turn grey: skip them.\n"
            "If a Tier 1-2 source misreads a value, say so in notes; its values still load.\n"
            "FOR EACH VALUE ROW (↳) under a source that is Correct site or Different site:\n"
            "7. would_load is the value; evidence is the quote from the page, the value as the page "
            "writes it, and the search's note. source_url opens the page at the quote, highlighted "
            "(Chrome and Edge; not PDFs).\n"
            f"- \"{PLANNED_FLAG}\": the source gives a closure that hasn't happened yet. Mark it "
            "Correct to keep it.\n"
            "8. Pick decision: Correct if the page bears the value out, Wrong if not. If it is Wrong and "
            "you know the right value, type it in corrected_value, in the unit would_load shows "
            "(tonnes a year, square metres, a year, or a fraction from 0 to 1 for efficiency); site "
            "type, gas collection and gas use offer a list. It loads instead. corrected_value is "
            "optional: Wrong with it blank loads nothing.\n"
            "Nothing from a row loads while its decision is blank.\n"
            "9. Put your name in reviewer, the date in reviewed_date, and anything worth knowing in "
            "notes.\n"
            "Leave review_id and check as they are: your decisions are read back by them.\n"
            "An empty tab means nothing needs you."
        ),
        "editable": f"{', '.join(SITE_REVIEW_EDITABLE)} - nothing else",
    },
    {
        "tab_name": "(file) found_values.csv",
        "purpose": (
            "Every value the search found, one row per site, year, source and attribute. Every source "
            "is filed under the site that was searched, and status says what becomes of each value. "
            "SME review: it fills a field that is empty in our database, and loads once its source is "
            "approved on Site_Review - a key value from a Tier 3-5 source (value_check required) only "
            "once it is marked Correct too. not used: it confirms or would change a value we hold, its "
            "link is dead, or it could not be converted or mapped. status_reason says why. review_id "
            "names the site-source pair, as on Site_Review. Once your decisions are read back, "
            "sme_decision gives the source's decision, value_decision and corrected_value a value "
            "row's, and loaded says whether the value loads: yes, or no where another value was picked "
            "for the same site, attribute and year."
        ),
        "sme_action": "Read only. Open it to see each value's quote, and why it has its status.",
        "editable": "nothing",
    },
    {
        "tab_name": "(file) accepted_values.csv",
        "purpose": (
            "What loads into our database, once your decisions are read back: one value per site, "
            "attribute and, for a yearly attribute, year, from the sources you approve. Where approved "
            "values disagree, the best tier wins, then the newest, then the value most sources give. "
            "approved_by is the reviewer. For ingestion."
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
            "one facility recorded twice, or one search claiming another site's source, which reason "
            "says is likelier where the sites are more than 5 km apart. A page cited with no name is "
            "left out. The page is sent to Site_Review under each site. For ingestion."
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
        "value_decision",
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


def definition_range(wb, name: str) -> str:
    """The Definitions cells listing one vocabulary, as a dropdown's source."""
    def_sheet = wb["Definitions"]
    columns = {def_sheet.cell(1, c).value: c for c in range(1, def_sheet.max_column + 1)}
    letter = get_column_letter(columns[name])
    return f"=Definitions!${letter}$2:${letter}${len(DEFINITION_VALUES[name]) + 1}"


def dropdown(wb, name: str, header: str, strict: bool = True) -> DataValidation:
    """A dropdown of one Definitions vocabulary. A strict one accepts only a listed choice, since a
    decision is read back as typed."""
    return DataValidation(
        type="list", formula1=definition_range(wb, name), allow_blank=True, showDropDown=False,
        showErrorMessage=strict, errorTitle=f"Pick a {header}",
        error=f"Pick one of: {', '.join(DEFINITION_VALUES[name])}.",
    )


# The vocabulary a value row's corrected_value offers, by attribute. A list attribute (gas use) can
# take several, so its dropdown also takes what is typed.
CORRECTION_LISTS = {"facility_type": "facility_type", "gccs_energy_project_type": "gccs_energy_project_type"}
LOOSE_LISTS = {"gccs_energy_project_type"}


def add_validations(wb, ws, kinds: list[str] | None = None, attributes: list[str] | None = None) -> None:
    """Attach dropdowns to the editable review columns only.

    A dropdown on a pipeline-set column (tiers, flags) reads as an invitation to change it. On
    Site_Review, `kinds` says what each data row is: a source row's decision offers the source
    decisions, a value row's Correct or Wrong, and a value row's corrected_value the attribute's own
    allowed values, by `attributes`.
    """
    headers = {ws.cell(1, c).value: c for c in range(1, ws.max_column + 1)}
    last_row = max(ws.max_row, 2)
    if kinds is None:
        for header, definition_name in FIELD_TO_DEFINITION.items():
            if header in headers:
                validation = dropdown(wb, definition_name, header)
                ws.add_data_validation(validation)
                target = get_column_letter(headers[header])
                validation.add(f"{target}2:{target}{last_row}")
        return

    decision = get_column_letter(headers["decision"])
    corrected = get_column_letter(headers["corrected_value"])
    by_list: dict[tuple[str, str], list[str]] = {}
    for offset, kind in enumerate(kinds):
        row = offset + 2
        if kind == VALUE_ROW:
            by_list.setdefault(("value_decision", "decision"), []).append(f"{decision}{row}")
            attribute = (attributes or [])[offset]
            name = CORRECTION_LISTS.get(attribute) or ("true_false" if attribute in BOOLEAN_TARGET_ATTRIBUTES else "")
            if name:
                by_list.setdefault((name, "corrected_value"), []).append(f"{corrected}{row}")
        else:
            by_list.setdefault(("decision", "decision"), []).append(f"{decision}{row}")
    for (name, header), cells in by_list.items():
        validation = dropdown(wb, name, header, strict=name not in LOOSE_LISTS)
        ws.add_data_validation(validation)
        for cell_range in runs(cells):
            validation.add(cell_range)


def runs(cells: list[str]) -> list[str]:
    """Cells of one column, in row order, as ranges of consecutive rows: ["W2", "W3", "W5"] is
    ["W2:W3", "W5"]."""
    found: list[list[tuple[str, int]]] = []
    for cell in cells:
        column, row = cell.rstrip("0123456789"), int(cell[len(cell.rstrip("0123456789")):])
        if found and found[-1][-1] == (column, row - 1):
            found[-1].append((column, row))
        else:
            found.append([(column, row)])
    return [f"{run[0][0]}{run[0][1]}" + (f":{run[-1][0]}{run[-1][1]}" if len(run) > 1 else "") for run in found]


# A flags cell with anything in it: a reason to look at the row twice.
FLAG_FILL = "F8CBAD"
# A value row: pale, so it reads as part of the source above it.
VALUE_ROW_FILL = "EEF3F8"
# A value row whose source is Junk or New site: nothing on it loads.
IGNORED_FONT = "A6A6A6"
IGNORED_FILL = "EDEDED"
# The rule drawn above the first pair of each of our sites, so a site's pairs read as one group.
SITE_RULE = Border(top=Side(style="medium", color="1F4E78"))


def add_conditionals(ws) -> None:
    """Shade a flags cell that has a flag in it, and grey out a value row once its source is marked
    Junk or New site."""
    headers = {ws.cell(1, c).value: c for c in range(1, ws.max_column + 1)}
    if "flags" not in headers:
        return
    last = max(ws.max_row, 2)
    # The grey comes first, so a value row nobody needs to check loses its flag shading too.
    if {"review_id", "check", "decision"} <= set(headers):
        review, check, decision = (get_column_letter(headers[name]) for name in ("review_id", "check", "decision"))
        source_decided = (
            f'COUNTIFS(${review}$2:${review}${last},${review}2,${check}$2:${check}${last},"",'
            f'${decision}$2:${decision}${last},"{{}}")'
        )
        ignored = f'AND(${check}2<>"",{source_decided.format(JUNK)}+{source_decided.format(NEW_SITE)}>0)'
        ws.conditional_formatting.add(
            f"A2:{get_column_letter(ws.max_column)}{last}",
            FormulaRule(
                formula=[ignored], stopIfTrue=True,
                font=Font(color=IGNORED_FONT), fill=PatternFill("solid", fgColor=IGNORED_FILL),
            ),
        )
    letter = get_column_letter(headers["flags"])
    ws.conditional_formatting.add(
        f"{letter}2:{letter}{last}",
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


def lay_out_site_review(ws, records: list[dict[str, Any]]) -> None:
    """Site_Review's own layout: our site's id and name stay in view; a value row is shaded, folds
    under its source, and links to the page at its quote; coordinates link to Google Maps."""
    headers = {ws.cell(1, c).value: c for c in range(1, ws.max_column + 1)}
    ws.freeze_panes = "C2"
    ws.sheet_properties.outlinePr.summaryBelow = False
    fill = PatternFill("solid", fgColor=VALUE_ROW_FILL)
    for offset, record in enumerate(records):
        row = offset + 2
        for header in ("our_coordinates", "source_coordinates"):
            cell = ws.cell(row, headers[header])
            target = map_link(cell.value)
            if target:
                cell.hyperlink = target
                cell.style = "Hyperlink"
        if record.get(KIND) != VALUE_ROW:
            continue
        ws.row_dimensions[row].outlineLevel = 1
        for c in range(1, ws.max_column + 1):
            ws.cell(row, c).fill = fill
        title = ws.cell(row, headers["source_title"])
        title.alignment = Alignment(vertical="top", wrap_text=True, indent=1)
        link = ws.cell(row, headers["source_url"])
        target = quote_link(record.get("source_url"), record.get("quote"))
        link.value = QUOTE_LINK_TEXT if target else link.value
        if target:
            link.hyperlink = target
            link.style = "Hyperlink"
            link.fill = fill


def write_review_workbook(
    path: Path,
    *,
    run_config: list[dict[str, Any]],
    site_review: list[dict[str, Any]],
    parse_warnings: list[dict[str, Any]],
) -> Path:
    """The SME-facing artifact: the site-source pairs an SME decides, and the key values under them
    (site_review.py). Every value
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

    add_validations(
        wb, ws_review,
        kinds=[record.get(KIND, "") for record in site_review],
        attributes=[record.get("_attribute", "") for record in site_review],
    )
    add_conditionals(ws_review)
    mark_sites(ws_review)
    lay_out_site_review(ws_review, site_review)

    path.parent.mkdir(parents=True, exist_ok=True)
    wb.save(path)
    return path
