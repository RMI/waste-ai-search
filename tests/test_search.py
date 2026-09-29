"""Phase 1: site selection, prompt scoping, search-tool retry, and the run log."""
from datetime import date
from pathlib import Path

from waste_ai_search.arbitrate import extract_evidence
from waste_ai_search.input_loader import load_sites
from waste_ai_search.prompt_builder import requested_attributes


REGULATOR = {
    "source_title": "Annual waste statistics",
    "publisher": "Waste Authority",
    "source_tier": "Tier 4",
    "publication_date": "2022-12-31",
    "url": "https://example.org/waste-statistics",
    "language": "English",
    "source_type": "Regulator",
}


def site(**overrides):
    base = {
        "site_id": "1",
        "internal_facility_id": "1",
        "site_name": "Test Landfill",
        "country_iso3": "NGA",
        "latitude": "6.5",
        "longitude": "3.3",
        "contributing_data_sources": "osm_2022",
        "reference_year": "2022",
    }
    base.update(overrides)
    return base


def payload(*attributes):
    return {"site_id": "1", "attributes": list(attributes)}


def attribute(name, value, unit="", sources=None, **extra):
    row = {
        "attribute_name": name,
        "value": value,
        "unit": unit,
        "value_basis": "Direct",
        "confidence_score": "High",
        "value_date": "2022",
        "evidence_summary": "summary",
        "sources": sources if sources is not None else [REGULATOR],
    }
    row.update(extra)
    return row


def extract(s, p):
    return extract_evidence(s, p, "run", "v", date(2026, 1, 1))


class _FlakyClient:
    """Reports a search-tool failure for the first `fail_times` calls, then succeeds."""

    def __init__(self, fail_times):
        self.fail_times = fail_times
        self.calls = 0

    def search_site(self, site, prompt):  # noqa: ARG002
        self.calls += 1
        if self.calls <= self.fail_times:
            return {"attributes": [], "search_notes": "The search_query tool did not return usable results."}
        return {"attributes": [], "search_notes": "Searched thoroughly and found no source-backed data."}


def test_search_retries_when_the_agent_reports_its_tool_failed(tmp_path, monkeypatch):
    """foundry_client only retries exceptions; a reported tool failure is a valid 200 response."""
    import csv as _csv

    from waste_ai_search import search as pl

    seed = tmp_path / "seed.csv"
    seed.write_text(
        "site_id,site_name,country_iso3,latitude,longitude\n1,Test Landfill,NGA,6.5,3.3\n",
        encoding="utf-8",
    )
    client = _FlakyClient(fail_times=2)
    monkeypatch.setattr(pl, "get_client", lambda config: client)
    monkeypatch.setattr(pl, "SEARCH_TOOL_RETRY_DELAY_SECONDS", 0.0)

    config = pl.PipelineConfig(
        input_csv=seed, run_dir=tmp_path / "run", run_id="t", site_delay_seconds=0, search_tool_retries=3
    )
    pl.run_search(config)

    assert client.calls == 3  # two failures, then a clean answer
    rows = list(_csv.DictReader((tmp_path / "run" / "foundry_run_log.csv").open(encoding="utf-8-sig")))
    assert rows[0]["status"] == "Succeeded after 2 tool retry(s)"
    assert rows[0]["retry_count"] == "2"


def test_persistent_tool_failure_is_recorded_not_hidden(tmp_path, monkeypatch):
    import csv as _csv

    from waste_ai_search import search as pl

    seed = tmp_path / "seed.csv"
    seed.write_text("site_id,site_name,country_iso3\n1,Test Landfill,NGA\n", encoding="utf-8")
    monkeypatch.setattr(pl, "get_client", lambda config: _FlakyClient(fail_times=99))
    monkeypatch.setattr(pl, "SEARCH_TOOL_RETRY_DELAY_SECONDS", 0.0)

    config = pl.PipelineConfig(
        input_csv=seed, run_dir=tmp_path / "run", run_id="t", site_delay_seconds=0, search_tool_retries=2
    )
    pl.run_search(config)

    rows = list(_csv.DictReader((tmp_path / "run" / "foundry_run_log.csv").open(encoding="utf-8-sig")))
    assert rows[0]["status"].startswith("Search tool failed")


def test_run_log_accumulates_across_retried_searches():
    """A retry only covers the sites it touched; replacing the log would erase the rest."""
    from waste_ai_search.search import merge_run_log

    class _P:
        def exists(self):
            return False

    first = merge_run_log(_P(), [{"site_id": "1", "status": "Succeeded"}, {"site_id": "2", "status": "Failed"}])
    assert [r["site_id"] for r in first] == ["1", "2"]


def test_search_tool_failure_is_distinguished_from_finding_nothing():
    """The API call succeeds either way; only one of them is worth retrying."""
    from waste_ai_search.search import search_tool_failed

    broken = [
        {"search_notes": "I could not complete the required web search because the browsing tool returned an invalid-arguments error."},
        {"search_notes": "The search_query tool did not return usable results in this session."},
        {"search_notes": "the web search tool did not return usable search-result content for verification."},
    ]
    genuine = [
        {"search_notes": "I searched in English and Spanish using the site name and coordinates but found no source-backed data for this facility."},
        {"search_notes": "Could not confidently confirm this exact facility from accessible source-backed material."},
        {"search_notes": ""},
        {},
    ]

    assert all(search_tool_failed(p) for p in broken)
    assert not any(search_tool_failed(p) for p in genuine)


def test_agent_identity_falls_back_from_id_to_name():
    from waste_ai_search.search import agent_identity

    class _Cfg:
        def __init__(self, agent_id, agent_name):
            self.agent_id = agent_id
            self.agent_name = agent_name

    class _Client:
        def __init__(self, cfg):
            self.config = cfg

    assert agent_identity(_Client(_Cfg("", "my-agent"))) == "my-agent"
    assert agent_identity(_Client(_Cfg("asst_1", "my-agent"))) == "asst_1"
    assert agent_identity(_Client(_Cfg("", ""))) == ""
    assert agent_identity(object()) == ""


def test_search_populates_admin_context_from_the_geocode_cache(tmp_path, monkeypatch):
    """The prompt tells the agent to search by municipality, so the field must be filled."""
    import json as _json

    from waste_ai_search import search as pl
    from waste_ai_search.prompt_builder import build_site_prompt

    seed = tmp_path / "seed.csv"
    seed.write_text(
        "site_id,site_name,country_iso3,latitude,longitude\n1,Test Landfill,PHL,14.5,121.0\n",
        encoding="utf-8",
    )
    cache = tmp_path / "geocode.jsonl"
    cache.write_text(
        _json.dumps({"site_id": "1", "municipality": "Olongapo", "admin1": "Zambales"}) + "\n",
        encoding="utf-8",
    )

    seen = {}

    class _Client:
        def search_site(self, site, prompt):
            seen["prompt"] = prompt
            return {"attributes": [], "search_notes": "none found"}

    monkeypatch.setattr(pl, "get_client", lambda config: _Client())
    config = pl.PipelineConfig(
        input_csv=seed,
        run_dir=tmp_path / "run",
        run_id="t",
        site_delay_seconds=0,
        geocode_cache=cache,
        use_geocode_cache=True,
    )
    pl.run_search(config)

    assert "Olongapo" in seen["prompt"]
    assert "Zambales" in seen["prompt"]


def test_admin_context_is_absent_when_the_cache_is_disabled():
    from waste_ai_search.geocoder import enrich_sites_from_cache

    sites = enrich_sites_from_cache([{"site_id": "1", "latitude": "1", "longitude": "2"}], enabled=False)
    assert sites[0]["geocode_status"] == "disabled"
    assert sites[0]["municipality"] == ""


def test_known_closed_sites_get_upfront_closing_year_emphasis():
    from waste_ai_search.prompt_builder import build_site_prompt, looks_inactive

    by_name = {"site_id": "1", "site_name": "Barangay Cauayan Dumpsite (Closed)", "country_iso3": "PHL"}
    by_baseline = {"site_id": "2", "site_name": "Some Dumpsite", "country_iso3": "PHL", "facility_status": "Inactive"}
    plain = {"site_id": "3", "site_name": "Guagua Dumpsite", "country_iso3": "PHL"}

    assert looks_inactive(by_name)
    assert looks_inactive(by_baseline)
    assert not looks_inactive(plain)
    assert "appears to be closed" in build_site_prompt(by_name)
    assert "appears to be closed" not in build_site_prompt(plain)


def test_every_prompt_requires_closing_year_once_closure_is_discovered():
    """Covers the case where closure is only learned during the search itself."""
    from waste_ai_search.prompt_builder import build_site_prompt

    prompt = build_site_prompt({"site_id": "3", "site_name": "Guagua Dumpsite", "country_iso3": "PHL"})
    assert "search for closing_year in the same pass" in prompt


def test_gccs_attributes_are_skipped_where_there_is_no_gas_collection():
    """Asking about methane flaring at a site with no capture system buys a certain nothing."""

    absent = requested_attributes({"site_id": "1", "has_landfill_gas_collection": "FALSE"})
    assert "has_landfill_gas_collection" not in absent  # already known
    assert not [a for a in absent if a.startswith("gccs")]
    # The rest of the gap-fill set is unaffected.
    assert "waste_in_place_metric_tonnes" in absent
    assert "waste_depth" in absent


def test_gccs_attributes_are_requested_where_gas_collection_exists():

    present = requested_attributes({"site_id": "1", "has_landfill_gas_collection": "TRUE"})
    assert len([a for a in present if a.startswith("gccs")]) == 7


def test_unknown_gas_collection_defers_gccs_rather_than_asking():
    """Deferred, not dropped: see test_seed_refresh for the pass-2 pickup."""
    from waste_ai_search.prompt_builder import has_gas_collection, requested_attributes

    unknown = {"site_id": "1"}
    assert not has_gas_collection(unknown)
    requested = requested_attributes(unknown)
    assert not [a for a in requested if a.startswith("gccs")]
    # has_landfill_gas_collection itself is still asked - that is what unlocks pass 2.
    assert "has_landfill_gas_collection" in requested


PILOT_FIXTURE = Path(__file__).parent / "fixtures" / "osm_sites_sample.csv"


def test_pilot_size_samples_a_spread_where_limit_takes_the_first_n():
    """--limit follows seed order, which clusters by country; --pilot-size spreads deliberately."""
    from waste_ai_search.run_context import PipelineConfig
    from waste_ai_search.search import select_sites

    sites, _headers = load_sites(PILOT_FIXTURE)
    base = dict(input_csv=PILOT_FIXTURE, run_dir=PILOT_FIXTURE.parent, run_id="t")

    # The fixture leaves 4 eligible sites once the country rules apply, so sample 3.
    first_n = select_sites(sites, PipelineConfig(limit=3, **base))
    spread = select_sites(sites, PipelineConfig(pilot_size=3, **base))

    # Both run after the country rules, so --limit takes the first 5 of what actually remains:
    # no US sites, and no site left with nothing to ask (a Brazilian row with an exact location).
    from waste_ai_search.prompt_builder import requested_attributes as _req

    eligible = [s for s in sites if s["country_iso3"] != "USA" and _req(s)]
    assert [s["site_id"] for s in first_n] == [s["site_id"] for s in eligible[:3]]
    assert "USA" not in {s["country_iso3"] for s in first_n + spread}
    assert len(spread) == 3
    assert all(row["pilot_selection_reason"] for row in spread)
    # deterministic
    assert [s["site_id"] for s in spread] == [
        s["site_id"] for s in select_sites(sites, PipelineConfig(pilot_size=3, **base))
    ]


def test_pilot_size_composes_with_the_country_filter():
    from waste_ai_search.run_context import PipelineConfig
    from waste_ai_search.search import select_sites

    sites, _headers = load_sites(PILOT_FIXTURE)
    chosen = select_sites(
        sites,
        PipelineConfig(input_csv=PILOT_FIXTURE, run_dir=PILOT_FIXTURE.parent, run_id="t", iso3=["NGA"], pilot_size=3),
    )
    assert chosen
    assert {row["country_iso3"] for row in chosen} == {"NGA"}


def seed_with_countries(tmp_path, *countries, is_location_exact="FALSE"):
    """`is_location_exact` defaults to FALSE so a Brazilian row is searchable unless stated."""
    path = tmp_path / "seed.csv"
    lines = ["site_id,site_name,country_iso3,is_location_exact,latitude,longitude"]
    for index, iso3 in enumerate(countries, start=1):
        lines.append(f"{index},Landfill {index},{iso3},{is_location_exact},1.0,2.0")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def selected_ids(seed, **kwargs):
    import contextlib
    import io

    from waste_ai_search.input_loader import load_sites
    from waste_ai_search.run_context import PipelineConfig
    from waste_ai_search.search import select_sites

    sites, _headers = load_sites(seed)
    config = PipelineConfig(input_csv=seed, run_dir=seed.parent, run_id="t", **kwargs)
    with contextlib.redirect_stdout(io.StringIO()):
        return [s["site_id"] for s in select_sites(sites, config)]


def test_us_sites_are_excluded_by_default(tmp_path):
    """US facilities are already Tier 1 covered by GHGRP and LMOP."""
    seed = seed_with_countries(tmp_path, "USA", "PHL", "USA", "BRA")
    # The Brazilian row survives only because the fixture flags its location inexact.
    assert selected_ids(seed) == ["2", "4"]


def test_brazil_with_an_exact_location_drops_out_of_selection_entirely(tmp_path):
    seed = seed_with_countries(tmp_path, "USA", "PHL", "BRA", is_location_exact="TRUE")
    assert selected_ids(seed) == ["2"]


def test_the_exclusion_is_overridable(tmp_path):
    seed = seed_with_countries(tmp_path, "USA", "PHL")
    assert selected_ids(seed, include_excluded_countries=True) == ["1", "2"]


def test_naming_the_country_explicitly_beats_the_default(tmp_path):
    """Asking for USA and getting nothing back would be worse than useless."""
    seed = seed_with_countries(tmp_path, "USA", "PHL")
    assert selected_ids(seed, iso3=["USA"]) == ["1"]


def test_naming_site_ids_explicitly_beats_the_default(tmp_path):
    seed = seed_with_countries(tmp_path, "USA", "PHL")
    assert selected_ids(seed, site_ids=["1"]) == ["1"]


def test_an_unrelated_country_filter_still_excludes_the_us(tmp_path):
    seed = seed_with_countries(tmp_path, "USA", "PHL")
    assert selected_ids(seed, iso3=["USA", "PHL"]) == ["1", "2"]  # USA named -> kept
    assert selected_ids(seed, iso3=["PHL"]) == ["2"]


def test_brazil_searches_coordinates_only_where_the_location_is_inexact():
    from waste_ai_search.prompt_builder import coordinates_only, requested_attributes

    bra = {"site_id": "1", "country_iso3": "BRA", "is_location_exact": "FALSE"}
    assert coordinates_only(bra)
    assert requested_attributes(bra) == ["found_latitude", "found_longitude"]


def test_brazil_with_an_exact_location_is_searched_for_nothing():
    """Every other Brazilian attribute is government-sourced; searching risks overwriting it."""
    from waste_ai_search.prompt_builder import requested_attributes

    exact = {
        "site_id": "1",
        "country_iso3": "BRA",
        "is_location_exact": "TRUE",
        "has_landfill_gas_collection": "TRUE",
        "facility_status": "",
        "area_square_meters": "",
    }
    assert requested_attributes(exact) == []


def test_unknown_location_accuracy_is_not_treated_as_inexact():
    from waste_ai_search.prompt_builder import location_is_inexact, requested_attributes

    unknown = {"site_id": "1", "country_iso3": "BRA", "is_location_exact": ""}
    assert not location_is_inexact(unknown)
    assert requested_attributes(unknown) == []


def test_brazilian_sites_are_scoped_to_coordinates_or_skipped():
    """The COORDINATES_ONLY_ISO3 rule is live now; it used to match nothing.

    This test previously asserted that NO Brazilian site was searched, because `is_location_exact`
    was TRUE corpus-wide in the seed of the day. Regenerating the seed changed that: Brazil went
    from 303 sites, all exact, to 4,225 of which 3,913 are flagged inexact. The rule those
    facilities were written for now fires for every one of them.

    What must hold either way is the shape of the rule: an inexact Brazilian site is searched for
    coordinates and nothing else, and an exact one is not searched at all.
    """
    import csv
    from pathlib import Path as _Path

    from waste_ai_search.prompt_builder import location_is_inexact, requested_attributes
    from waste_ai_search.schema import COORDINATE_ATTRIBUTES

    seed = _Path(__file__).parents[1] / "inputs" / "consolidated_sites.csv"
    if not seed.exists():
        return
    with seed.open(encoding="utf-8-sig") as handle:
        bra = [r for r in csv.DictReader(handle) if r["country_iso3"] == "BRA"]
    assert bra, "expected Brazilian sites in the seed"

    for site in bra:
        requested = requested_attributes(site)
        if location_is_inexact(site):
            assert requested == list(COORDINATE_ATTRIBUTES), (
                f"BRA site {site['site_id']} should be scoped to coordinates alone, got {requested}"
            )
        else:
            assert requested == [], (
                f"BRA site {site['site_id']} has an exact location and should not be searched"
            )


def test_sites_with_nothing_to_ask_are_dropped_from_selection(tmp_path):
    """Querying a site with zero attributes spends a request to receive nothing."""
    seed = tmp_path / "seed.csv"
    seed.write_text(
        "site_id,site_name,country_iso3,is_location_exact,latitude,longitude\n"
        "1,Aterro,BRA,TRUE,1.0,2.0\n"
        "2,Aterro Inexato,BRA,FALSE,1.0,2.0\n"
        "3,Philippine Site,PHL,TRUE,1.0,2.0\n",
        encoding="utf-8",
    )
    assert selected_ids(seed) == ["2", "3"]


def test_other_countries_are_unaffected_by_the_brazil_rule():
    from waste_ai_search.prompt_builder import coordinates_only, requested_attributes

    phl = {"site_id": "1", "site_name": "Y", "country_iso3": "PHL"}
    assert not coordinates_only(phl)
    assert len(requested_attributes(phl)) > 2


class _ScriptedClient:
    """Answers pass 1 with a gas-collection discovery, pass 2 with a GCCS value."""

    def __init__(self):
        self.prompts = []

    def search_site(self, site, prompt):  # noqa: ARG002
        self.prompts.append(prompt)
        source = {
            "source_title": "Permit",
            "publisher": "Regulator",
            "source_type": "Regulator",
            "publication_date": "2024-01-01",
            "url": "https://reg.example/permit",
        }
        common = {"value_basis": "Direct", "confidence_score": "High", "value_date": "2024", "sources": [source]}
        if "gccs_ch4_flared_metric_tonnes" in prompt:
            return {"attributes": [
                {"attribute_name": "gccs_ch4_flared_metric_tonnes", "value": "1200",
                 "unit": "metric tonnes", **common}
            ]}
        return {"attributes": [
            {"attribute_name": "has_landfill_gas_collection", "value": "Yes", "unit": "", **common}
        ]}


def test_run_command_follows_up_on_newly_discovered_gas_collection(tmp_path, monkeypatch):
    """The whole point: no manual reseed, no second run id, no second input file."""
    import csv as _csv

    from waste_ai_search import arbitrate as ar
    from waste_ai_search import cli, search as se
    from waste_ai_search.run_context import PipelineConfig

    seed = tmp_path / "seed.csv"
    seed.write_text(
        "site_id,internal_facility_id,site_name,country_iso3,latitude,longitude,"
        "is_location_exact,has_landfill_gas_collection,reference_year\n"
        "1,1,Test Landfill,PHL,6.5,3.3,TRUE,,2022\n",
        encoding="utf-8",
    )
    client = _ScriptedClient()
    monkeypatch.setattr(se, "get_client", lambda config: client)
    monkeypatch.setattr(ar, "write_review_workbook", lambda path, **kw: path)

    run_dir = tmp_path / "run"
    cli.run_everything(
        PipelineConfig(input_csv=seed, run_dir=run_dir, run_id="t", site_delay_seconds=0)
    )

    # pass 1 did not ask for GCCS; pass 2 did
    assert "gccs_ch4_flared_metric_tonnes" not in client.prompts[0]
    assert len(client.prompts) == 2
    assert "gccs_ch4_flared_metric_tonnes" in client.prompts[1]

    # both passes are cached side by side, neither overwritten
    cached = sorted(p.name for p in (run_dir / "raw_foundry_responses").glob("*.json"))
    assert cached == ["site_1.json", "site_1__gccs.json"]

    # and the final arbitration sees both findings for the one site
    resolved = list(_csv.DictReader((run_dir / "resolved.csv").open(encoding="utf-8-sig")))
    found = {r["attribute_name"]: r["resolved_value"] for r in resolved if r["resolved_value"]}
    assert found.get("has_landfill_gas_collection") == "TRUE"
    assert found.get("gccs_ch4_flared_metric_tonnes") == "1200"


def test_run_command_skips_the_follow_up_when_nothing_was_unlocked(tmp_path, monkeypatch):
    from waste_ai_search import arbitrate as ar
    from waste_ai_search import cli, search as se
    from waste_ai_search.run_context import PipelineConfig

    seed = tmp_path / "seed.csv"
    seed.write_text(
        "site_id,internal_facility_id,site_name,country_iso3,latitude,longitude,"
        "is_location_exact,has_landfill_gas_collection,reference_year\n"
        "1,1,Test Landfill,PHL,6.5,3.3,TRUE,FALSE,2022\n",
        encoding="utf-8",
    )

    class _Quiet:
        def __init__(self):
            self.calls = 0

        def search_site(self, site, prompt):  # noqa: ARG002
            self.calls += 1
            return {"attributes": [], "search_notes": "nothing found"}

    client = _Quiet()
    monkeypatch.setattr(se, "get_client", lambda config: client)
    monkeypatch.setattr(ar, "write_review_workbook", lambda path, **kw: path)

    cli.run_everything(
        PipelineConfig(input_csv=seed, run_dir=tmp_path / "run", run_id="t", site_delay_seconds=0)
    )
    assert client.calls == 1


def test_merge_payloads_combines_passes():
    from waste_ai_search.arbitrate import merge_payloads

    merged = merge_payloads([
        {"site_id": "1", "attributes": [{"attribute_name": "a"}], "search_notes": "first"},
        {"site_id": "1", "attributes": [{"attribute_name": "b"}], "search_notes": "second"},
    ])
    assert [a["attribute_name"] for a in merged["attributes"]] == ["a", "b"]
    assert merged["search_notes"] == "first | second"
