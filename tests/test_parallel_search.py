"""Sites are searched in parallel, each thread with its own client and a time limit that works off
the main thread."""
from __future__ import annotations

import csv
import threading
import time
from types import SimpleNamespace

import pytest


def _seed(tmp_path, count):
    seed = tmp_path / "seed.csv"
    rows = "".join(f"{i},Landfill {i},NGA\n" for i in range(1, count + 1))
    seed.write_text("site_id,site_name,country_iso3\n" + rows, encoding="utf-8")
    return seed


def _log(tmp_path):
    return list(csv.DictReader((tmp_path / "run" / "foundry_run_log.csv").open(encoding="utf-8-sig")))


def test_sites_are_searched_at_the_same_time(tmp_path, monkeypatch):
    from waste_ai_search import search as pl

    # Four searches can only pass this barrier together, so a one-at-a-time loop would break it.
    barrier = threading.Barrier(4, timeout=5)

    class _Client:
        def search_site(self, site, prompt):  # noqa: ARG002
            barrier.wait()
            return {"attributes": [], "search_notes": "found nothing"}

    monkeypatch.setattr(pl, "get_client", lambda config: _Client())
    pl.run_search(pl.PipelineConfig(
        input_csv=_seed(tmp_path, 8), run_dir=tmp_path / "run", run_id="t", site_delay_seconds=0, workers=4,
    ))

    assert sorted(row["status"] for row in _log(tmp_path)) == ["Succeeded"] * 8
    assert len(list((tmp_path / "run" / "raw_foundry_responses").glob("site_*.json"))) == 8


def test_each_site_logs_its_own_bing_count(tmp_path, monkeypatch):
    """A client holds the count of the site it is searching, so threads must not share one."""
    from waste_ai_search import search as pl

    class _Client:
        def search_site(self, site, prompt):  # noqa: ARG002
            self.web_searches = int(site["site_id"])
            time.sleep(0.05)  # let another thread's site start before this one reads its count
            return {"attributes": [], "search_notes": "found nothing"}

    monkeypatch.setattr(pl, "get_client", lambda config: _Client())
    pl.run_search(pl.PipelineConfig(
        input_csv=_seed(tmp_path, 8), run_dir=tmp_path / "run", run_id="t", site_delay_seconds=0, workers=4,
    ))

    assert all(row["web_searches"] == row["site_id"] for row in _log(tmp_path))


def test_call_with_limit_returns_raises_or_gives_up():
    from waste_ai_search.foundry_client import call_with_limit

    assert call_with_limit(lambda: 7, 1) == 7
    assert call_with_limit(lambda: 7, None) == 7
    with pytest.raises(ValueError):
        call_with_limit(lambda: int("x"), 1)

    started = time.monotonic()
    with pytest.raises(TimeoutError):
        call_with_limit(lambda: time.sleep(5), 0.1)
    assert time.monotonic() - started < 1


def test_a_hung_call_is_abandoned_and_not_retried(monkeypatch):
    """WP-545 saw a timed-out site retried for another 15 minutes. Past the limit, it stops."""
    from waste_ai_search import run_context
    from waste_ai_search.foundry_client import AzureFoundryAgentClient, FoundryClientConfig, call_with_limit

    monkeypatch.setattr(run_context, "AzureFoundryAgentClient", lambda cfg: cfg)
    config = run_context.PipelineConfig(input_csv=None, run_dir=None, run_id="t", hard_site_timeout_seconds=1)
    assert run_context.get_client(config).site_timeout_seconds == 1

    calls = []
    client = AzureFoundryAgentClient.__new__(AzureFoundryAgentClient)
    client.config = FoundryClientConfig(max_retries=2, site_timeout_seconds=1)
    client._invoke_agent = lambda prompt, limit: calls.append(limit) or call_with_limit(lambda: time.sleep(5), limit)

    started = time.monotonic()
    with pytest.raises(TimeoutError):
        client.search_site(SimpleNamespace(), "prompt")
    assert len(calls) == 1
    assert time.monotonic() - started < 2


def test_a_response_is_written_whole_or_not_at_all(tmp_path, monkeypatch):
    """A hard stop mid-write must not leave a file that a resumed run reads as unreadable."""
    import os

    from waste_ai_search.run_context import save_json

    path = tmp_path / "site_1.json"
    monkeypatch.setattr(os, "replace", lambda src, dst: (_ for _ in ()).throw(KeyboardInterrupt()))
    with pytest.raises(KeyboardInterrupt):
        save_json(path, {"attributes": []})
    assert not path.exists()

    monkeypatch.undo()
    save_json(path, {"attributes": []})
    assert path.read_text(encoding="utf-8").startswith("{")
    assert [p.name for p in tmp_path.iterdir()] == ["site_1.json"]
