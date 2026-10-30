"""The workbook is the only file an SME opens, so its affordances must match the instructions.

Since step 3 of the approve-sources plan its worklist is the Site_Review tab: one row per site-source
pair an SME decides, grouped by our site, and since the key-values plan, under a Tier 3-5 pair a row
per key value an SME checks against the page. It replaces the Review_Queue and Leads tabs, whose tests
(the validation_status, rejection_reason, corrected_value and corrected_site_id dropdowns, and the
match column) went in step 2, and step 2's read-only Found_Values tab, whose rows are in
found_values.csv. Which pairs are listed, and their flags, are tested in test_site_review.py.
"""
import zipfile

from openpyxl import load_workbook
from openpyxl.utils import get_column_letter

from waste_ai_search.schema import (
    FACILITY_TYPE_ALLOWED,
    JUNK,
    SITE_REVIEW_DECISIONS,
    SITE_REVIEW_EDITABLE,
    SITE_REVIEW_HEADERS,
    VALUE_DECISIONS,
)
from waste_ai_search.site_review import FAR_AWAY_FLAG, KIND, SOURCE_ROW, VALUE_ROW
from waste_ai_search.workbook_io import FLAG_FILL, IGNORED_FILL, QUOTE_LINK_TEXT, VALUE_ROW_FILL, quote_link, write_review_workbook


def review_row(site_id, url="https://a.example/permit", flags="", **extra):
    return {h: "" for h in SITE_REVIEW_HEADERS} | {
        KIND: SOURCE_ROW, "review_id": f"WAIREV-{site_id}-{url}", "site_id": site_id,
        "source_url": url, "flags": flags, "would_load": "operator = City", **extra,
    }


def value_row(source, attribute, quote="The landfill closed in 2008 after 30 years of service", **extra):
    return {h: "" for h in SITE_REVIEW_HEADERS} | {
        KIND: VALUE_ROW, "review_id": source["review_id"], "site_id": source["site_id"], "check": attribute,
        "source_url": source["source_url"], "source_title": f"↳ check {attribute}", "quote": quote,
        "_attribute": attribute, "would_load": f"{attribute} = 2008", **extra,
    }


ROWS = [
    review_row("900", flags=FAR_AWAY_FLAG),
    review_row("900", url="https://news.example/a page"),
    review_row("13413", url="javascript:alert(1)"),
]


def build(tmp_path, rows=ROWS):
    path = write_review_workbook(
        tmp_path / "r_review.xlsx",
        run_config=[{"setting": "run_id", "value": "r"}],
        site_review=rows,
        parse_warnings=[],
    )
    return load_workbook(path), path


def letters(ws):
    return {ws.cell(1, c).value: get_column_letter(c) for c in range(1, ws.max_column + 1)}


def test_site_review_replaces_the_review_queue_the_leads_and_found_values(tmp_path):
    wb, _ = build(tmp_path)
    assert wb.sheetnames == ["ReadMe", "Run_Config", "Site_Review", "Parse_Warnings", "Definitions"]
    ws = wb["Site_Review"]
    assert [cell.value for cell in ws[1]] == SITE_REVIEW_HEADERS
    column = letters(ws)["site_id"]
    assert [ws[f"{column}{r}"].value for r in range(2, ws.max_row + 1)] == ["900", "900", "13413"]


def test_the_columns_go_from_our_site_to_the_source_to_the_smes_decision():
    """Our site, then the source, what it says, the flags, what approving would load and the evidence,
    then the six columns an SME fills in, and last the two a decision is read back by."""
    assert SITE_REVIEW_HEADERS[:7] == [
        "site_id", "site_name", "original_site_name", "source_language", "country_iso3", "our_coordinates", "our_place",
    ]
    for group in (
        ["source_url", "source_title", "publisher", "source_tier", "link_status"],
        # As the source writes them, then what we read from them, and why not where we couldn't.
        ["facility_name_in_source", "facility_name_english", "source_address", "source_place", "stated_coordinates",
         "source_coordinates", "coordinates_from", "distance_km", "location_note"],
        ["flags", "would_load", "evidence"],
    ):
        start = SITE_REVIEW_HEADERS.index(group[0])
        assert SITE_REVIEW_HEADERS[start:start + len(group)] == group
    assert SITE_REVIEW_HEADERS[-8:-2] == SITE_REVIEW_EDITABLE == [
        "decision", "other_site_id", "corrected_value", "reviewer", "reviewed_date", "notes",
    ]
    assert SITE_REVIEW_HEADERS[-2:] == ["review_id", "check"]


def test_decision_is_the_only_dropdown_and_offers_the_four_decisions(tmp_path):
    """A dropdown on a pipeline-set column reads as permission to change it; a decision is read back
    as typed, so only a listed one is accepted."""
    wb, _ = build(tmp_path)
    ws = wb["Site_Review"]
    [validation] = ws.data_validations.dataValidation
    column = letters(ws)["decision"]
    assert str(validation.sqref) == f"{column}2:{column}4"
    assert validation.showErrorMessage and validation.type == "list"

    defs = list(wb["Definitions"].values)
    index = defs[0].index("decision")
    assert [row[index] for row in defs[1:] if row[index]] == SITE_REVIEW_DECISIONS == ["Correct site", "Different site", "New site", "Junk"]
    definitions_column = get_column_letter(index + 1)
    assert validation.formula1 == f"=Definitions!${definitions_column}$2:${definitions_column}$5"


def test_the_readme_says_which_columns_an_sme_may_edit(tmp_path):
    wb, _ = build(tmp_path)
    readme = {row[0]: row for row in list(wb["ReadMe"].values)[1:]}
    assert readme["Site_Review"][3] == "decision, other_site_id, corrected_value, reviewer, reviewed_date, notes - nothing else"
    assert {row[3] for name, row in readme.items() if name != "Site_Review"} == {"nothing"}


def test_the_source_url_is_a_safe_hyperlink(tmp_path):
    """The cell shows the link as the source gave it; the target is encoded, and only http(s) links."""
    _, path = build(tmp_path)
    ws = load_workbook(path)["Site_Review"]
    column = letters(ws)["source_url"]
    assert ws[f"{column}2"].hyperlink.target == "https://a.example/permit"
    assert ws[f"{column}3"].value == "https://news.example/a page"
    assert ws[f"{column}3"].hyperlink.target == "https://news.example/a%20page"
    assert ws[f"{column}4"].hyperlink is None and ws[f"{column}4"].value == "javascript:alert(1)"
    with zipfile.ZipFile(path) as z:
        assert not any("javascript" in z.read(name).decode() for name in z.namelist() if "_rels" in name)


def test_a_flags_cell_with_a_flag_in_it_is_shaded(tmp_path):
    wb, _ = build(tmp_path)
    ws = wb["Site_Review"]
    flags = letters(ws)["flags"]
    [(cells, [rule])] = [(str(ranges.sqref), ranges.rules) for ranges in ws.conditional_formatting if "LEN" in ranges.rules[0].formula[0]]
    assert cells == f"{flags}2:{flags}4"
    assert rule.formula == [f"LEN(${flags}2)>0"]
    assert rule.dxf.fill.fgColor.rgb.endswith(FLAG_FILL)


def test_a_rule_is_drawn_above_each_of_our_sites_after_the_first(tmp_path):
    ws = build(tmp_path)[0]["Site_Review"]
    ruled = [r for r in range(2, ws.max_row + 1) if ws.cell(r, 1).border.top.style]
    assert ruled == [4]
    assert all(ws.cell(4, c).border.top.style == "medium" for c in range(1, ws.max_column + 1))


def test_an_empty_worklist_still_writes_the_tab(tmp_path):
    wb, _ = build(tmp_path, rows=[])
    ws = wb["Site_Review"]
    assert ws.max_row == 1 and [cell.value for cell in ws[1]] == SITE_REVIEW_HEADERS
    readme = {row[0]: row[2] for row in list(wb["ReadMe"].values)[1:]}
    assert "If this tab is empty, there's nothing to review." in readme["Site_Review"]


def test_definitions_tab_publishes_the_tier_scale(tmp_path):
    defs = build(tmp_path)[0]["Definitions"]
    rows = list(defs.values)
    index = rows[0].index("source_tier")
    assert [r[index] for r in rows[1:] if r[index]] == ["Tier 1", "Tier 2", "Tier 3", "Tier 4", "Tier 5"]
    assert not {"review_decision", "rejection_reason", "validation_status", "resolution"} & set(rows[0])


def test_readme_tab_documents_every_tab_and_the_csv_sidecars(tmp_path):
    wb = build(tmp_path)[0]
    described = {row[0] for row in list(wb["ReadMe"].values)[1:]}
    for tab in wb.sheetnames:
        if tab != "ReadMe":
            assert tab in described or tab == "Run_Config"
    assert "(file) found_values.csv" in described
    assert not {"Review_Queue", "Leads", "Found_Values"} & described


def test_the_readme_walks_an_sme_through_a_row_in_plain_words(tmp_path):
    readme = {row[0]: row for row in list(build(tmp_path)[0]["ReadMe"].values)[1:]}
    purpose, steps = readme["Site_Review"][1], readme["Site_Review"][2]
    assert purpose.startswith("YOUR WORKLIST.")
    assert "whether the page is really about that site" in purpose
    numbered = [line.split(".")[0] for line in steps.splitlines() if line[:1].isdigit()]
    assert numbered == ["1", "2", "3", "4", "5", "6", "7", "8", "9"]
    for column in ("our_coordinates", "source_url", "source_coordinates", "coordinates_from", "distance_km",
                   "flags", "would_load", "evidence", *SITE_REVIEW_EDITABLE):
        assert column in steps, column
    for decision in SITE_REVIEW_DECISIONS:
        assert f"- {decision}:" in steps
    # accept-reviews stops on a Different site with no other_site_id, so an SME who can't tell which
    # of our sites leaves the decision blank.
    assert "If you can't tell which, leave decision blank" in steps
    # Short enough to be read.
    assert len(purpose.split()) + len(steps.split()) < 500


def test_the_readme_tells_smes_which_rows_they_can_skip(tmp_path):
    readme = {row[0]: row[2] for row in list(build(tmp_path)[0]["ReadMe"].values)[1:]}
    assert all("You can skip this." in action for tab, action in readme.items() if tab != "Site_Review")


def test_the_readme_says_what_each_status_means(tmp_path):
    purpose = {row[0]: row[1] for row in list(build(tmp_path)[0]["ReadMe"].values)[1:]}["(file) found_values.csv"]
    for words in ("SME review", "not used", "value_check", "review_id", "Site_Review", "corrected_value"):
        assert words in purpose


def test_the_readme_describes_the_lists_for_ingestion(tmp_path):
    described = {row[0]: row[1] for row in list(build(tmp_path)[0]["ReadMe"].values)[1:]}
    for name in ("accepted_values.csv", "new_facility_candidates.csv", "possible_duplicates.csv", "datasets_found.csv"):
        assert f"(file) {name}" in described
    assert "one value per site, attribute and, for a yearly attribute, year" in described["(file) accepted_values.csv"]
    assert "(file) outside_run_matches.csv" not in described
    assert "same page under the same facility name" in described["(file) possible_duplicates.csv"]
    assert "New site" in described["(file) new_facility_candidates.csv"]


def test_an_smes_decisions_in_the_sharepoint_copy_survive_publishing_again(tmp_path):
    """publish.py copies the workbook whole, so the Site_Review tab changes nothing there: a copy an
    SME has recorded decisions in is left as it is, and the re-arbitrated workbook goes beside it."""
    from waste_ai_search.publish import publish_review

    run_dir, sharepoint = tmp_path / "run", tmp_path / "sp"
    sharepoint.mkdir()
    write_review_workbook(run_dir / "r_review.xlsx", run_config=[], site_review=ROWS, parse_warnings=[])
    published = publish_review(run_dir, "r", target_dir=sharepoint)

    reviewed = load_workbook(published)
    ws = reviewed["Site_Review"]
    ws[f"{letters(ws)['decision']}2"] = "Correct site"
    ws[f"{letters(ws)['reviewer']}2"] = "An SME"
    reviewed.save(published)

    write_review_workbook(run_dir / "r_review.xlsx", run_config=[], site_review=ROWS[:1], parse_warnings=[])
    beside = publish_review(run_dir, "r", target_dir=sharepoint)
    assert beside != published and beside.parent == published.parent
    ws = load_workbook(published)["Site_Review"]
    assert (ws[f"{letters(ws)['decision']}2"].value, ws[f"{letters(ws)['reviewer']}2"].value) == ("Correct site", "An SME")
    assert load_workbook(beside)["Site_Review"].max_row == 2


# --- value rows ------------------------------------------------------------------------------------
VALUE_ROWS = [
    news := review_row("13413", url="https://news.example/landfill"),
    value_row(news, "closing_year"),
    value_row(news, "facility_type"),
    review_row("13413", url="https://news.example/other.pdf"),
]


def test_a_value_row_is_shaded_folds_under_its_source_and_links_to_the_quote(tmp_path):
    wb, _ = build(tmp_path, rows=VALUE_ROWS)
    ws = wb["Site_Review"]
    column = letters(ws)
    assert [ws.row_dimensions[r].outlineLevel for r in range(2, 6)] == [0, 1, 1, 0]
    assert ws.sheet_properties.outlinePr.summaryBelow is False
    assert ws[f"{column['site_id']}3"].fill.fgColor.rgb.endswith(VALUE_ROW_FILL)
    link = ws[f"{column['source_url']}3"]
    assert link.value == QUOTE_LINK_TEXT
    # A long quote is marked by its first and last four words.
    assert link.hyperlink.target == "https://news.example/landfill#:~:text=The%20landfill%20closed%20in,30%20years%20of%20service"
    # The source's own row keeps its plain link.
    assert ws[f"{column['source_url']}2"].hyperlink.target == "https://news.example/landfill"


def test_a_quote_link_marks_the_quote_and_falls_back_to_the_page():
    assert quote_link("https://a.example/p", "A short - quote, & more") == "https://a.example/p#:~:text=A%20short%20%2D%20quote%2C%20%26%20more"
    assert quote_link("https://a.example/p#top", "Closed 2008") == "https://a.example/p#top:~:text=Closed%202008"
    # A PDF doesn't follow text fragments, and a link with no quote has nothing to mark.
    assert quote_link("https://a.example/report.pdf", "Closed 2008") == "https://a.example/report.pdf"
    assert quote_link("https://a.example/p", "") == "https://a.example/p"
    assert quote_link("javascript:alert(1)", "x") is None


def test_coordinates_open_google_maps(tmp_path):
    rows = [review_row("13413", our_coordinates="49.7583, -97.2004", source_coordinates="49.759, -97.201")]
    ws = build(tmp_path, rows=rows)[0]["Site_Review"]
    column = letters(ws)
    assert ws[f"{column['our_coordinates']}2"].hyperlink.target == "https://www.google.com/maps/search/?api=1&query=49.7583,-97.2004"
    assert ws[f"{column['source_coordinates']}2"].hyperlink.target == "https://www.google.com/maps/search/?api=1&query=49.759,-97.201"


def test_a_value_rows_decision_offers_correct_or_wrong_and_its_correction_the_attributes_values(tmp_path):
    wb, _ = build(tmp_path, rows=VALUE_ROWS)
    ws = wb["Site_Review"]
    column = letters(ws)
    defs = list(wb["Definitions"].values)
    by_range = {str(v.sqref): v for v in ws.data_validations.dataValidation}
    decision, corrected = column["decision"], column["corrected_value"]
    assert set(by_range) == {f"{decision}2 {decision}5", f"{decision}3:{decision}4", f"{corrected}4"}

    def listed(validation):
        letter = validation.formula1.split("$")[1]
        index = [cell for cell in defs[0]].index(wb["Definitions"][f"{letter}1"].value)
        return [row[index] for row in defs[1:] if row[index]]

    assert listed(by_range[f"{decision}2 {decision}5"]) == SITE_REVIEW_DECISIONS
    assert listed(by_range[f"{decision}3:{decision}4"]) == VALUE_DECISIONS == ["Correct", "Wrong"]
    # A closing year is typed; a site type is picked from its list.
    assert listed(by_range[f"{corrected}4"]) == FACILITY_TYPE_ALLOWED


def test_a_value_row_greys_out_once_its_source_is_junk_or_a_new_site(tmp_path):
    ws = build(tmp_path, rows=VALUE_ROWS)[0]["Site_Review"]
    column = letters(ws)
    [grey] = [ranges.rules[0] for ranges in ws.conditional_formatting if "COUNTIFS" in ranges.rules[0].formula[0]]
    formula = grey.formula[0]
    assert formula.startswith(f'AND(${column["check"]}2<>"",')
    assert f'"{JUNK}"' in formula and '"New site"' in formula
    assert grey.dxf.fill.fgColor.rgb.endswith(IGNORED_FILL)
