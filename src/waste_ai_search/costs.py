"""What a run costs, estimated from list prices: its Bing queries and its model tokens.

Every search records, in the run log, the Bing queries it made (web_searches) and the model's
tokens: input, the part of it read from the prompt cache, and output, which includes the model's
reasoning. A retried attempt and the gas follow-up pass are billed too, so they count. This turns
those counts into dollars at list prices, for comparing models and budgeting batches. Azure Cost
Management has the billed amount; prices change, so check the table against it.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any


# USD per 1,000 queries: Grounding with Bing's published rate. Batch 1 billed about $14.10 (39,031
# queries for about $550), so it holds for the web search tool too.
WEB_SEARCH_PER_1000 = 14.0

# USD per million tokens: input, cached input, output (reasoning is billed as output). OpenAI's list
# prices as of October 2026, which Azure's GlobalStandard deployments match.
MODEL_PRICES = {
    "gpt-5.4-mini": (0.75, 0.075, 4.50),
    "gpt-5.4": (2.50, 0.25, 15.00),
}

TOKEN_COLUMNS = ["input_tokens", "cached_input_tokens", "output_tokens", "reasoning_tokens"]
# Where a run's searches record what they used, in the run folder.
RUN_LOG_FILE = "foundry_run_log.csv"


@dataclass
class Usage:
    """A search's model tokens, added up over its calls."""

    input_tokens: int = 0
    cached_input_tokens: int = 0
    output_tokens: int = 0
    reasoning_tokens: int = 0

    def add(self, response: Any) -> None:
        """Add a response's usage: the whole of it, the web search tool's loop included."""
        usage = getattr(response, "usage", None)
        if usage is None:
            return
        self.input_tokens += _int(getattr(usage, "input_tokens", 0))
        self.output_tokens += _int(getattr(usage, "output_tokens", 0))
        self.cached_input_tokens += _int(getattr(getattr(usage, "input_tokens_details", None), "cached_tokens", 0))
        self.reasoning_tokens += _int(getattr(getattr(usage, "output_tokens_details", None), "reasoning_tokens", 0))

    def as_row(self) -> dict[str, int]:
        return {column: getattr(self, column) for column in TOKEN_COLUMNS}


def _int(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def prices_for(model: str) -> tuple[float, float, float] | None:
    """A model's prices, by its deployment name or the dated name a response gives
    ("gpt-5.4-mini-2026-03-17"): the longest name in the table it starts with."""
    model = (model or "").lower()
    matches = [name for name in MODEL_PRICES if model == name or model.startswith(f"{name}-")]
    return MODEL_PRICES[max(matches, key=len)] if matches else None


def model_cost(model: str, input_tokens: int, cached_input_tokens: int, output_tokens: int) -> float | None:
    """Dollars for a model's tokens, or None for a model not in the table."""
    prices = prices_for(model)
    if prices is None:
        return None
    input_price, cached_price, output_price = prices
    uncached = max(0, input_tokens - cached_input_tokens)
    return (uncached * input_price + cached_input_tokens * cached_price + output_tokens * output_price) / 1_000_000


@dataclass
class RunCost:
    """A run's queries, tokens and estimated cost, from its run log."""

    queries: int = 0
    sites: int = 0
    models: tuple[str, ...] = ()
    input_tokens: int = 0
    cached_input_tokens: int = 0
    output_tokens: int = 0
    model_dollars: float | None = 0.0

    @property
    def search_dollars(self) -> float:
        return self.queries * WEB_SEARCH_PER_1000 / 1000

    @property
    def counted_tokens(self) -> bool:
        return bool(self.input_tokens or self.output_tokens)


def run_cost(rows: list[dict[str, Any]]) -> RunCost:
    """Add up a run log's rows. A row with no count (a search before tokens were recorded, or a
    fake client) adds nothing, rather than a false zero."""
    cost = RunCost()
    models: list[str] = []
    for row in rows:
        queries = str(row.get("web_searches") or "")
        if queries.isdigit():
            cost.queries += int(queries)
            cost.sites += 1
        tokens = {column: _int(row.get(column)) for column in TOKEN_COLUMNS}
        if not (tokens["input_tokens"] or tokens["output_tokens"]):
            continue
        model = str(row.get("model") or "")
        if model and model not in models:
            models.append(model)
        cost.input_tokens += tokens["input_tokens"]
        cost.cached_input_tokens += tokens["cached_input_tokens"]
        cost.output_tokens += tokens["output_tokens"]
        dollars = model_cost(model, tokens["input_tokens"], tokens["cached_input_tokens"], tokens["output_tokens"])
        cost.model_dollars = None if dollars is None or cost.model_dollars is None else cost.model_dollars + dollars
    cost.models = tuple(models)
    return cost


def describe_run_cost(cost: RunCost) -> list[str]:
    """The lines a search prints, and Run_Config records."""
    lines = [f"Bing queries: {cost.queries:,} across {cost.sites} searched site(s), ~${cost.search_dollars:,.2f} at ${WEB_SEARCH_PER_1000:g} per 1,000."]
    if not cost.counted_tokens:
        lines.append("Model tokens: not recorded for this run's searches.")
        return lines
    on = f" on {', '.join(cost.models)}" if cost.models else ""
    tokens = (
        f"Model tokens{on}: {cost.input_tokens:,} in ({cost.cached_input_tokens:,} cached), "
        f"{cost.output_tokens:,} out"
    )
    if cost.model_dollars is None:
        lines.append(f"{tokens}; no price for {', '.join(cost.models) or 'this model'} in costs.MODEL_PRICES.")
        return lines
    lines.append(f"{tokens}, ~${cost.model_dollars:,.2f}.")
    total = cost.search_dollars + cost.model_dollars
    per_site = f", ~${total / cost.sites:,.2f} a site" if cost.sites else ""
    lines.append(f"Estimated cost at list prices: ~${total:,.2f}{per_site}.")
    return lines
