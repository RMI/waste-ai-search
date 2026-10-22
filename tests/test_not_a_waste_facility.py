"""WP-525: flag facilities that sources say were never disposal sites.

For facilities known only from OSM or Global Plastic Watch, `facility_type` may come back as
"Not a Waste Facility" when a source positively says the site is something else. It is a
contradiction, not a verification: finding nothing leaves facility_type empty.

The failure these tests exist to prevent is the one the ticket names: a closed, capped or
redeveloped landfill read as "not a landfill". Those sites keep emitting methane for decades and
are exactly what WasteMAP tracks, so "it's a park now" must never become "Not a Waste Facility".

By decision on WP-525 the value is handled like any facility_type: it reaches the next pass's seed,
and it is always reviewed. A Tier 1-2 value filling an empty field is approved automatically again
(step 2 of the approve-sources plan), so the verdict is one of the exceptions that send its source to
an SME whatever its tier. Tests below pin both, so changing either is deliberate.

All offline: agent responses are hand-built payloads fed through the real extraction, sorting,
arbitration, refresh and workbook code.
"""
from __future__ import annotations

from datetime import date

from openpyxl import load_workbook

from waste_ai_search.arbitrate import extract_evidence
from waste_ai_search.found_values import AUTO_APPROVED, SME_REVIEW, pick_values, sort_values
from waste_ai_search.prompt_builder import (
    build_site_prompt,
    needs_contradiction_check,
    requested_attributes,
)
from waste_ai_search.schema import (
    FACILITY_TYPE_OFFERED,
    NOT_A_WASTE_FACILITY,
    PENDING_UPSTREAM_FACILITY_TYPES,
    map_enum_value,
)


REGULATOR = {
    "source_title": "Mining concessions register",
    "publisher": "Provincial Ministry of Mines",
    "source_type": "Regulator",
    "publication_date": "2023-04-01",
    "url": "https://mines.example.gov/concession/4471",
    "quoted_evidence_short": "Granite quarry, active concession since 1998.",
}

WIKI = {
    "source_title": "Local places",
    "publisher": "Community wiki",
    "source_type": "Wiki or aggregator",
    "publication_date": "",
    "url": "https://wiki.example.org/place/4471",
    "quoted_evidence_short": "The site is a gravel pit.",
}

NEWS = {
    "source_title": "Old tip becomes a park",
    "publisher": "Regional Times",
    "source_type": "News report",
    "publication_date": "2019-06-01",
    "url": "https://news.example.com/tip-park",
    "quoted_evidence_short": "The former landfill was capped in 2009 and reopened as a park.",
}


def unconfirmed_site(**overrides):
    """A facility nothing confirms: known only from OSM, a name and a point."""
    base = {
        "site_id": "4471",
        "internal_facility_id": "4471",
        "site_name": "Landfill",
        "country_iso3": "CAN",
        "latitude": "49.1",
        "longitude": "-122.3",
        "contributing_data_sources": "osm_2022",
        "reference_year": "2022",
    }
    base.update(overrides)
    return base


def attribute(name, value, sources, summary="summary"):
    return {
        "attribute_name": name,
        "value": value,
        "unit": "",
        "value_basis": "Direct",
        "confidence_score": "High",
        "value_date": "2023",
        "evidence_summary": summary,
        "sources": sources,
    }


def arbitrate(site, *attributes):
    """Run one site's payload through extraction and sorting, as run_arbitration does: its
    found_values.csv rows."""
    payload = {"site_id": site["site_id"], "attributes": list(attributes)}
    _by_attr, evidence, sources, _warn = extract_evidence(site, payload, "run", "v", date(2026, 1, 1))
    cited = {}
    for entry in sources:
        cited.setdefault((entry["site_id"], entry["source_index"]), entry)
    found = sort_values(evidence, list(cited.values()), {site["site_id"]: site}, 2026).rows
    return found, evidence


def row_for(found, name):
    """The value that would load for an attribute (found_values.pick_values)."""
    return next(row for (_site, attribute, _year), row in pick_values(found).items() if attribute == name)


def quarry(sources=(REGULATOR,), summary="Active granite quarry."):
    return attribute("facility_type", NOT_A_WASTE_FACILITY, list(sources), summary=summary)


# --- the value ----------------------------------------------------------------------------------
def test_the_value_is_accepted_and_common_phrasings_map_to_it():
    assert map_enum_value("facility_type", NOT_A_WASTE_FACILITY)[0] == NOT_A_WASTE_FACILITY
    for phrasing in ("not waste", "Not a waste site", "not a waste facility"):
        assert map_enum_value("facility_type", phrasing)[0] == NOT_A_WASTE_FACILITY


def test_a_bare_description_is_not_mistaken_for_the_verdict():
    """"granite quarry" in the value field is not a facility type; the verdict has to be explicit."""
    assert map_enum_value("facility_type", "granite quarry")[0] is None


def test_the_ordinary_facility_types_are_unchanged():
    assert map_enum_value("facility_type", "landfill")[0] == "Sanitary Landfill"
    assert map_enum_value("facility_type", "open dump")[0] == "Dumpsite"


# --- criterion 1: a positive contradiction ------------------------------------------------------
def test_a_source_describing_a_quarry_yields_not_a_waste_facility_with_a_clickable_source():
    found, _ = arbitrate(unconfirmed_site(), quarry())
    row = row_for(found, "facility_type")

    assert row["value"] == NOT_A_WASTE_FACILITY
    assert row["source_url"].startswith("https://")
    assert row["source_tier"] == "Tier 1"


def test_even_a_tier_1_verdict_is_routed_to_review():
    found, _ = arbitrate(unconfirmed_site(), quarry())
    row = row_for(found, "facility_type")

    assert row["status"] == SME_REVIEW
    assert row["status_reason"] == f"Tier 1 source; it says the site is {NOT_A_WASTE_FACILITY}."


def test_an_ordinary_tier_1_facility_type_is_auto_approved():
    """Reversed twice on purpose: this once auto-validated, then nothing was accepted automatically,
    and now batch 1's rule is back. The verdict alone is an exception."""
    found, _ = arbitrate(unconfirmed_site(), attribute("facility_type", "Dumpsite", [REGULATOR]))
    assert row_for(found, "facility_type")["status"] == AUTO_APPROVED


# --- a low-tier contradiction -------------------------------------------------------------------
def test_a_low_tier_verdict_still_reaches_review_marked_with_its_tier():
    found, _ = arbitrate(unconfirmed_site(), quarry(sources=[WIKI]))
    row = row_for(found, "facility_type")

    assert row["value"] == NOT_A_WASTE_FACILITY
    assert row["source_tier"] == "Tier 4"
    assert row["status"] == SME_REVIEW


def test_a_higher_tier_source_wins():
    found, _ = arbitrate(unconfirmed_site(), quarry(sources=[WIKI, REGULATOR]))
    assert row_for(found, "facility_type")["source_tier"] == "Tier 1"


# --- criterion 2: closure is not a contradiction ------------------------------------------------
def test_a_capped_landfill_keeps_its_type_and_reports_closure():
    """The agent is told a park-on-a-landfill keeps its landfill type, with closure recorded
    through facility_status and closing_year. That is an ordinary fill, not a verdict."""
    found, evidence = arbitrate(
        unconfirmed_site(),
        attribute("facility_type", "Sanitary Landfill", [NEWS]),
        attribute("facility_status", "Inactive", [NEWS]),
        attribute("closing_year", "2009", [NEWS]),
    )

    assert row_for(found, "facility_type")["value"] == "Sanitary Landfill"
    assert row_for(found, "facility_status")["value"] == "Inactive"
    assert row_for(found, "closing_year")["value"] == "2009"
    assert "CHECK:" not in row_for(found, "facility_type")["status_reason"]


def test_a_capped_landfill_wrongly_called_not_waste_is_caught_by_the_closure_cross_check():
    """If the agent misreads "now a park" anyway, the reviewer is told to look twice."""
    found, evidence = arbitrate(
        unconfirmed_site(),
        quarry(sources=[NEWS], summary="Now a public park."),
        attribute("facility_status", "Inactive", [NEWS]),
        attribute("closing_year", "2009", [NEWS]),
    )
    summary = row_for(found, "facility_type")["status_reason"]

    assert "CHECK: this run also found" in summary
    assert "facility_status = Inactive" in summary
    assert "closing_year = 2009" in summary


def test_the_guidance_tells_the_agent_closure_is_not_a_contradiction():
    prompt = build_site_prompt(unconfirmed_site(), attributes=requested_attributes(unconfirmed_site()))
    assert "IS STILL A WASTE DISPOSAL" in prompt
    assert "was this ever a waste disposal site?" in prompt
    assert "facility_status = Inactive and closing_year" in prompt


# --- criterion 3: silence is never a negative ---------------------------------------------------
def test_no_contradicting_evidence_leaves_facility_type_empty():
    found, evidence = arbitrate(unconfirmed_site(), attribute("operator", "Fraser Valley Regional District", [REGULATOR]))

    assert [row["attribute_name"] for row in found] == ["operator"]
    assert all(row["value"] != NOT_A_WASTE_FACILITY for row in found)


def test_a_site_the_agent_could_not_find_gets_no_verdict():
    """An empty attributes list - the agent's "I could not find this facility" - decides nothing."""
    found, _ = arbitrate(unconfirmed_site())
    assert found == []


# --- criterion 4, as decided on WP-525: carried forward, and always reviewed --------------------
def test_a_real_arbitration_queues_the_verdict_and_the_refresh_carries_it_forward(tmp_path):
    """Decided on WP-525: flows like any facility_type value. Pinned so reversing it is deliberate.

    The verdict waits for SME review in found_values.csv, and refresh-seed still reads it there, as
    it reads every value awaiting review.
    """
    import json

    from waste_ai_search.arbitrate import run_arbitration
    from waste_ai_search.cli import read_arbitrated_rows
    from waste_ai_search.input_loader import write_csv_records
    from waste_ai_search.run_context import PipelineConfig
    from waste_ai_search.seed_refresh import refresh_sites
    from waste_ai_search.seed_source import seed_headers

    site = unconfirmed_site(facility_id="4471", year="2022", facility_name="Landfill", iso3c_plus="CAN")
    run_dir = tmp_path / "run"
    write_csv_records(run_dir / "seed.csv", [site], seed_headers())
    (run_dir / "raw_foundry_responses").mkdir(parents=True)
    (run_dir / "raw_foundry_responses" / "site_4471.json").write_text(
        json.dumps({"site_id": "4471", "attributes": [quarry()]})
    )
    run_arbitration(PipelineConfig(input_csv=None, run_dir=run_dir, run_id="run", dataset_version="v"))

    rows = read_arbitrated_rows(run_dir)
    [verdict] = [row for row in rows if row["attribute_name"] == "facility_type"]
    assert (verdict["value"], verdict["status"]) == (NOT_A_WASTE_FACILITY, SME_REVIEW)
    [refreshed], _stats = refresh_sites([site], rows, "run")
    assert refreshed["facility_type"] == NOT_A_WASTE_FACILITY


def test_the_verdict_is_merged_into_the_next_pass_seed_and_not_offered_again():
    """facility_type is a gap-fill attribute, so the refresh carries it forward. The site then has
    a facility_type, counts as corroborated, and is not offered the verdict a second time."""
    from waste_ai_search.seed_refresh import refresh_sites

    site = unconfirmed_site()
    found, _ = arbitrate(site, quarry())
    [refreshed], _stats = refresh_sites([site], found, "run")

    assert refreshed["facility_type"] == NOT_A_WASTE_FACILITY
    assert not needs_contradiction_check(refreshed)


def test_the_value_is_declared_as_waiting_on_the_database():
    """The DB enum and chk_facility_type do not carry it yet; the pending list says so explicitly."""
    from waste_ai_search.db_enums import FACILITY_TYPE_VALUES

    assert NOT_A_WASTE_FACILITY in PENDING_UPSTREAM_FACILITY_TYPES
    assert NOT_A_WASTE_FACILITY not in FACILITY_TYPE_VALUES


def test_the_verdict_is_reviewed_on_its_own_facility_type_row():
    """No separate tab: the facility_type row carries the verdict, its source and tier."""
    found, _ = arbitrate(unconfirmed_site(), quarry())
    row = row_for(found, "facility_type")

    assert row["value"] == NOT_A_WASTE_FACILITY
    assert row["source_tier"] == "Tier 1"
    assert row["status"] == SME_REVIEW
    assert "CHECK:" not in row["status_reason"]


def test_the_workbook_has_no_contradictions_tab(tmp_path):
    from waste_ai_search.workbook_io import write_review_workbook

    path = write_review_workbook(tmp_path / "r.xlsx", run_config=[], found_values=[], parse_warnings=[])
    assert "Contradictions" not in load_workbook(path).sheetnames


# --- criterion 5: scope ---------------------------------------------------------------------------
def test_an_osm_only_site_is_offered_the_value():
    assert needs_contradiction_check(unconfirmed_site())
    assert "facility_type" in requested_attributes(unconfirmed_site())


def test_a_gpw_only_site_is_offered_it_too():
    assert needs_contradiction_check(unconfirmed_site(contributing_data_sources="gpw_2021"))


def test_osm_plus_gpw_is_still_unconfirmed():
    """Two Tier 4 detections of one polygon do not establish what it is used for."""
    assert needs_contradiction_check(unconfirmed_site(contributing_data_sources="osm_2022+gpw_2021"))


def test_any_corroborating_baseline_removes_the_offer():
    assert not needs_contradiction_check(unconfirmed_site(facility_status="Active"))
    assert not needs_contradiction_check(unconfirmed_site(annual_incoming_waste_metric_tonnes="12000"))


def test_area_from_the_same_polygon_is_not_corroboration():
    assert needs_contradiction_check(unconfirmed_site(area_square_meters="101999.87"))


def test_a_regulator_backed_site_is_not_offered_it():
    assert not needs_contradiction_check(unconfirmed_site(contributing_data_sources="lmop_2024"))
    assert not needs_contradiction_check(unconfirmed_site(contributing_data_sources="gpw_2021+eprtr_2022"))


def test_a_corroborated_site_prompt_is_not_offered_the_value():
    """Facilities with backing get the ordinary values and none of the addendum."""
    site = unconfirmed_site(contributing_data_sources="lmop_2024")
    prompt = build_site_prompt(site, attributes=requested_attributes(site))

    assert NOT_A_WASTE_FACILITY not in prompt
    assert "IS STILL A WASTE DISPOSAL" not in prompt
    assert f"Allowed values only: {', '.join(FACILITY_TYPE_OFFERED)}." in prompt


def test_an_unconfirmed_site_prompt_offers_it_in_guidance_and_definitions():
    prompt = build_site_prompt(unconfirmed_site(), attributes=requested_attributes(unconfirmed_site()))
    definitions = prompt.split("Definitions:")[1]
    assert NOT_A_WASTE_FACILITY in definitions


# --- criterion 6: the premise -------------------------------------------------------------------
def test_the_prompt_no_longer_asserts_the_site_is_a_disposal_facility():
    prompt = build_site_prompt(unconfirmed_site(), attributes=requested_attributes(unconfirmed_site()))
    assert "for this waste disposal site" not in prompt
    assert "recorded as a waste disposal facility" in prompt
