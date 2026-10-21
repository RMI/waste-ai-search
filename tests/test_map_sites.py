"""Mapcarta and Maptons are dropped by website (build step 1 of the source-matching plan).

Mapcarta's landfill pages copy OpenStreetMap, where most of our sites came from, and Maptons is an
unattributed scrape of business listings. A citation of either is dropped when evidence is
extracted, whatever source_type the agent gave it, and counted in parse_warnings.csv per site and
website; so is a lead of the agent's that points at one. The agent is also told not to cite them,
and to cite the government source where a directory republishes one.

Changed on purpose for build step 5: a dropped page's coordinates no longer feed the 5 km identity
gate, which matching each source to the site it is about replaced. The tests of that went with it.
"""
from __future__ import annotations

import csv
import json
from datetime import date

import pytest

from waste_ai_search.arbitrate import cached_source_urls, extract_evidence, run_arbitration
from waste_ai_search.arbitration import resolve
from waste_ai_search.credibility import DROPPED_SOURCE_SITES, dropped_source_site
from waste_ai_search.prompt_builder import build_site_prompt, requested_attributes
from waste_ai_search.run_context import PipelineConfig


REGULATOR = {
    "source_title": "Landfill register",
    "publisher": "Ministry of Environment",
    "source_type": "Regulator",
    "publication_date": "2023-01-01",
    "url": "https://environment.example.gov/register",
}


def site():
    return {"site_id": "1", "internal_facility_id": "1", "site_name": "Landfill", "country_iso3": "CAN",
            "latitude": "49.85", "longitude": "-99.95",
            "contributing_data_sources": "osm_2022", "reference_year": "2022"}


def attribute(name, value, *sources, unit=""):
    return {"attribute_name": name, "value": value, "unit": unit, "value_basis": "Direct",
            "confidence_score": "High", "value_date": "2023", "evidence_summary": "s",
            "sources": list(sources)}


def cite(url, **overrides):
    return dict(REGULATOR, url=url, **overrides)


def extract(*attributes):
    return extract_evidence(site(), {"site_id": "1", "attributes": list(attributes)}, "r", "v", date(2026, 1, 1))


# --- which URLs are dropped -------------------------------------------------------------------
@pytest.mark.parametrize("url,dropped", [
    ("https://mapcarta.com/W123456789", "mapcarta.com"),
    ("https://www.mapcarta.com/W123456789", "mapcarta.com"),
    ("https://mapcarta.com/fr/W123456789", "mapcarta.com"),  # a language path
    ("HTTPS://MapCarta.COM/W1", "mapcarta.com"),
    ("https://mapcarta.com:443/W1", "mapcarta.com"),
    ("mapcarta.com/W1", "mapcarta.com"),  # no scheme
    ("https://ca.maptons.com/p/brandon-landfill", "maptons.com"),  # a country subdomain
    ("https://maptons.com/123", "maptons.com"),
    ("https://wikimapia.org/123/Landfill", ""),  # independent crowd-sourced map: kept
    ("https://notmapcarta.com/x", ""),
    ("https://mapcarta.com.example.org/x", ""),
    ("https://example.org/page?from=mapcarta.com", ""),
    ("", ""),
    ("https://[invalid", ""),
])
def test_map_sites_are_known_by_their_host(url, dropped):
    assert dropped_source_site(url) == dropped


# --- extraction drops them --------------------------------------------------------------------
def test_a_map_site_is_dropped_whatever_the_agent_called_it():
    """Dropped by website, not label: a Mapcarta page called a regulator still goes."""
    by_attribute, evidence, sources, _warnings = extract(
        attribute("facility_status", "active", cite("https://mapcarta.com/W1"), REGULATOR),
    )
    assert [row["url"] for row in evidence] == [REGULATOR["url"]]
    assert [row["url"] for row in sources] == [REGULATOR["url"]]
    assert [item["url"] for item in by_attribute["facility_status"]] == [REGULATOR["url"]]


def test_a_map_label_alone_drops_nothing():
    """The agent called asbestossafety.gov.au a map directory 38 times in batch 1."""
    labelled = cite("https://www.asbestossafety.gov.au/landfills", source_type="Map or coordinate directory")
    _by, evidence, _sources, warnings = extract(attribute("facility_status", "active", labelled))
    assert len(evidence) == 1
    assert not any("Dropped" in warning for warning in warnings)


def test_drops_are_counted_per_site_and_website():
    _by, _evidence, _sources, warnings = extract(
        attribute("found_latitude", "49.85", cite("https://mapcarta.com/W1"), unit="decimal degrees"),
        attribute("found_longitude", "-99.95", cite("https://fr.mapcarta.com/fr/W1"), unit="decimal degrees"),
        attribute("operator", "City of Brandon", cite("https://ca.maptons.com/p/1"), REGULATOR),
    )
    dropped = [warning for warning in warnings if warning.startswith("Dropped")]
    assert len(dropped) == 2
    assert dropped[0].startswith("Dropped 2 citation(s) of mapcarta.com")
    assert dropped[1].startswith("Dropped 1 citation(s) of maptons.com")


def test_an_attribute_left_with_no_source_is_not_promoted():
    """Like a value given with no source: no evidence, nothing to resolve, nothing in Leads."""
    by_attribute, evidence, sources, warnings = extract(
        attribute("operator", "City of Brandon", cite("https://mapcarta.com/W1")),
    )
    assert evidence == [] and sources == []
    assert "operator" not in by_attribute
    assert resolve(site(), "operator", by_attribute.get("operator", []))["resolution"] == "Not found"
    assert "operator: every source was a dropped map site; not promoted." in warnings


def test_a_kept_source_still_carries_the_value():
    by_attribute, _evidence, _sources, warnings = extract(
        attribute("facility_status", "active", cite("https://mapcarta.com/W1"), REGULATOR),
    )
    row = resolve(site(), "facility_status", by_attribute["facility_status"])
    assert row["resolved_value"] == "Active"
    assert row["agreeing_source_count"] == 1
    assert not any("every source was a dropped map site" in warning for warning in warnings)


def test_a_lead_pointing_at_a_map_site_is_counted_with_the_citations():
    payload = {"site_id": "1", "attributes": [], "unverified_leads": [
        {"lead_summary": "Mapcarta lists a landfill here.", "url": "https://mapcarta.com/W2"},
        {"lead_summary": "A news story mentions it.", "url": "https://news.example.com/landfill"},
        "A lead given as text only.",
    ]}
    _by, _evidence, _sources, warnings = extract_evidence(site(), payload, "r", "v", date(2026, 1, 1))
    assert [warning.split(":")[0] for warning in warnings] == ["Dropped 1 citation(s) of mapcarta.com"]


# --- the link check and a whole run -----------------------------------------------------------
NEWS_LEAD = {"lead_summary": "A news story says the landfill is closing.",
             "url": "https://news.example.com/landfill"}


def build_run(tmp_path, attributes=None, leads=None):
    from waste_ai_search.input_loader import write_csv_records
    from waste_ai_search.seed_source import seed_headers

    run_dir = tmp_path / "run"
    write_csv_records(run_dir / "seed.csv", [dict(site(), facility_id="1", year="2022", facility_name="Landfill",
                                                  iso3c_plus="CAN")], seed_headers())
    raw = run_dir / "raw_foundry_responses"
    raw.mkdir(parents=True)
    if attributes is None:
        attributes = [
            attribute("facility_status", "active", cite("https://mapcarta.com/W1"), REGULATOR),
            attribute("found_latitude", "49.85", cite("https://ca.maptons.com/p/1"), unit="decimal degrees"),
        ]
    if leads is None:
        leads = [{"lead_summary": "Mapcarta lists a landfill nearby.", "url": "https://mapcarta.com/W2"},
                 NEWS_LEAD]
    response = {"site_id": "1", "attributes": attributes, "unverified_leads": leads}
    (raw / "site_1.json").write_text(json.dumps(response))
    return run_dir


def arbitrate(run_dir):
    run_arbitration(PipelineConfig(input_csv=None, run_dir=run_dir, run_id="run", dataset_version="v"))
    return {name: list(csv.DictReader((run_dir / f"{name}.csv").open()))
            for name in ("resolved", "review_queue", "parse_warnings", "supplementary_leads")}


def test_dropped_urls_are_not_link_checked(tmp_path):
    assert cached_source_urls(build_run(tmp_path)) == [REGULATOR["url"]]


def test_a_run_counts_its_drops_in_parse_warnings_and_cites_none_of_them(tmp_path):
    """The agent's own leads included: one pointing at a map site is dropped and counted too."""
    run_dir = build_run(tmp_path)
    out = arbitrate(run_dir)

    dropped = [row for row in out["parse_warnings"] if row["warning"].startswith("Dropped")]
    assert [(row["site_id"], row["warning"].split(":")[0]) for row in dropped] == [
        ("1", "Dropped 2 citation(s) of mapcarta.com"),
        ("1", "Dropped 1 citation(s) of maptons.com"),
    ]
    for name in ("evidence.csv", "sources.csv", "supplementary_leads.csv", "resolved.csv"):
        text = (run_dir / name).read_text()
        assert "mapcarta" not in text and "maptons" not in text, name
    assert [lead["url"] for lead in out["supplementary_leads"]] == [NEWS_LEAD["url"]]


# --- the prompt -------------------------------------------------------------------------------
def test_the_prompt_asks_for_neither_map_site_and_for_the_government_source():
    prompt = " ".join(build_site_prompt(site(), attributes=requested_attributes(site())).split())
    assert "Never cite mapcarta.com or maptons.com" in prompt
    assert all(dropped in prompt for dropped in DROPPED_SOURCE_SITES)
    assert "cite the government source instead" in prompt
    assert "wastebook.co.uk copies the Environment Agency register" in prompt
    assert "postcode centroids" in prompt and "permit date, not a closure date" in prompt
    assert "landfills.ca copies Statistics Canada's infrastructure database" in prompt
