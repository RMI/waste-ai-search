"""F30: the full run is searched in six country batches, most-needed first."""
from __future__ import annotations

from pathlib import Path

from waste_ai_search.schema import COUNTRY_BATCHES, LAST_BATCH, in_batch


def _sites(*countries):
    return [
        {"site_id": str(i), "site_name": f"Site {i}", "country_iso3": iso3, "latitude": "1", "longitude": "1"}
        for i, iso3 in enumerate(countries, start=1)
    ]


def test_no_country_is_in_two_batches():
    listed = [iso3 for countries in COUNTRY_BATCHES.values() for iso3 in countries]
    assert len(listed) == len(set(listed))
    assert LAST_BATCH == 6


def test_the_last_batch_takes_every_country_not_listed():
    assert in_batch("can", 1)
    assert not in_batch("CAN", LAST_BATCH)
    assert in_batch("ARG", LAST_BATCH)  # unlisted, so it can never be left out
    assert not in_batch("ARG", 1)


def test_a_batch_selects_only_its_countries_and_keeps_default_exclusions():
    from waste_ai_search.run_context import PipelineConfig
    from waste_ai_search.search import select_sites

    sites = _sites("CAN", "MEX", "ARG", "USA", "GBR")
    config = lambda batch: PipelineConfig(input_csv=None, run_dir=Path("r"), run_id="t", batch=batch)

    assert [s["country_iso3"] for s in select_sites(sites, config(1))] == ["CAN", "GBR"]
    assert [s["country_iso3"] for s in select_sites(sites, config(3))] == ["MEX"]
    assert [s["country_iso3"] for s in select_sites(sites, config(LAST_BATCH))] == ["ARG"]  # USA stays excluded


def test_the_cli_passes_the_batch_through():
    from waste_ai_search.cli import build_parser, make_config

    for command in ("search", "run"):
        assert make_config(build_parser().parse_args([command, "--run-id", "t", "--batch", "2"])).batch == 2
        assert make_config(build_parser().parse_args([command, "--run-id", "t"])).batch == 0
