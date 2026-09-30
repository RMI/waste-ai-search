"""Where a run's seed comes from.

The database is the source of truth, so `search` and `run` read it directly rather than a
checked-in export that goes stale the moment consolidation moves. A CSV is still accepted for two
cases that genuinely need a file: pinning a corpus, and the gas-collection follow-up, which
rewrites the seed between passes.

These are offline. `input_csv is None` selects the database path, so the tests that exercise it
stub `load_seed_sites` rather than reaching for a connection.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from waste_ai_search.input_loader import (
    describe_seed,
    resolve_sites,
    run_seed_path,
    write_csv_records,
)
from waste_ai_search.run_context import PipelineConfig


def _config(tmp_path: Path, input_csv: Path | None = None, iso3: list[str] | None = None):
    return PipelineConfig(
        input_csv=input_csv,
        run_dir=tmp_path / "run",
        run_id="t",
        iso3=iso3 or [],
    )


# --- choosing a source ----------------------------------------------------------------------
def test_a_csv_path_is_read_from_disk(tmp_path):
    seed = tmp_path / "seed.csv"
    write_csv_records(
        seed,
        [{"site_id": "1", "site_name": "Aterro", "country_iso3": "BRA"}],
        ["site_id", "site_name", "country_iso3"],
    )

    sites, headers = resolve_sites(_config(tmp_path, input_csv=seed))

    assert [s["site_id"] for s in sites] == ["1"]
    assert "site_name" in headers


def test_no_csv_means_the_database(tmp_path, monkeypatch):
    import waste_ai_search.seed_source as seed_source

    called = {}

    def fake_load(iso3=None, **kwargs):
        called["iso3"] = iso3
        return [{"site_id": "9", "site_name": "From DB", "country_iso3": "MEX"}]

    monkeypatch.setattr(seed_source, "load_seed_sites", fake_load)
    sites, headers = resolve_sites(_config(tmp_path, iso3=["MEX"]))

    assert [s["site_id"] for s in sites] == ["9"]
    assert headers, "database seeding must still supply headers for the snapshot"


def test_country_filter_is_pushed_into_the_query(tmp_path, monkeypatch):
    """Filtering after the load would read all 19,492 facilities to keep one country's worth."""
    import waste_ai_search.seed_source as seed_source

    seen = {}

    def fake_load(iso3=None, **kwargs):
        seen["iso3"] = iso3
        return []

    monkeypatch.setattr(seed_source, "load_seed_sites", fake_load)
    resolve_sites(_config(tmp_path, iso3=["MEX", "ARG"]))

    assert seen["iso3"] == ["MEX", "ARG"]


def test_no_country_filter_passes_none_rather_than_an_empty_list(tmp_path, monkeypatch):
    """An empty list would render as `iso3c_plus = ANY('{}')` and match nothing."""
    import waste_ai_search.seed_source as seed_source

    seen = {}

    def fake_load(iso3=None, **kwargs):
        seen["iso3"] = iso3
        return []

    monkeypatch.setattr(seed_source, "load_seed_sites", fake_load)
    resolve_sites(_config(tmp_path))

    assert seen["iso3"] is None


# --- the reproducibility snapshot (read back, not just written) -----------------------------
def _stub_database(monkeypatch, rows):
    """Serve `rows` on the first database read and fail loudly on any later one."""
    import waste_ai_search.seed_source as seed_source

    calls = {"n": 0}

    def fake_load(iso3=None, **kwargs):
        calls["n"] += 1
        if calls["n"] > 1:
            raise AssertionError("queried the database again instead of reading the run snapshot")
        return rows

    monkeypatch.setattr(seed_source, "load_seed_sites", fake_load)
    return calls


def test_the_first_database_read_writes_the_run_snapshot(tmp_path, monkeypatch):
    _stub_database(monkeypatch, [{"site_id": "1", "site_name": "A"}])
    config = _config(tmp_path)

    resolve_sites(config)

    assert run_seed_path(config).exists()


def test_every_later_phase_reads_the_snapshot_not_the_database(tmp_path, monkeypatch):
    """Arbitration, the refresh between passes and a resumed search must all see the corpus the
    search ran against. A second database query would fail the stub."""
    calls = _stub_database(monkeypatch, [{"site_id": "1", "site_name": "A"}])
    config = _config(tmp_path)

    first, _ = resolve_sites(config)       # search
    second, _ = resolve_sites(config)      # arbitrate
    third, _ = resolve_sites(config)       # refresh / pass-2 arbitrate

    assert calls["n"] == 1
    assert [s["site_id"] for s in first] == [s["site_id"] for s in second] == [s["site_id"] for s in third]


def test_a_resumed_run_does_not_overwrite_its_snapshot(tmp_path, monkeypatch):
    """Cached responses from the first attempt must stay matched to the corpus they searched."""
    _stub_database(monkeypatch, [{"site_id": "1", "site_name": "Original"}])
    config = _config(tmp_path)
    resolve_sites(config)
    before = run_seed_path(config).read_text()

    resolve_sites(config)  # the resume

    assert run_seed_path(config).read_text() == before


def test_standalone_arbitrate_needs_no_database_once_a_snapshot_exists(tmp_path, monkeypatch):
    """The CLI promises arbitrate is offline. With a snapshot present it must not reach the DB."""
    import waste_ai_search.seed_source as seed_source

    config = _config(tmp_path)
    write_csv_records(run_seed_path(config), [{"site_id": "7", "site_name": "Pinned"}], ["site_id", "site_name"])

    def no_database(**kwargs):
        raise AssertionError("arbitrate reached for the database")

    monkeypatch.setattr(seed_source, "load_seed_sites", no_database)
    sites, _ = resolve_sites(config)

    assert [s["site_id"] for s in sites] == ["7"]


def test_a_csv_seeded_run_writes_no_snapshot(tmp_path):
    """The supplied file already is the record; copying it into the run dir adds nothing."""
    seed = tmp_path / "given.csv"
    write_csv_records(seed, [{"site_id": "1"}], ["site_id"])
    config = _config(tmp_path, input_csv=seed)

    resolve_sites(config)

    assert not run_seed_path(config).exists()


def test_describe_seed_reports_where_the_run_is_pinned(tmp_path, monkeypatch):
    _stub_database(monkeypatch, [{"site_id": "1"}])
    config = _config(tmp_path)

    assert describe_seed(config) == "consolidation.consolidated_facility (live)"
    resolve_sites(config)
    assert "pinned in" in describe_seed(config)
    assert str(run_seed_path(config)) in describe_seed(config)


# --- the follow-up pass still needs a file --------------------------------------------------
def test_pass_two_can_point_the_config_at_a_rewritten_seed(tmp_path):
    """The gas-collection follow-up writes a modified seed and re-runs against it."""
    from dataclasses import replace

    rewritten = tmp_path / "refreshed_seed.csv"
    write_csv_records(rewritten, [{"site_id": "5", "site_name": "Unlocked"}], ["site_id", "site_name"])

    followup = replace(_config(tmp_path), input_csv=rewritten)
    sites, _ = resolve_sites(followup)

    assert [s["site_id"] for s in sites] == ["5"]


def test_input_csv_is_optional_on_the_config():
    """The dataclass must accept None, which is what 'seed from the database' means."""
    config = PipelineConfig(input_csv=None, run_dir=Path("/tmp/x"), run_id="t")
    assert config.input_csv is None
