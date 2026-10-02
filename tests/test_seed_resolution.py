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


def test_a_csv_seeded_run_pins_the_supplied_file_byte_for_byte(tmp_path):
    """The supplied file is often temporary; once it is gone the run could not be re-arbitrated."""
    seed = tmp_path / "given.csv"
    write_csv_records(seed, [{"site_id": "1", "site_name": "A"}], ["site_id", "site_name"])
    config = _config(tmp_path, input_csv=seed)

    resolve_sites(config)

    assert run_seed_path(config).read_bytes() == seed.read_bytes()


def test_a_csv_seeded_run_survives_the_supplied_file_being_deleted(tmp_path):
    """The case that prompted this: a seed in a scratch directory that is later cleared."""
    seed = tmp_path / "scratch" / "given.csv"
    write_csv_records(seed, [{"site_id": "9", "site_name": "Pinned"}], ["site_id", "site_name"])
    resolve_sites(_config(tmp_path, input_csv=seed))
    seed.unlink()

    sites, _ = resolve_sites(_config(tmp_path))  # a later `arbitrate`, no --input-csv
    assert [s["site_id"] for s in sites] == ["9"]


def test_the_pass_two_seed_inside_the_run_never_replaces_the_original(tmp_path):
    """The follow-up rewrites the seed into the run dir and points the config at it. The run's
    record must stay the corpus pass 1 searched, not the refreshed one."""
    original = tmp_path / "given.csv"
    write_csv_records(original, [{"site_id": "1", "site_name": "Original"}], ["site_id", "site_name"])
    config = _config(tmp_path, input_csv=original)
    resolve_sites(config)
    before = run_seed_path(config).read_bytes()

    refreshed = config.run_dir / "refreshed_seed.csv"
    write_csv_records(refreshed, [{"site_id": "1", "site_name": "Refreshed"}], ["site_id", "site_name"])
    resolve_sites(_config(tmp_path, input_csv=refreshed))

    assert run_seed_path(config).read_bytes() == before


def test_a_first_pass_seeded_from_a_file_inside_the_run_writes_no_snapshot(tmp_path):
    """A file already in the run directory is the run's own; copying it would add nothing."""
    config = _config(tmp_path)
    inside = config.run_dir / "refreshed_seed.csv"
    write_csv_records(inside, [{"site_id": "1"}], ["site_id"])

    resolve_sites(_config(tmp_path, input_csv=inside))

    assert not run_seed_path(config).exists()


def test_a_different_csv_on_a_pinned_run_is_refused_not_silently_used(tmp_path):
    """Copilot review: the rerun searched the new file while seed.csv kept the old one, so a later
    arbitrate loaded the wrong corpus. A different file now stops the run instead."""
    config = _config(tmp_path)
    first = tmp_path / "first.csv"
    write_csv_records(first, [{"site_id": "keep"}], ["site_id"])
    resolve_sites(_config(tmp_path, input_csv=first))
    before = run_seed_path(config).read_bytes()

    other = tmp_path / "other.csv"
    write_csv_records(other, [{"site_id": "new"}], ["site_id"])
    with pytest.raises(ValueError, match="pinned to .* Use a new --run-id"):
        resolve_sites(_config(tmp_path, input_csv=other))

    assert run_seed_path(config).read_bytes() == before


def test_rerunning_with_the_same_csv_reads_the_snapshot(tmp_path):
    """A resumed search with the identical file is fine, and every phase sees one corpus."""
    seed = tmp_path / "given.csv"
    write_csv_records(seed, [{"site_id": "1", "site_name": "A"}], ["site_id", "site_name"])
    first, _ = resolve_sites(_config(tmp_path, input_csv=seed))
    again, _ = resolve_sites(_config(tmp_path, input_csv=seed))
    later, _ = resolve_sites(_config(tmp_path))
    assert [s["site_id"] for s in first] == [s["site_id"] for s in again] == [s["site_id"] for s in later]


def test_rerunning_after_the_supplied_file_is_deleted_reads_the_snapshot(tmp_path):
    seed = tmp_path / "scratch.csv"
    write_csv_records(seed, [{"site_id": "9"}], ["site_id"])
    resolve_sites(_config(tmp_path, input_csv=seed))
    seed.unlink()

    sites, _ = resolve_sites(_config(tmp_path, input_csv=seed))
    assert [s["site_id"] for s in sites] == ["9"]


def test_describe_seed_names_the_file_and_where_it_is_pinned(tmp_path):
    seed = tmp_path / "given.csv"
    write_csv_records(seed, [{"site_id": "1"}], ["site_id"])
    config = _config(tmp_path, input_csv=seed)
    resolve_sites(config)

    assert describe_seed(config) == f"{seed}, pinned in {run_seed_path(config)}"


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


# --- the snapshot is published atomically (Copilot review) ----------------------------------
class _ExplodesOnWrite:
    """A cell value that fails while the CSV is being written, after earlier rows are flushed."""

    def __init__(self, exc: BaseException) -> None:
        self.exc = exc

    def __str__(self) -> str:
        raise self.exc


def _rows_that_fail_partway(exc: BaseException) -> list[dict]:
    good = [{"site_id": str(i), "site_name": f"Site {i}"} for i in range(50)]
    return good + [{"site_id": "bad", "site_name": _ExplodesOnWrite(exc)}]


def test_an_interrupted_first_read_leaves_no_pinned_snapshot(tmp_path, monkeypatch):
    """A truncated seed.csv would be trusted by every later phase and never re-queried."""
    import waste_ai_search.seed_source as seed_source

    monkeypatch.setattr(
        seed_source, "load_seed_sites", lambda **kw: _rows_that_fail_partway(OSError("disk full"))
    )
    config = _config(tmp_path)

    with pytest.raises(OSError, match="disk full"):
        resolve_sites(config)

    assert not run_seed_path(config).exists(), "a partial snapshot was published"
    assert not list(config.run_dir.glob(".seed.csv*.tmp")), "the temporary file was left behind"


def test_the_next_attempt_after_an_interruption_queries_again(tmp_path, monkeypatch):
    """Because nothing was published, the run is not pinned to the broken read."""
    import waste_ai_search.seed_source as seed_source

    config = _config(tmp_path)
    monkeypatch.setattr(
        seed_source, "load_seed_sites", lambda **kw: _rows_that_fail_partway(OSError("disk full"))
    )
    with pytest.raises(OSError):
        resolve_sites(config)

    monkeypatch.setattr(seed_source, "load_seed_sites", lambda **kw: [{"site_id": "1", "site_name": "A"}])
    sites, _ = resolve_sites(config)

    assert [s["site_id"] for s in sites] == ["1"]
    assert run_seed_path(config).exists()


def test_ctrl_c_during_the_write_also_cleans_up(tmp_path, monkeypatch):
    """KeyboardInterrupt is a BaseException, not an Exception, and must not strand a partial file."""
    import waste_ai_search.seed_source as seed_source

    monkeypatch.setattr(
        seed_source, "load_seed_sites", lambda **kw: _rows_that_fail_partway(KeyboardInterrupt())
    )
    config = _config(tmp_path)

    with pytest.raises(KeyboardInterrupt):
        resolve_sites(config)

    assert not run_seed_path(config).exists()
    assert not list(config.run_dir.glob(".seed.csv*.tmp"))


def test_a_failed_write_never_damages_an_existing_file(tmp_path):
    from waste_ai_search.input_loader import write_snapshot_atomically

    path = tmp_path / "seed.csv"
    write_csv_records(path, [{"site_id": "keep"}], ["site_id"])
    before = path.read_text()

    with pytest.raises(OSError):
        write_snapshot_atomically(path, _rows_that_fail_partway(OSError("boom")), ["site_id", "site_name"])

    assert path.read_text() == before


def test_a_temporary_file_stranded_by_a_hard_crash_is_not_mistaken_for_a_snapshot(tmp_path, monkeypatch):
    """A kill -9 can skip cleanup. Only the final name pins a run, so the stray is ignored."""
    import waste_ai_search.seed_source as seed_source

    config = _config(tmp_path)
    config.run_dir.mkdir(parents=True)
    (config.run_dir / ".seed.csv.tmp").write_text("site_id\ntruncated")

    monkeypatch.setattr(seed_source, "load_seed_sites", lambda **kw: [{"site_id": "1", "site_name": "A"}])
    sites, _ = resolve_sites(config)

    assert [s["site_id"] for s in sites] == ["1"]
    assert "truncated" not in run_seed_path(config).read_text()
