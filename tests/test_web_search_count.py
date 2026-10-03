"""Bing queries are counted per site, retries included, because each one is billed (WP-545)."""
from __future__ import annotations

import csv
from types import SimpleNamespace

from waste_ai_search.foundry_client import count_web_searches


def test_counts_each_query_a_search_sent_and_ignores_page_reads():
    # Shape taken from a live Foundry response (azure-ai-projects 2.3.0).
    response = SimpleNamespace(output=[
        SimpleNamespace(type="reasoning"),
        SimpleNamespace(type="web_search_call", action={"type": "search", "queries": ["a", "b"]}),
        SimpleNamespace(type="web_search_call", action=SimpleNamespace(type="search", queries=["c"])),
        SimpleNamespace(type="web_search_call", action={"type": "open_page", "url": "https://x"}),
        SimpleNamespace(type="message"),
    ])
    assert count_web_searches(response) == 3
    assert count_web_searches(SimpleNamespace(output=None)) == 0


class _CountingClient:
    """Like the real client: `web_searches` holds the last call's queries. Fails its tool once."""

    def __init__(self):
        self.calls = 0
        self.web_searches = 0

    def search_site(self, site, prompt):  # noqa: ARG002
        self.calls += 1
        self.web_searches = 4
        if self.calls == 1:
            return {"attributes": [], "search_notes": "The search_query tool did not return usable results."}
        return {"attributes": [], "search_notes": "Searched thoroughly and found no source-backed data."}


def test_a_retried_site_logs_the_queries_of_every_attempt(tmp_path, monkeypatch, capsys):
    from waste_ai_search import search as pl

    seed = tmp_path / "seed.csv"
    seed.write_text("site_id,site_name,country_iso3\n1,Test Landfill,NGA\n", encoding="utf-8")
    monkeypatch.setattr(pl, "get_client", lambda config: _CountingClient())
    monkeypatch.setattr(pl, "SEARCH_TOOL_RETRY_DELAY_SECONDS", 0.0)

    pl.run_search(pl.PipelineConfig(input_csv=seed, run_dir=tmp_path / "run", run_id="t", site_delay_seconds=0))

    rows = list(csv.DictReader((tmp_path / "run" / "foundry_run_log.csv").open(encoding="utf-8-sig")))
    assert rows[0]["web_searches"] == "8"
    assert "Bing queries: 8 across 1 searched site(s)." in capsys.readouterr().out


def test_retries_default_to_two():
    from waste_ai_search.foundry_client import FoundryClientConfig
    from waste_ai_search.run_context import PipelineConfig

    assert FoundryClientConfig().max_retries == 2
    assert PipelineConfig(input_csv=None, run_dir=None, run_id="t").search_tool_retries == 2
