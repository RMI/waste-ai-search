"""What a run costs: each site's Bing queries and model tokens are recorded, every attempt and pass
added up, and priced at list prices (costs.py)."""
from __future__ import annotations

import csv
from types import SimpleNamespace

import pytest
from openpyxl import load_workbook

from waste_ai_search.costs import (
    RunCost,
    Usage,
    describe_run_cost,
    model_cost,
    prices_for,
    run_cost,
)


def response(input_tokens=0, cached=0, output_tokens=0, reasoning=0, model="gpt-5.4-2026-03-05"):
    """A Responses API response's usage, in the shape azure-ai-projects returns it."""
    return SimpleNamespace(
        model=model,
        output=[],
        output_text='{"sources": []}',
        usage=SimpleNamespace(
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            input_tokens_details=SimpleNamespace(cached_tokens=cached),
            output_tokens_details=SimpleNamespace(reasoning_tokens=reasoning),
        ),
    )


# --- counting and pricing ------------------------------------------------------------------------
def test_usage_adds_up_responses_and_ignores_one_without_usage():
    usage = Usage()
    usage.add(response(1000, 200, 300, 100))
    usage.add(response(500, 0, 50, 0))
    usage.add(SimpleNamespace(output=[]))
    assert usage.as_row() == {"input_tokens": 1500, "cached_input_tokens": 200, "output_tokens": 350, "reasoning_tokens": 100}


def test_a_models_price_is_found_by_its_deployment_or_dated_name():
    assert prices_for("gpt-5.4-mini") == (0.75, 0.075, 4.50)
    assert prices_for("gpt-5.4-mini-2026-03-17") == (0.75, 0.075, 4.50)
    assert prices_for("gpt-5.4-2026-03-05") == (2.50, 0.25, 15.00)
    assert prices_for("gpt-5.5") is None


def test_cached_input_is_priced_as_cached_and_output_includes_reasoning():
    # 1M input of which 400K cached, 100K output, on gpt-5.4: 600K x $2.50 + 400K x $0.25 + 100K x $15.
    assert model_cost("gpt-5.4", 1_000_000, 400_000, 100_000) == pytest.approx(1.5 + 0.1 + 1.5)
    assert model_cost("unknown-model", 1000, 0, 1000) is None


def test_a_run_is_priced_from_its_log_and_a_row_with_no_count_adds_nothing():
    rows = [
        {"web_searches": "20", "model": "gpt-5.4-2026-03-05", "input_tokens": "200000", "cached_input_tokens": "0", "output_tokens": "11000"},
        {"web_searches": "10", "model": "gpt-5.4-2026-03-05", "input_tokens": "100000", "cached_input_tokens": "0", "output_tokens": "5000"},
        # A search from before tokens were recorded, and one skipped with a cached response.
        {"web_searches": "15", "model": "", "input_tokens": "", "output_tokens": ""},
        {"web_searches": "", "model": "", "input_tokens": "", "output_tokens": ""},
    ]
    cost = run_cost(rows)
    assert (cost.queries, cost.sites, cost.models, cost.input_tokens, cost.output_tokens) == (45, 3, ("gpt-5.4-2026-03-05",), 300_000, 16_000)
    assert cost.search_dollars == pytest.approx(0.63)
    assert cost.model_dollars == pytest.approx(0.75 + 0.24)
    lines = describe_run_cost(cost)
    assert lines[0] == "Bing queries: 45 across 3 searched site(s), ~$0.63 at $14 per 1,000."
    assert lines[1] == "Model tokens on gpt-5.4-2026-03-05: 300,000 in (0 cached), 16,000 out, ~$0.99."
    assert lines[2] == "Estimated cost at list prices: ~$1.62, ~$0.54 a site."


def test_a_run_whose_model_has_no_price_or_no_tokens_says_so():
    unpriced = run_cost([{"web_searches": "5", "model": "gpt-9", "input_tokens": "10", "output_tokens": "10"}])
    assert "no price for gpt-9" in describe_run_cost(unpriced)[1]
    assert describe_run_cost(RunCost(queries=5, sites=1))[1] == "Model tokens: not recorded for this run's searches."


# --- recording ----------------------------------------------------------------------------------
class _UsageClient:
    """Like the real client: after each call, `web_searches`, `usage` and `model` hold its use."""

    model = "gpt-5.4-2026-03-05"

    def __init__(self):
        self.web_searches, self.usage = 0, Usage()

    def search_site(self, site, prompt):  # noqa: ARG002
        self.web_searches, self.usage = 6, Usage()
        self.usage.add(response(50_000, 10_000, 2_000, 500))
        return {"sources": [], "search_notes": "Searched thoroughly and found no source-backed data."}


def _log(run_dir):
    return list(csv.DictReader((run_dir / "foundry_run_log.csv").open(encoding="utf-8-sig")))


def test_a_search_records_its_model_and_tokens_and_a_second_pass_adds_to_them(tmp_path, monkeypatch, capsys):
    from waste_ai_search import search as pl

    seed = tmp_path / "seed.csv"
    seed.write_text("site_id,site_name,country_iso3\n1,Test Landfill,NGA\n", encoding="utf-8")
    monkeypatch.setattr(pl, "get_client", lambda config: _UsageClient())
    run_dir = tmp_path / "run"
    pl.run_search(pl.PipelineConfig(input_csv=seed, run_dir=run_dir, run_id="t", site_delay_seconds=0))
    [row] = _log(run_dir)
    assert (row["model"], row["input_tokens"], row["cached_input_tokens"], row["output_tokens"], row["reasoning_tokens"]) == (
        "gpt-5.4-2026-03-05", "50000", "10000", "2000", "500",
    )
    printed = capsys.readouterr().out
    assert "Model tokens on gpt-5.4-2026-03-05: 50,000 in (10,000 cached), 2,000 out" in printed
    assert "Estimated cost at list prices: ~$" in printed

    # A second pass at the same site, the gas follow-up say, is billed too: its counts add up.
    pl.run_search(pl.PipelineConfig(input_csv=seed, run_dir=run_dir, run_id="t", site_delay_seconds=0, force=True))
    [row] = _log(run_dir)
    assert (row["web_searches"], row["input_tokens"], row["output_tokens"]) == ("12", "100000", "4000")


def test_the_real_client_records_a_responses_tokens_and_model(monkeypatch):
    from waste_ai_search.foundry_client import AzureFoundryAgentClient, FoundryClientConfig

    class FakeOpenAI:
        responses = SimpleNamespace(create=lambda **kwargs: response(7000, 1000, 800, 200, "gpt-5.4-mini-2026-03-17"))
        conversations = SimpleNamespace(create=lambda: SimpleNamespace(id="c"))

        def with_options(self, **kwargs):  # noqa: ARG002
            return self

    client = AzureFoundryAgentClient.__new__(AzureFoundryAgentClient)
    client.config = FoundryClientConfig(project_endpoint="x", agent_name="agent")
    client.project_client = SimpleNamespace(agents=SimpleNamespace(), get_openai_client=lambda agent_name: FakeOpenAI())  # noqa: ARG005
    client.search_site({}, "prompt")
    assert client.usage.as_row() == {"input_tokens": 7000, "cached_input_tokens": 1000, "output_tokens": 800, "reasoning_tokens": 200}
    assert client.model == "gpt-5.4-mini-2026-03-17"


def test_the_workbook_records_the_runs_cost(tmp_path):
    import json

    from waste_ai_search.arbitrate import run_arbitration
    from waste_ai_search.input_loader import write_csv_records
    from waste_ai_search.run_context import PipelineConfig
    from waste_ai_search.schema import FOUNDRY_RUN_LOG_HEADERS
    from waste_ai_search.seed_source import seed_headers

    run_dir = tmp_path / "run"
    write_csv_records(run_dir / "seed.csv", [{
        "facility_id": "1", "site_id": "1", "internal_facility_id": "1", "year": "2022", "site_name": "Test Landfill",
        "facility_name": "Test Landfill", "country_iso3": "NZL", "iso3c_plus": "NZL", "latitude": "-41.0",
        "longitude": "175.0", "contributing_data_sources": "osm_2022", "reference_year": "2022",
    }], seed_headers())
    (run_dir / "raw_foundry_responses").mkdir()
    (run_dir / "raw_foundry_responses" / "site_1.json").write_text(json.dumps({"site_id": "1", "sources": []}))
    write_csv_records(run_dir / "foundry_run_log.csv", [{
        "site_id": "1", "web_searches": "10", "model": "gpt-5.4-mini", "input_tokens": "100000",
        "cached_input_tokens": "0", "output_tokens": "10000",
    }], FOUNDRY_RUN_LOG_HEADERS)
    run_arbitration(PipelineConfig(input_csv=None, run_dir=run_dir, run_id="run", dataset_version="v"))
    config = {row[0]: row[1] for row in load_workbook(run_dir / "run_review.xlsx")["Run_Config"].values}
    assert config["search_cost"].startswith("Bing queries: 10 across 1 searched site(s), ~$0.14")
    assert "Estimated cost at list prices: ~$0.26" in config["search_cost"]
