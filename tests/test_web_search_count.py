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
    # Changed on purpose: the line now prices the queries too.
    assert "Bing queries: 8 across 1 searched site(s), ~$0.11 at $14 per 1,000." in capsys.readouterr().out


def test_a_resumed_run_keeps_and_a_follow_up_pass_adds_to_the_count(tmp_path):
    from waste_ai_search.search import merge_run_log
    from waste_ai_search.schema import FOUNDRY_RUN_LOG_HEADERS
    from waste_ai_search.input_loader import write_csv_records

    log = tmp_path / "foundry_run_log.csv"
    write_csv_records(log, [{"site_id": "1", "web_searches": 8}, {"site_id": "2", "web_searches": 5}],
                      FOUNDRY_RUN_LOG_HEADERS)
    merged = merge_run_log(log, [
        {"site_id": "1", "status": "Skipped existing response", "web_searches": ""},
        {"site_id": "2", "status": "Succeeded", "web_searches": 3},
    ])
    assert [row["web_searches"] for row in merged] == [8, 8]


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


# --- what each search did: its queries and the pages it opened (#15) --------------------------
def _response(*actions):
    """A response whose web search tool did these actions, in the shapes a live one gives them."""
    return SimpleNamespace(output=[SimpleNamespace(type="reasoning")] + [
        SimpleNamespace(type="web_search_call", action=action) for action in actions
    ] + [SimpleNamespace(type="message")], output_text='{"sources": []}')


def test_each_search_query_and_page_opened_is_kept_in_order():
    from waste_ai_search.foundry_client import web_search_actions

    response = _response(
        {"type": "search", "queries": ["relleno sanitario Yautepec", "tiradero El Zarco"]},
        SimpleNamespace(type="open_page", url="https://diario.example/nota"),
        {"type": "find_in_page", "url": "https://diario.example/nota", "pattern": "toneladas"},
        SimpleNamespace(type="search", queries=None, query="basurero Yautepec"),
    )
    assert web_search_actions(response) == [
        {"type": "search", "queries": ["relleno sanitario Yautepec", "tiradero El Zarco"]},
        {"type": "open_page", "url": "https://diario.example/nota"},
        {"type": "find_in_page", "url": "https://diario.example/nota", "pattern": "toneladas"},
        {"type": "search", "queries": ["basurero Yautepec"]},
    ]
    assert count_web_searches(response) == 3


def _live_client(queued):
    """The real client, answered by these responses in turn instead of Foundry."""
    from waste_ai_search.foundry_client import AzureFoundryAgentClient, FoundryClientConfig

    class FakeOpenAI:
        responses = SimpleNamespace(create=lambda **kwargs: queued.pop(0))
        conversations = SimpleNamespace(create=lambda: SimpleNamespace(id="c"))

        def with_options(self, **kwargs):  # noqa: ARG002
            return self

    client = AzureFoundryAgentClient.__new__(AzureFoundryAgentClient)
    client.config = FoundryClientConfig(project_endpoint="x", agent_name="agent")
    client.project_client = SimpleNamespace(agents=SimpleNamespace(), get_openai_client=lambda agent_name: FakeOpenAI())  # noqa: ARG005
    return client


def test_the_client_keeps_each_sites_actions_and_only_its_own():
    """Each worker thread has its own client, which starts each site afresh, as it does its counts."""
    client = _live_client([
        _response({"type": "search", "queries": ["a"]}, {"type": "open_page", "url": "https://a.example"}),
        _response({"type": "search", "queries": ["b"]}),
    ])
    client.search_site({}, "prompt")
    assert client.search_actions == [
        {"type": "search", "queries": ["a"], "attempt": 1}, {"type": "open_page", "url": "https://a.example", "attempt": 1},
    ]
    client.search_site({}, "prompt")
    assert client.search_actions == [{"type": "search", "queries": ["b"], "attempt": 1}]


def test_each_action_says_which_attempt_made_it(monkeypatch):
    """An answer that can't be read is asked for again, with a fresh cap of tool calls. Both attempts'
    calls are kept, each with its attempt, so the answer doesn't seem to have made more than the cap."""
    import time

    from waste_ai_search import foundry_client

    unreadable = _response({"type": "search", "queries": ["a"]}, {"type": "open_page", "url": "https://a.example"})
    unreadable.output_text = "I could not finish the search."
    client = _live_client([unreadable, _response({"type": "search", "queries": ["b"]})])
    monkeypatch.setattr(foundry_client, "time", SimpleNamespace(sleep=lambda seconds: None, monotonic=time.monotonic))
    assert client.search_site({}, "prompt") == {"sources": []}
    assert client.search_actions == [
        {"type": "search", "queries": ["a"], "attempt": 1}, {"type": "open_page", "url": "https://a.example", "attempt": 1},
        {"type": "search", "queries": ["b"], "attempt": 2},
    ]


def test_a_saved_response_keeps_its_actions_and_the_log_counts_its_tool_calls(tmp_path, monkeypatch):
    import json

    from waste_ai_search import search as pl
    from waste_ai_search.arbitrate import run_arbitration

    failed = [{"type": "search", "queries": ["a"]}]
    answered = [{"type": "search", "queries": ["a", "b"]}, {"type": "open_page", "url": "https://a.example"}]

    class _Client:
        def __init__(self):
            self.calls = 0

        def search_site(self, site, prompt):  # noqa: ARG002
            self.calls += 1
            if self.calls == 1:  # the tool fails once, and the retry's answer is the one kept
                self.web_searches, self.search_actions = 1, list(failed)
                return {"sources": [], "search_notes": "The search_query tool did not return usable results."}
            self.web_searches, self.search_actions = 2, list(answered)
            return {"sources": [], "search_notes": "Searched thoroughly and found no source-backed data."}

    seed = tmp_path / "seed.csv"
    seed.write_text("site_id,site_name,country_iso3,latitude,longitude\n1,Test Landfill,NGA,6.5,3.3\n", encoding="utf-8")
    monkeypatch.setattr(pl, "get_client", lambda config: _Client())
    monkeypatch.setattr(pl, "SEARCH_TOOL_RETRY_DELAY_SECONDS", 0.0)
    config = pl.PipelineConfig(input_csv=seed, run_dir=tmp_path / "run", run_id="t", site_delay_seconds=0)
    pl.run_search(config)

    saved = json.loads((tmp_path / "run" / "raw_foundry_responses" / "site_1.json").read_text(encoding="utf-8"))
    assert saved["_search_actions"] == answered
    [row] = list(csv.DictReader((tmp_path / "run" / "foundry_run_log.csv").open(encoding="utf-8-sig")))
    assert (row["web_searches"], row["tool_calls"]) == ("3", "3")  # both attempts, like the queries
    # Nothing that reads a response minds it.
    run_arbitration(config)
    assert not list(csv.DictReader((tmp_path / "run" / "parse_warnings.csv").open(encoding="utf-8-sig")))
