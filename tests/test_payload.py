"""One reader for every cached response (build step 4 of the source-matching plan).

The agent now reports each source with the facility it describes and the values it gives. Runs
cached before, attribute-first, are converted to that shape, so every reader - the evidence, the
link check, the run log, validation, the follow-up merge, retry detection and the leads - reads
both alike.
"""
from __future__ import annotations

import csv
import json
from datetime import date

from waste_ai_search.arbitrate import cached_source_urls, extract_evidence, payload_leads, run_arbitration
from waste_ai_search.payload import (
    ATTRIBUTE_FIRST,
    SOURCE_FIRST,
    is_empty,
    merge_payloads,
    read_payload,
)
from waste_ai_search.run_context import PipelineConfig
from waste_ai_search.schema import validate_foundry_payload


PERMIT = {
    "source_title": "Permit EPR/AB1234",
    "publisher": "Environment Agency",
    "source_type": "Permit or registry",
    "publication_date": "2021-04-01",
    "url": "https://environment.data.gov.uk/permit/AB1234",
    "quoted_evidence_short": "Operator: Ecoserv Ltd",
}
NEWS = {
    "source_title": "Landfill to close",
    "publisher": "Local paper",
    "source_type": "News report",
    "publication_date": "2024-02-01",
    "url": "https://news.example/landfill",
    "quoted_evidence_short": "The landfill closes in March.",
}


def attribute(name, value, *sources, **extra):
    return {
        "attribute_name": name, "value": value, "unit": "", "value_basis": "Direct",
        "confidence_score": "High", "value_date": "2024", "evidence_summary": f"{name} summary",
        "search_terms_used": "terms", "sources": list(sources), **extra,
    }


OLD = {
    "site_id": "7",
    "attributes": [
        attribute("operator", "Ecoserv Ltd", PERMIT),
        attribute("facility_status", "Inactive", NEWS, dict(PERMIT, quoted_evidence_short="Status: closed")),
        attribute("found_facility_name", "Ecoserv Landfill", PERMIT),
        attribute("found_latitude", "54.12345", PERMIT, NEWS),
        attribute("found_longitude", "-1.54321", PERMIT),
    ],
    "unverified_leads": [{"url": "https://lead.example", "lead_summary": "Maybe"}],
    "search_notes": "Searched in English.",
}


def by_url(read):
    return {source["url"]: source for source in read["sources"]}


# --- converting an attribute-first response ---------------------------------------------------
def test_citations_become_one_source_per_exact_url_with_their_values():
    read = read_payload(OLD)
    assert read["shape"] == ATTRIBUTE_FIRST
    sources = by_url(read)
    assert list(sources) == [PERMIT["url"], NEWS["url"]]
    permit = sources[PERMIT["url"]]
    # The coordinates are not values: they are the location the source states, below.
    assert [value["attribute_name"] for value in permit["values"]] == ["operator", "facility_status", "found_facility_name"]
    # Each value keeps its own citation's quote, and the attribute's other fields.
    status = permit["values"][1]
    assert status["quoted_evidence_short"] == "Status: closed"
    assert (status["value"], status["value_date"], status["evidence_summary"]) == ("Inactive", "2024", "facility_status summary")
    assert permit["publisher"] == "Environment Agency"


def test_only_sources_cited_for_the_name_carry_it():
    sources = by_url(read_payload(OLD))
    assert sources[PERMIT["url"]]["facility_name_in_source"] == "Ecoserv Landfill"
    assert sources[NEWS["url"]]["facility_name_in_source"] == ""
    assert sources[NEWS["url"]]["facility_name_english"] == ""


def test_only_sources_cited_for_both_coordinates_state_a_location():
    sources = by_url(read_payload(OLD))
    permit = sources[PERMIT["url"]]
    assert (permit["stated_location"], permit["stated_location_kind"]) == ("54.12345, -1.54321", "coordinates")
    # The news story was cited for the latitude alone, and half a location is no location.
    assert sources[NEWS["url"]]["stated_location"] == ""


def test_two_spellings_of_one_url_are_one_source_and_blanks_are_filled_from_the_other():
    undated = dict(PERMIT, publication_date="", url="https://www.environment.data.gov.uk/permit/AB1234/")
    read = read_payload({"attributes": [attribute("operator", "Ecoserv Ltd", undated), attribute("opening_year", "1990", PERMIT)]})
    (source,) = read["sources"]
    assert source["url"] == undated["url"]  # the first spelling, which the link check fetched
    assert source["publication_date"] == "2021-04-01"
    assert len(source["values"]) == 2


def test_each_value_keeps_how_its_own_citation_described_the_page():
    # Batch 1, site 14922: the main pass labelled anewclimate.com a directory listing, and the gas
    # follow-up pass an operator disclosure. Each value is tiered by its own citation's label, as it
    # was before the conversion, while an undated citation still takes the page's date.
    listing = {"source_title": "Profile", "publisher": "Directory", "source_type": "Company directory listing",
               "publication_date": "2024-01-01", "url": "https://anewclimate.com/site"}
    disclosure = dict(listing, source_type="Operator disclosure", publication_date="2025-06-10", publisher="")
    main = {"site_id": "7", "attributes": [attribute("operator", "Anew Climate", listing)]}
    gccs = {"site_id": "7", "attributes": [
        attribute("has_flare", "TRUE", disclosure),
        attribute("opening_year", "1990", dict(listing, publication_date="")),
    ]}
    site = {"site_id": "7", "internal_facility_id": "7", "site_name": "Ecoserv", "country_iso3": "GBR"}
    _by_attribute, rows, sources, _warnings = extract_evidence(site, merge_payloads([main, gccs]), "run", "v", date(2026, 1, 1))
    tiers = {row["attribute_name"]: (row["source_tier"], row["tier_rule_applied"]) for row in rows}
    assert tiers["operator"] == ("Tier 4", "source_type 'company directory listing' -> Tier 4")
    assert tiers["has_flare"] == ("Tier 1", "source_type 'operator disclosure' -> Tier 1")
    assert tiers["opening_year"] == ("Tier 4", "source_type 'company directory listing' -> Tier 4")
    # One page, one source, still described by its first citation in sources.csv.
    (page,) = read_payload(merge_payloads([main, gccs]))["sources"]
    assert (page["source_type"], page["publisher"]) == ("Company directory listing", "Directory")
    assert {entry["source_type"] for entry in sources} == {"Company directory listing", "Operator disclosure"}


def test_a_converted_location_is_tiered_by_the_citation_of_its_coordinates():
    blog = dict(NEWS, source_type="Wiki or aggregator", url=PERMIT["url"])
    old = {"site_id": "7", "attributes": [
        attribute("operator", "Ecoserv Ltd", blog),
        attribute("found_latitude", "54.12345", PERMIT),
        attribute("found_longitude", "-1.54321", PERMIT),
    ]}
    site = {"site_id": "7", "internal_facility_id": "7", "site_name": "Ecoserv", "country_iso3": "GBR"}
    _by_attribute, rows, _sources, _warnings = extract_evidence(site, old, "run", "v", date(2026, 1, 1))
    location = next(row for row in rows if row["attribute_name"] == "found_location")
    assert (location["location_basis"], location["source_tier"]) == ("coordinates", "Tier 1")


def test_a_citation_without_a_url_stays_a_source_of_its_own():
    no_url = dict(PERMIT, url="")
    read = read_payload({"attributes": [attribute("operator", "A", no_url, no_url)]})
    assert len(read["sources"]) == 2


def test_reading_twice_is_harmless():
    read = read_payload(OLD)
    assert read_payload(read) is read


# --- source-first responses ---------------------------------------------------------------------
NEW = {
    "site_id": "7",
    "sources": [
        {
            **{key: value for key, value in PERMIT.items() if key != "quoted_evidence_short"},
            "facility_name_in_source": "Ecoserv Landfill",
            "facility_name_english": "Ecoserv Landfill",
            "stated_location": "NZ 123 456",
            "stated_location_kind": "grid reference",
            "values": [
                {"attribute_name": "operator", "value": "Ecoserv Ltd", "unit": "", "value_basis": "Direct",
                 "confidence_score": "High", "value_date": "2021", "evidence_summary": "Permit holder.",
                 "search_terms_used": "Ecoserv landfill permit", "quoted_evidence_short": "Operator: Ecoserv Ltd"},
            ],
        },
        {**{key: value for key, value in NEWS.items() if key != "quoted_evidence_short"}, "values": []},
    ],
    "unverified_leads": [],
    "search_notes": "Searched in English.",
}


def test_a_source_first_response_reads_as_written():
    read = read_payload(NEW)
    assert read["shape"] == SOURCE_FIRST
    assert [source["url"] for source in read["sources"]] == [PERMIT["url"], NEWS["url"]]
    assert read["sources"][0]["stated_location"] == "NZ 123 456"
    assert validate_foundry_payload(NEW) == []


def test_validation_reports_each_shape_in_its_own_terms():
    old = {"attributes": [{"attribute_name": "operator", "value": "A", "sources": []}, "junk"]}
    assert validate_foundry_payload(old) == [
        "attributes[1] missing required field 'value_basis'.",
        "attributes[1] missing required field 'confidence_score'.",
        "attributes[1] has no clickable source URL.",
        "attributes[2] must be an object.",
    ]
    new = {"sources": [{"url": "", "values": [{"attribute_name": "colour", "value": "red"}]}, 3]}
    assert validate_foundry_payload(new) == [
        "sources[2] must be an object.",
        "sources[1] has no clickable source URL.",
        "sources[1].values[1] missing required field 'value_basis'.",
        "sources[1].values[1] missing required field 'confidence_score'.",
        "sources[1].values[1] has unsupported attribute_name 'colour'.",
    ]
    assert validate_foundry_payload({"sources": "none"}) == ["Payload field 'sources' must be a list."]
    odd_kind = {"sources": [{"url": "https://a.example", "stated_location": "x", "stated_location_kind": "coordinates | postcode", "values": []}]}
    assert validate_foundry_payload(odd_kind) == ["sources[1] has unrecognised stated_location_kind 'postcode'."]
    assert validate_foundry_payload(["not", "an", "object"]) == ["Response is not a JSON object."]


def test_passes_of_either_shape_merge():
    merged = merge_payloads([OLD, NEW])
    assert len(merged["sources"]) == 4
    assert merged["unverified_leads"] == OLD["unverified_leads"]
    assert merged["search_notes"] == "Searched in English. | Searched in English."


def test_a_response_with_no_sources_is_empty():
    assert is_empty({"sources": [], "search_notes": "Nothing found."})
    assert is_empty({"attributes": []})
    assert not is_empty(NEW)


def test_evidence_reads_either_shape_alike():
    site = {"site_id": "7", "internal_facility_id": "7", "site_name": "Ecoserv", "country_iso3": "GBR"}
    for payload in (OLD, NEW):
        by_attribute, rows, _sources, _warnings = extract_evidence(site, payload, "run", "v", date(2026, 1, 1))
        operator = by_attribute["operator"][0]
        assert (operator["value"], operator["url"]) == ("Ecoserv Ltd", PERMIT["url"])
        assert next(row for row in rows if row["attribute_name"] == "operator")["quoted_evidence_short"] == "Operator: Ecoserv Ltd"


# --- run-level readers ------------------------------------------------------------------------
def write_run(tmp_path, responses):
    from waste_ai_search.input_loader import write_csv_records
    from waste_ai_search.seed_source import seed_headers

    run_dir = tmp_path / "run"
    seeds = [
        {"facility_id": sid, "site_id": sid, "internal_facility_id": sid, "year": "2022", "site_name": f"Site {sid}",
         "facility_name": f"Site {sid}", "country_iso3": "GBR", "iso3c_plus": "GBR", "reference_year": "2022",
         "contributing_data_sources": "osm_2022"}
        for sid in responses
    ]
    write_csv_records(run_dir / "seed.csv", seeds, seed_headers())
    raw = run_dir / "raw_foundry_responses"
    raw.mkdir(parents=True)
    for sid, response in responses.items():
        (raw / f"site_{sid}.json").write_text(json.dumps(response))
    return run_dir


def test_the_link_check_and_the_leads_read_both_shapes(tmp_path):
    run_dir = write_run(tmp_path, {"7": OLD, "8": {**NEW, "site_id": "8", "unverified_leads": ["A lead in prose"]}})
    assert cached_source_urls(run_dir) == [PERMIT["url"], NEWS["url"]]
    paths = sorted((run_dir / "raw_foundry_responses").glob("site_*.json"))
    leads = payload_leads(paths, {}, "run")
    assert [(lead["site_id"], lead["url"] or lead["lead_summary"]) for lead in leads] == [
        ("7", "https://lead.example"), ("8", "A lead in prose"),
    ]


def test_a_source_first_run_arbitrates_and_a_tool_failure_is_still_retried(tmp_path):
    failed = {"site_id": "9", "sources": [], "search_notes": "The search_query tool did not return usable results."}
    run_dir = write_run(tmp_path, {"8": {**NEW, "site_id": "8"}, "9": failed})
    run_arbitration(PipelineConfig(input_csv=None, run_dir=run_dir, run_id="run", dataset_version="v"))

    queue = list(csv.DictReader((run_dir / "review_queue.csv").open(encoding="utf-8-sig")))
    assert ("8", "operator", "Ecoserv Ltd") in {(row["site_id"], row["attribute_name"], row["resolved_value"]) for row in queue}
    retry = list(csv.DictReader((run_dir / "sites_to_retry.csv").open(encoding="utf-8-sig")))
    assert [row["site_id"] for row in retry] == ["9"]


def test_the_run_log_counts_attributes_and_sources_in_either_shape(tmp_path, monkeypatch):
    from waste_ai_search import search

    class Client:
        def __init__(self):
            self.responses = [NEW, OLD]

        def search_site(self, site, prompt):  # noqa: ARG002
            return self.responses.pop(0)

    seed = tmp_path / "seed.csv"
    seed.write_text(
        "site_id,site_name,country_iso3,latitude,longitude\n7,Ecoserv Landfill,GBR,54.1,-1.5\n"
        "8,Tip Road Landfill,GBR,54.2,-1.6\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(search, "get_client", lambda config: Client())
    search.run_search(PipelineConfig(input_csv=seed, run_dir=tmp_path / "run", run_id="t", site_delay_seconds=0, workers=1))

    rows = list(csv.DictReader((tmp_path / "run" / "foundry_run_log.csv").open(encoding="utf-8-sig")))
    counts = {row["site_id"]: (row["parsed_attribute_count"], row["parsed_source_count"]) for row in rows}
    assert counts == {"7": ("1", "2"), "8": ("3", "2")}


def test_a_converted_response_keeps_its_evidence_ids_and_order():
    """Arbitrating an older run again gives the same evidence ids, in the order the agent cited them,
    which is also the last tie-break between equal sources."""
    site = {"site_id": "7", "internal_facility_id": "7", "site_name": "Ecoserv", "country_iso3": "GBR"}
    _by, rows, _sources, _warnings = extract_evidence(site, OLD, "run", "v", date(2026, 1, 1))
    assert [row["evidence_id"] for row in rows][:3] == ["WAIEV-7-001-001", "WAIEV-7-002-001", "WAIEV-7-002-002"]
    assert [row["attribute_name"] for row in rows][:3] == ["operator", "facility_status", "facility_status"]
    _by, rows, _sources, _warnings = extract_evidence(site, NEW, "run", "v", date(2026, 1, 1))
    assert rows[0]["evidence_id"] == "WAIEV-7-S001-001"


def test_a_search_tool_failure_is_read_alike_in_either_shape():
    from waste_ai_search.run_context import search_tool_failed

    notes = "The search_query tool did not return usable results."
    assert search_tool_failed({"attributes": [], "search_notes": notes})
    assert search_tool_failed({"sources": [], "search_notes": notes})
    assert not search_tool_failed(["not", "an", "object"])
