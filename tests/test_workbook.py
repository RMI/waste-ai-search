"""The workbook is the only file an SME opens, so its affordances must match the instructions.

Since step 3 of the approve-sources plan its worklist is the Site_Review tab: one row per site-source
pair an SME decides, grouped by our site. It replaces the Review_Queue and Leads tabs, whose tests
(the validation_status, rejection_reason, corrected_value and corrected_site_id dropdowns, and the
match column) went in step 2, and step 2's read-only Found_Values tab, whose rows are in
found_values.csv. Which pairs are listed, and their flags, are tested in test_site_review.py.
"""
import zipfile

from openpyxl import load_workbook
from openpyxl.utils import get_column_letter

from waste_ai_search.schema import SITE_REVIEW_DECISIONS, SITE_REVIEW_EDITABLE, SITE_REVIEW_HEADERS
from waste_ai_search.site_review import FAR_AWAY_FLAG
from waste_ai_search.workbook_io import FLAG_FILL, write_review_workbook


def review_row(site_id, url="https://a.example/permit", flags="", **extra):
    return {h: "" for h in SITE_REVIEW_HEADERS} | {
        "review_id": f"WAIREV-{site_id}-{url}", "site_id": site_id,
        "source_url": url, "flags": flags, "would_load": "operator = City", **extra,
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
    assert [ws.cell(r, 2).value for r in range(2, ws.max_row + 1)] == ["900", "900", "13413"]


def test_the_columns_go_from_our_site_to_the_source_to_the_smes_decision():
    """Our site, then the source, what it says, the flags and what approving would load, and last
    the five columns an SME fills in."""
    assert SITE_REVIEW_HEADERS[0] == "review_id"
    assert SITE_REVIEW_HEADERS[1:7] == ["site_id", "site_name", "original_site_name", "source_language", "country_iso3", "our_coordinates"]
    for group in (
        ["source_url", "source_title", "publisher", "source_tier", "link_status"],
        # As the source writes them, then what we read from them, and why not where we couldn't.
        ["facility_name_in_source", "facility_name_english", "source_address", "stated_coordinates",
         "source_coordinates", "coordinates_from", "distance_km", "location_note"],
        ["flags", "would_load"],
    ):
        start = SITE_REVIEW_HEADERS.index(group[0])
        assert SITE_REVIEW_HEADERS[start:start + len(group)] == group
    assert SITE_REVIEW_HEADERS[-5:] == SITE_REVIEW_EDITABLE == ["decision", "other_site_id", "reviewer", "reviewed_date", "notes"]


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
    assert readme["Site_Review"][3] == "decision, other_site_id, reviewer, reviewed_date, notes - nothing else"
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
    [(cells, [rule])] = [(str(ranges.sqref), ranges.rules) for ranges in ws.conditional_formatting]
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
    assert "An empty tab means nothing needs you." in readme["Site_Review"]


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


def test_the_readme_walks_an_sme_through_a_row_keeping_our_site_and_the_source_apart(tmp_path):
    readme = {row[0]: row for row in list(build(tmp_path)[0]["ReadMe"].values)[1:]}
    purpose, steps = readme["Site_Review"][1], readme["Site_Review"][2]
    assert purpose.startswith("YOUR WORKLIST.")
    assert "whether the source is about this site of ours" in purpose
    for words in ("our database", "the facility it describes", "where the source places the facility"):
        assert words in steps
    numbered = [line.split(".")[0] for line in steps.splitlines() if line[:1].isdigit()]
    assert numbered == ["1", "2", "3", "4", "5", "6", "7"]
    for column in ("our_coordinates", "source_url", "source_coordinates", "coordinates_from", "distance_km",
                   "flags", "would_load", *SITE_REVIEW_EDITABLE):
        assert column in steps, column
    for decision in SITE_REVIEW_DECISIONS:
        assert f"- {decision}:" in steps
    assert FAR_AWAY_FLAG in steps
    # accept-reviews stops on a Different site with no other_site_id, so an SME who can't tell which
    # of our sites leaves the decision blank.
    assert "if you can't tell which of our sites it is about, leave decision blank" in steps


def test_the_readme_says_what_each_status_means(tmp_path):
    purpose = {row[0]: row[1] for row in list(build(tmp_path)[0]["ReadMe"].values)[1:]}["(file) found_values.csv"]
    for words in ("auto-approved", "SME review", "not used", "more than 5 km", "Not a Waste Facility", "review_id", "Site_Review"):
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
