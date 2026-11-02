"""A second pass with another model on a batch's largest sites, merged into the same run.

Batch 3 is searched with GPT-5.6 Terra, then GPT-5.6 Sol searches the sites taking 100,000 t a year or
more again (`search --min-tonnes 100000 --pass-label sol`). Each pass keeps its own responses
(site_<id>__sol.json) and its own run-log row, priced at its own model; arbitration merges the passes,
so a page both models cite is one pair to review.
"""
from __future__ import annotations

import csv
from dataclasses import replace

import pytest
from openpyxl import load_workbook

from waste_ai_search import cli, search
from waste_ai_search.arbitrate import run_arbitration
from waste_ai_search.costs import Usage, model_cost, run_cost
from waste_ai_search.foundry_client import FoundryClientConfig
from waste_ai_search.input_loader import write_csv_records
from waste_ai_search.run_context import PipelineConfig
from waste_ai_search.schema import FOUNDRY_RUN_LOG_HEADERS
from waste_ai_search.search import merge_run_log


def seed(tmp_path):
    path = tmp_path / "seed.csv"
    path.write_text(
        "site_id,internal_facility_id,site_name,country_iso3,latitude,longitude,contributing_data_sources,"
        "reference_year,annual_incoming_waste_metric_tonnes\n"
        "1,1,Relleno Sanitario Chico,MEX,19.10,-98.20,osm_2022,2022,20000\n"
        "2,2,Relleno Sanitario Grande,MEX,19.50,-99.10,osm_2022,2022,328500.0\n"
        "3,3,Tiradero Sin Dato,MEX,20.10,-99.50,osm_2022,2022,\n",
        encoding="utf-8",
    )
    return path


def news(url, name, operator):
    return {
        "url": url, "source_title": "Nota", "publisher": "Diario", "source_type": "News report",
        "publication_date": "2025-05-01", "facility_name_in_source": name, "facility_name_english": "",
        "stated_address": "", "stated_coordinates": "",
        "values": [{"attribute_name": "operator", "value": operator, "unit": "", "value_basis": "Direct",
                    "confidence_score": "High", "value_date": "2025", "evidence_summary": "Says so.",
                    "quoted_evidence_short": "El relleno lo opera el municipio."}],
    }


BOTH = news("https://diario.example/relleno-grande", "Relleno Sanitario Grande", "Municipio")
TERRA_ONLY = news("https://otro.example/nota", "Relleno Sanitario Grande", "Municipio")
SOL_ONLY = news("https://tercero.example/clausura", "Relleno Sanitario Grande", "Municipio")


class Client:
    """Like the real client: after each call, its Bing queries, tokens and the model that answered."""

    def __init__(self, model, sources, queries, tokens):
        self.model, self.sources, self.queries, self.tokens = model, sources, queries, tokens
        self.web_searches, self.usage = 0, Usage()

    def search_site(self, site, prompt):  # noqa: ARG002
        self.web_searches = self.queries
        self.usage = Usage(input_tokens=self.tokens, output_tokens=self.tokens // 10)
        return {"site_id": site["site_id"], "sources": list(self.sources), "unverified_leads": [], "search_notes": "Searched."}


def log(run_dir):
    return list(csv.DictReader((run_dir / "foundry_run_log.csv").open(encoding="utf-8-sig")))


def test_min_tonnes_keeps_the_sites_our_seed_says_take_that_much(tmp_path, capsys):
    from waste_ai_search.input_loader import load_sites

    sites, _headers = load_sites(seed(tmp_path))
    config = PipelineConfig(input_csv=None, run_dir=tmp_path / "run", run_id="t", min_tonnes=100000)
    # A site with no tonnage in the seed is not known to be large.
    assert [site["site_id"] for site in search.select_sites(sites, config)] == ["2"]
    assert "Taking 100,000 t a year or more: 1 site(s)." in capsys.readouterr().out
    assert len(search.select_sites(sites, replace(config, min_tonnes=0))) == 3


def test_a_second_pass_has_its_own_row_priced_at_its_own_model_and_arbitration_merges_it(tmp_path, monkeypatch):
    run_dir = tmp_path / "run"
    config = PipelineConfig(input_csv=seed(tmp_path), run_dir=run_dir, run_id="run", site_delay_seconds=0)
    monkeypatch.setattr(search, "get_client", lambda _config: Client("gpt-5.6-terra", [BOTH, TERRA_ONLY], 10, 30000))
    search.run_search(config)
    monkeypatch.setattr(search, "get_client", lambda _config: Client("gpt-5.6-sol", [BOTH, SOL_ONLY], 6, 40000))
    search.run_search(replace(config, min_tonnes=100000), pass_label="sol")

    rows = log(run_dir)
    assert [(row["site_id"], row["pass_label"], row["model"]) for row in rows] == [
        ("1", "", "gpt-5.6-terra"), ("2", "", "gpt-5.6-terra"), ("2", "sol", "gpt-5.6-sol"), ("3", "", "gpt-5.6-terra"),
    ]
    assert (run_dir / "raw_foundry_responses" / "site_2__sol.json").exists()
    assert rows[1]["raw_response_path"].endswith("site_2.json")

    # Each pass at its own model's price, not the last one's; the site searched twice counts once.
    cost = run_cost(rows)
    terra, sol = model_cost("gpt-5.6-terra", 30000, 0, 3000), model_cost("gpt-5.6-sol", 40000, 0, 4000)
    assert cost.model_dollars == pytest.approx(3 * terra + sol)
    assert (cost.sites, cost.queries) == (3, 36)

    # Arbitration reads both passes as one response: every source of each, and the page both cited
    # as one pair to review.
    run_arbitration(config)
    found = list(csv.DictReader((run_dir / "found_values.csv").open(encoding="utf-8-sig")))
    assert {row["source_url"] for row in found if row["site_id"] == "2"} == {BOTH["url"], TERRA_ONLY["url"], SOL_ONLY["url"]}
    sheet = load_workbook(run_dir / "run_review.xlsx")["Site_Review"]
    header = [cell.value for cell in sheet[1]]
    listed = [dict(zip(header, row)) for row in sheet.iter_rows(min_row=2, values_only=True)]
    pairs = [row["source_url"] for row in listed if str(row["site_id"]) == "2" and not row["check"]]
    assert sorted(pairs) == sorted([BOTH["url"], TERRA_ONLY["url"], SOL_ONLY["url"]])
    settings = {row[0]: row[1] for row in load_workbook(run_dir / "run_review.xlsx")["Run_Config"].values}
    assert settings["sites_arbitrated"] == 3
    assert settings["search_cost"].startswith("Bing queries: 36 across 3 searched site(s)")


def test_a_retried_pass_replaces_only_its_own_row(tmp_path):
    path = tmp_path / "foundry_run_log.csv"
    write_csv_records(path, [
        {"site_id": "2", "pass_label": "", "status": "Succeeded", "web_searches": 10, "model": "gpt-5.6-terra", "input_tokens": 30000},
        {"site_id": "2", "pass_label": "sol", "status": "Failed", "web_searches": 4, "model": "gpt-5.6-sol", "input_tokens": 9000},
    ], FOUNDRY_RUN_LOG_HEADERS)
    merged = merge_run_log(path, [
        {"site_id": "2", "pass_label": "sol", "status": "Succeeded", "web_searches": 6, "model": "gpt-5.6-sol", "input_tokens": 40000},
    ])
    first, second = merged
    assert (first["pass_label"], first["status"], first["web_searches"], first["input_tokens"]) == ("", "Succeeded", "10", "30000")
    # Every attempt is billed, so the retry's counts add to the pass's own.
    assert (second["pass_label"], second["status"], second["web_searches"], second["input_tokens"]) == ("sol", "Succeeded", 10, 49000)


def test_a_log_from_before_passes_had_rows_reads_as_the_first_pass(tmp_path):
    path = tmp_path / "foundry_run_log.csv"
    write_csv_records(path, [{"site_id": "2", "status": "Succeeded", "web_searches": "10"}], ["site_id", "status", "web_searches"])
    merged = merge_run_log(path, [
        {"site_id": "2", "pass_label": "", "status": "Skipped existing response", "web_searches": ""},
        {"site_id": "2", "pass_label": "sol", "status": "Succeeded", "web_searches": 6},
    ])
    assert [(row.get("pass_label", ""), row["web_searches"]) for row in merged] == [("", 10), ("sol", 6)]


def test_a_pass_still_failing_is_retried_as_that_pass_by_the_same_agent(tmp_path, monkeypatch, capsys):
    """Without its label, the retry's --force would replace the first pass's responses. Without its
    agent, a shell that doesn't name one would search with .env's, and add that model's tokens to
    this pass's row."""
    class Failing:
        config = FoundryClientConfig(agent_name="waste-ai-search-gpt-5-6-sol")

        def search_site(self, site, prompt):  # noqa: ARG002
            return {"sources": [], "search_notes": "The search_query tool did not return usable results."}

    monkeypatch.setattr(search, "get_client", lambda _config: Failing())
    monkeypatch.setattr(search, "SEARCH_TOOL_RETRY_DELAY_SECONDS", 0.0)
    config = PipelineConfig(input_csv=seed(tmp_path), run_dir=tmp_path / "run", run_id="batch3", site_delay_seconds=0,
                            site_ids=["2"])
    search.run_search(config, pass_label="sol")
    assert (
        "   AZURE_AI_AGENT_NAME=waste-ai-search-gpt-5-6-sol uv run waste-ai-search search --run-id batch3 "
        "--pass-label sol --force --site-ids 2\n"
    ) in capsys.readouterr().out
    assert search.agent_setting(object()) == ""  # a client that names no agent


def test_the_search_command_takes_a_pass_label_and_a_minimum_tonnage(tmp_path, monkeypatch):
    seen = []
    monkeypatch.setattr(cli, "run_search", lambda config, pass_label="": seen.append((config.min_tonnes, pass_label)))
    argv = ["search", "--run-id", "batch3", "--run-dir", str(tmp_path / "run"), "--local", "--batch", "3"]
    assert cli.main([*argv, "--min-tonnes", "100000", "--pass-label", "sol"]) == 0
    assert cli.main(argv) == 0
    assert seen == [(100000, "sol"), (0, "")]
    # The label names files, so it can't hold a path or the separator between site and pass.
    for label in ("../sol", "sol__2", "s o l"):
        with pytest.raises(SystemExit):
            cli.build_parser().parse_args(["search", "--pass-label", label])


def test_name_yield_counts_a_site_searched_in_two_passes_once(tmp_path):
    from waste_ai_search.name_yield import one_row_per_site

    rows = one_row_per_site([
        {"site_id": "2", "pass_label": "", "status": "Succeeded", "web_searches": "10"},
        {"site_id": "2", "pass_label": "sol", "status": "Succeeded", "web_searches": "6"},
        {"site_id": "3", "pass_label": "", "status": "Failed", "web_searches": ""},
    ])
    assert [(row["site_id"], row["web_searches"], row["status"]) for row in rows] == [("2", "16", "Succeeded"), ("3", "", "Failed")]
