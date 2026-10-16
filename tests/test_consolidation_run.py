"""A seed reads exactly one consolidation run, picked the way the Climate TRACE pipeline picks it.

consolidation.consolidated_facility keeps every run side by side, as do the value resolution ledger
and the crosswalk. Read whole, a seed would blend them.
"""
from __future__ import annotations

from datetime import datetime

import pytest

import waste_ai_search.db as db
from waste_ai_search.consolidation_run import RUNS_SQL, choose_run


def _row(run_id, strategy="legacy_replica_v1", linkage="legacy_replica_v1", created=None, rows=44260):
    return {
        "consolidation_run_id": run_id,
        "consolidation_strategy": strategy,
        "entity_linkage_strategy": linkage,
        "entity_linkage_run_id": f"link-{run_id}",
        "created": created,
        "row_count": rows,
    }


# RUNS_SQL returns runs newest first.
RUNS = [
    _row("experiment", linkage="exact_name_v1", created=datetime(2026, 10, 14, 9)),
    _row("newest", created=datetime(2026, 10, 13, 16)),
    _row("older", created=datetime(2026, 10, 2, 8)),
]


def test_the_newest_run_of_the_agreed_strategy_wins_over_a_newer_experiment():
    run = choose_run(RUNS, environ={})
    assert run.run_id == "newest"
    assert run.entity_linkage_run_id == "link-newest"
    assert run.created == "2026-10-13T16:00:00"
    assert run.rows == 44260
    assert "newest of 2 legacy_replica_v1/legacy_replica_v1 run(s), out of 3" in run.chosen_by


def test_a_pinned_run_is_read_whatever_its_strategy():
    run = choose_run(RUNS, environ={"CONSOLIDATION_RUN_ID": "experiment"})
    assert run.run_id == "experiment"
    assert run.chosen_by == "pinned by CONSOLIDATION_RUN_ID"


def test_a_pinned_run_that_is_not_there_stops_the_run():
    with pytest.raises(RuntimeError, match="'gone' is not in .*experiment, newest, older"):
        choose_run(RUNS, environ={"CONSOLIDATION_RUN_ID": "gone"})


def test_the_strategies_can_be_set():
    run = choose_run(RUNS, environ={"ENTITY_LINKAGE_STRATEGY": "exact_name_v1"})
    assert run.run_id == "experiment"


def test_no_run_of_the_strategy_stops_the_run_and_says_what_is_there():
    with pytest.raises(RuntimeError, match="legacy_replica_v1/exact_name_v1, legacy_replica_v1/legacy_replica_v1"):
        choose_run(RUNS, environ={"CONSOLIDATION_STRATEGY": "precise_v1"})


def test_an_empty_table_stops_the_run():
    with pytest.raises(RuntimeError, match="empty"):
        choose_run([], environ={})


def test_runs_are_listed_newest_first_and_undated_runs_last():
    # The run id is a uuid4 and says nothing about time, so it only breaks ties.
    assert "ORDER BY created DESC NULLS LAST, consolidation_run_id DESC" in RUNS_SQL


def test_every_seed_query_reads_the_one_run(monkeypatch):
    """The seed, its per-attribute provenance, the original names and SINIR's municipalities all
    come from tables holding every run; each must be filtered to the same one."""
    from waste_ai_search.seed_source import load_seed_sites

    queries = []

    def fake_fetch_all(sql, params=None, config=None):
        queries.append((sql, params))
        return []

    monkeypatch.setattr(db, "fetch_all", fake_fetch_all)
    load_seed_sites(iso3=["MEX"], consolidation_run_id="run-7")

    assert len(queries) >= 4
    for sql, params in queries:
        assert "consolidation_run_id = %s" in sql, sql
        assert "run-7" in params
    seed_sql, seed_params = queries[0]
    assert seed_params == ("run-7", ["MEX"])


def test_a_seed_with_no_run_given_picks_one(monkeypatch):
    import waste_ai_search.consolidation_run as consolidation_run
    from waste_ai_search.seed_source import load_seed_sites

    picked = choose_run(RUNS, environ={})
    monkeypatch.setattr(consolidation_run, "resolve_run", lambda config=None: picked)
    queries = []
    monkeypatch.setattr(db, "fetch_all", lambda sql, params=None, config=None: queries.append(params) or [])

    load_seed_sites()  # e.g. scripts/seed_metadata_search.py, which pins no run itself
    assert all("newest" in params for params in queries)
