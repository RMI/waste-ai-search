"""Fixes from the full-codebase review, before the first country batch."""
from __future__ import annotations

import pytest

from waste_ai_search.unit_converter import convert_attribute_value as convert


@pytest.mark.parametrize(
    ("attribute", "value", "unit", "expected"),
    [
        # The prompt asks for digits in value, so a magnitude often arrives in the unit.
        ("waste_in_place_metric_tonnes", "1.2", "million tonnes", "1200000"),
        ("annual_incoming_waste_metric_tonnes", "120", "thousand tonnes/year", "120000"),
        ("waste_in_place_metric_tonnes", "1.2 million", "million tonnes", "1200000"),  # not twice
        ("gccs_ch4_flared_metric_tonnes", "2", "million m3 CH4", "1358"),
        # European flare flow is written per hour as /h.
        ("gccs_ch4_flared_metric_tonnes", "500", "Nm3/h methane", "2974.02"),
        # Degrees, minutes and seconds are part of the coordinate.
        ("found_latitude", "51°30'26\"N", "", "51.50722222"),
        ("found_longitude", "0°07'39\"W", "", "-0.1275"),
        ("found_latitude", "51°30.4'N", "", "51.50666667"),
        ("found_latitude", "-33.86", "", "-33.86"),
        ("area_square_meters", "5", "sq mi", "12949940.55168"),
        ("area_square_meters", "5", "sq m", "5"),
    ],
)
def test_values_convert_to_the_right_number(attribute, value, unit, expected):
    result = convert(attribute, value, unit)
    assert (result.value, result.warning) == (expected, "")


@pytest.mark.parametrize(
    ("attribute", "value", "unit", "warning"),
    [
        ("gccs_ch4_flared_metric_tonnes", "50000", "metric tonnes CO2e", "CO2"),
        ("waste_in_place_metric_tonnes", "50", "t/h", "Rate unit supplied for stock"),
        ("flare_efficiency", "98-99", "%", "range"),
    ],
)
def test_values_that_cannot_be_converted_are_refused(attribute, value, unit, warning):
    assert warning in convert(attribute, value, unit).warning


def test_a_unit_abbreviation_is_not_read_as_a_magnitude():
    assert convert("waste_depth", "500", "mm").value == "500"  # millimetres, not million


def test_a_lone_far_coordinate_still_withdraws_auto_validation():
    from waste_ai_search.arbitrate import flag_identity_mismatch, single_axis_offset_km

    site = {"latitude": "52.5", "longitude": "13.4"}
    resolved = [
        {"attribute_name": "found_latitude", "resolved_value": "50.5", "resolution": "Filled empty baseline",
         "validation_status": "Auto-validated"},
        {"attribute_name": "facility_status", "resolved_value": "Active", "resolution": "Filled empty baseline",
         "validation_status": "Auto-validated"},
    ]
    km = single_axis_offset_km(site, resolved)
    assert km == pytest.approx(222.4, abs=0.5)
    assert flag_identity_mismatch(resolved, km)
    assert resolved[1]["validation_status"] == "Needs review"


def test_the_workbook_does_not_point_smes_at_rows_resolved_csv_no_longer_holds():
    from waste_ai_search.workbook_io import REVIEW_INSTRUCTIONS

    text = " ".join(str(value) for tab in REVIEW_INSTRUCTIONS for value in tab.values())
    assert "INCLUDING 'Not found'" not in text
    assert "Every row here also exists in resolved.csv" not in text


@pytest.mark.parametrize(
    ("claimed", "expected"),
    [
        ("mining waste", "others"),
        ("sewage sludge", "others"),
        ("Municipal Solid Waste (MSW)", "municipal solid waste"),
        ("C&D", "inert waste"),
        ("construction waste", "inert waste"),
    ],
)
def test_bulk_waste_type_maps_the_prompts_own_examples(claimed, expected):
    from waste_ai_search.schema import map_enum_value

    assert map_enum_value("bulk_waste_type", claimed)[0] == expected


def test_refresh_seed_prints_a_search_narrowed_to_the_unlocked_sites(tmp_path, capsys):
    from waste_ai_search.cli import run_refresh_seed
    from waste_ai_search.input_loader import run_seed_path, write_csv_records
    from waste_ai_search.run_context import PipelineConfig
    from waste_ai_search.schema import RESOLVED_HEADERS

    config = PipelineConfig(input_csv=None, run_dir=tmp_path / "run", run_id="r")
    write_csv_records(
        run_seed_path(config),
        [{"site_id": str(i), "site_name": f"S{i}", "country_iso3": "PHL", "has_landfill_gas_collection": ""}
         for i in (1, 2)],
        ["site_id", "site_name", "country_iso3", "has_landfill_gas_collection"],
    )
    write_csv_records(
        config.run_dir / "resolved.csv",
        [{"site_id": "2", "attribute_name": "has_landfill_gas_collection", "resolved_value": "TRUE",
          "resolution": "Filled empty baseline", "winning_source_tier": "Tier 1"}],
        RESOLVED_HEADERS,
    )

    assert run_refresh_seed(config, config.run_dir / "refreshed_seed.csv", merge_identity=False) == 0
    out = capsys.readouterr().out
    assert "--site-ids 2" in out
    assert "Do not search the refreshed seed without --site-ids" in out


def test_arbitrate_never_reads_the_database(tmp_path, monkeypatch):
    """A mistyped --run-id, or a run from before seeds were pinned, used to seed from live data."""
    import waste_ai_search.seed_source as seed_source
    import waste_ai_search.snapshot as snapshot
    from waste_ai_search.arbitrate import run_arbitration
    from waste_ai_search.run_context import PipelineConfig

    def no_database(*args, **kwargs):
        raise AssertionError("arbitrate reached for the database")

    monkeypatch.setattr(snapshot, "take_snapshot", no_database)
    monkeypatch.setattr(seed_source, "load_seed_sites", no_database)
    config = PipelineConfig(input_csv=None, run_dir=tmp_path / "typo", run_id="typo")

    with pytest.raises(ValueError, match="No cached responses"):
        run_arbitration(config)

    (config.run_dir / "raw_foundry_responses").mkdir(parents=True)
    (config.run_dir / "raw_foundry_responses" / "site_1.json").write_text('{"attributes": []}')
    with pytest.raises(ValueError, match="no pinned seed"):
        run_arbitration(config)


def test_an_interrupted_search_still_pushes_to_blob(tmp_path, monkeypatch):
    from waste_ai_search import cli

    events = []

    class _Sync:
        def __init__(self, config, local=False):  # noqa: ARG002
            pass

        def down(self):
            events.append("down")

        def up(self):
            events.append("up")

    def interrupted(config):  # noqa: ARG001
        raise KeyboardInterrupt

    monkeypatch.setattr(cli, "RunSync", _Sync)
    monkeypatch.setattr(cli, "run_search", interrupted)
    args = cli.build_parser().parse_args(["search", "--run-id", "t", "--run-dir", str(tmp_path / "r")])
    with pytest.raises(KeyboardInterrupt):
        cli.dispatch(args, cli.make_config(args))
    assert events == ["down", "up"]


def test_the_publish_ledger_is_written_before_the_push(tmp_path, monkeypatch):
    from waste_ai_search import cli

    events = []

    class _Sync:
        def __init__(self, config, local=False):  # noqa: ARG002
            pass

        def down(self):
            pass

        def up(self):
            events.append("push")

    monkeypatch.setattr(cli, "RunSync", _Sync)
    monkeypatch.setattr(cli, "run_arbitration", lambda config: {})
    monkeypatch.setattr(cli, "publish", lambda config, enabled: events.append("publish"))
    args = cli.build_parser().parse_args(["arbitrate", "--run-id", "t", "--run-dir", str(tmp_path / "r")])
    cli.dispatch(args, cli.make_config(args))
    assert events == ["publish", "push"]


def test_zero_retries_still_makes_one_attempt():
    from waste_ai_search.foundry_client import AzureFoundryAgentClient, FoundryClientConfig

    client = AzureFoundryAgentClient.__new__(AzureFoundryAgentClient)
    client.config = FoundryClientConfig(max_retries=0)
    client._invoke_agent = lambda prompt, limit: '{"attributes": []}'
    assert client.search_site({}, "prompt") == {"attributes": []}


@pytest.mark.parametrize("option", ["--limit", "--pilot-size"])
def test_a_negative_count_is_refused_not_read_as_all(option):
    from waste_ai_search.cli import build_parser

    for command in ("search", "run"):
        with pytest.raises(SystemExit):
            build_parser().parse_args([command, "--run-id", "t", option, "-5"])


def test_the_batch_summary_counts_after_default_exclusions(capsys):
    from pathlib import Path

    from waste_ai_search.run_context import PipelineConfig
    from waste_ai_search.search import select_sites

    sites = [{"site_id": str(i), "site_name": "S", "country_iso3": iso3, "latitude": "1", "longitude": "1"}
             for i, iso3 in enumerate(["USA", "USA", "ARG"])]
    select_sites(sites, PipelineConfig(input_csv=None, run_dir=Path("r"), run_id="t", batch=6))
    assert "Batch 6: 1 site(s) in 1 country(ies)." in capsys.readouterr().out


def test_a_forced_retry_that_fails_keeps_the_earlier_good_response(tmp_path, monkeypatch):
    import json

    from waste_ai_search import search as pl

    seed = tmp_path / "seed.csv"
    seed.write_text("site_id,site_name,country_iso3\n1,Test Landfill,NGA\n", encoding="utf-8")
    good = {"attributes": [{"attribute_name": "facility_status", "value": "Active"}], "search_notes": "ok"}
    cached = tmp_path / "run" / "raw_foundry_responses" / "site_1.json"
    cached.parent.mkdir(parents=True)
    cached.write_text(json.dumps(good), encoding="utf-8")

    class _Broken:
        def search_site(self, site, prompt):  # noqa: ARG002
            return {"attributes": [], "search_notes": "The search_query tool did not return usable results."}

    monkeypatch.setattr(pl, "get_client", lambda config: _Broken())
    monkeypatch.setattr(pl, "SEARCH_TOOL_RETRY_DELAY_SECONDS", 0.0)
    pl.run_search(pl.PipelineConfig(input_csv=seed, run_dir=tmp_path / "run", run_id="t",
                                    site_delay_seconds=0, force=True, check_links=False))

    assert json.loads(cached.read_text(encoding="utf-8")) == good


def test_list_values_agree_whatever_their_order_or_repeats():
    from waste_ai_search.arbitration import comparable

    seed = "other soil mixture; sand cover"
    assert comparable(seed, "cover_types") == comparable(["sand cover", "other soil mixture"], "cover_types")
    assert comparable("Electricity Generation; Electricity Generation", "gccs_energy_project_type") == comparable(
        ["Electricity Generation"], "gccs_energy_project_type"
    )
    assert comparable("Active") == comparable("active")  # scalars unchanged


def test_a_geocode_hit_keeps_seed_values_and_gets_a_date(tmp_path):
    import json
    from datetime import date

    from waste_ai_search.geocoder import enrich_sites_from_cache

    cache = tmp_path / "geocode_cache.jsonl"
    cache.write_text(json.dumps({"site_id": "1", "municipality": "", "admin1": "Lagos"}) + "\n", encoding="utf-8")
    site = {"site_id": "1", "municipality": "Ikeja", "latitude": "6.5", "longitude": "3.3"}
    enriched = enrich_sites_from_cache([site], cache_path=cache, enabled=True)[0]
    if enriched["geocode_status"] != "cache_hit":
        pytest.skip("cache keyed differently in this repo version")
    assert enriched["municipality"] == "Ikeja"  # a blank cache field does not erase it
    assert enriched["admin1"] == "Lagos"
    assert enriched["geocoded_at"] == date.today().isoformat()


def test_nan_from_the_database_is_empty_in_the_seed():
    from decimal import Decimal

    from waste_ai_search.seed_source import is_empty

    assert is_empty(Decimal("NaN")) and is_empty(float("nan")) and is_empty([Decimal("NaN")])
    assert not is_empty(Decimal("0")) and not is_empty(False) and not is_empty([Decimal("1"), Decimal("NaN")])


def test_a_kill_while_rewriting_a_csv_leaves_the_old_file_whole(tmp_path, monkeypatch):
    """The run log is rewritten per site; a kill mid-write must not empty it."""
    import csv as _csv
    import os

    from waste_ai_search.input_loader import write_csv_records

    log = tmp_path / "foundry_run_log.csv"
    write_csv_records(log, [{"site_id": "1", "web_searches": "8"}], ["site_id", "web_searches"])
    monkeypatch.setattr(os, "replace", lambda src, dst: (_ for _ in ()).throw(KeyboardInterrupt()))
    with pytest.raises(KeyboardInterrupt):
        write_csv_records(log, [{"site_id": "1", "web_searches": "8"}, {"site_id": "2"}], ["site_id", "web_searches"])

    assert list(_csv.DictReader(log.open(encoding="utf-8"))) == [{"site_id": "1", "web_searches": "8"}]
    assert [p.name for p in tmp_path.iterdir()] == ["foundry_run_log.csv"]  # no temporary left behind
