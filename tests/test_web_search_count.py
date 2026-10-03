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


def test_the_prompt_carries_the_search_budget_unless_it_is_off():
    from waste_ai_search.prompt_builder import build_site_prompt

    site = {"site_id": "1", "site_name": "Olushosun", "country_iso3": "NGA"}
    assert "Use at most 12 web searches in total" in build_site_prompt(site, max_web_searches=12)
    assert "Search budget" not in build_site_prompt(site, max_web_searches=0)


def test_cli_defaults_match_the_capped_retries_and_the_budget():
    """The CLI once hard-coded 3 retries, silently overriding the config default."""
    from waste_ai_search.cli import build_parser, make_config

    for command in ("search", "run"):
        config = make_config(build_parser().parse_args([command, "--run-id", "t"]))
        assert (config.search_tool_retries, config.max_web_searches) == (2, 6)


def test_the_search_cap_is_sent_as_max_tool_calls(monkeypatch):
    """The prompt alone was ignored; the service-enforced cap is what limits searches."""
    from waste_ai_search import run_context
    from waste_ai_search.foundry_client import AzureFoundryAgentClient

    sent = {}

    class FakeResponses:
        def create(self, **kwargs):
            sent.update(kwargs)
            return SimpleNamespace(output=[], output_text='{"attributes": []}')

    class FakeOpenAI:
        responses = FakeResponses()
        conversations = SimpleNamespace(create=lambda: SimpleNamespace(id="c"))

        def with_options(self, **kwargs):  # noqa: ARG002
            return self

    created = []
    monkeypatch.setattr(run_context, "AzureFoundryAgentClient", lambda cfg: created.append(cfg) or cfg)
    config = run_context.PipelineConfig(input_csv=None, run_dir=None, run_id="t", max_web_searches=6)
    client_config = run_context.get_client(config)
    assert client_config.max_tool_calls == 6

    client = AzureFoundryAgentClient.__new__(AzureFoundryAgentClient)
    client.config = client_config
    client.project_client = SimpleNamespace(
        agents=SimpleNamespace(), get_openai_client=lambda agent_name: FakeOpenAI()  # noqa: ARG005
    )
    client._resolve_agent_name = lambda: "agent"
    client.search_site({}, "prompt")
    assert sent["max_tool_calls"] == 6

    client.config.max_tool_calls = 0
    sent.clear()
    client.search_site({}, "prompt")
    assert "max_tool_calls" not in sent
