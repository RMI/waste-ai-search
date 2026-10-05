"""Shared run scaffolding: configuration, output paths, cached-response IO, and the agent client.

Both phases depend on this; it depends on neither of them.
"""
from __future__ import annotations

import json
import re
import signal
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from .foundry_client import AzureFoundryAgentClient, FoundryClient, FoundryClientConfig
from .schema import normalize_scalar


# The agent's search tool fails transiently and returns a perfectly valid JSON response saying so,
# which is why foundry_client's exception-based retry never sees it. Measured on a 10-site pilot:
# 6 sites failed this way on the first pass and all 6 recovered on retry, one needing two attempts.
# So the search phase retries on the reported failure itself.
SEARCH_TOOL_RETRY_ATTEMPTS = 2
# Bing grounding bills per query (WP-545). Asking in the prompt alone was ignored (233 queries vs
# 235 on the same 10 sites), so this is also sent as the Responses API's max_tool_calls, which the
# service enforces. It caps searches, not queries: each search sent ~4 queries in testing, and a
# cap of 6 cut one site from 30-39 queries to 20. The cap is per response, so each retry gets a
# fresh one. 0 means no cap.
DEFAULT_MAX_WEB_SEARCHES = 6
SEARCH_TOOL_RETRY_DELAY_SECONDS = 20.0


@dataclass
class PipelineConfig:
    # None means seed straight from consolidation.consolidated_facility. A path is used for a
    # pinned or offline run, and by the gas-collection follow-up, which writes a modified seed
    # into the run directory and points pass 2 at it.
    input_csv: Path | None
    run_dir: Path
    run_id: str
    dataset_version: str = "waste_ai_search_v0.2"
    limit: int = 0
    iso3: list[str] = field(default_factory=list)
    site_ids: list[str] = field(default_factory=list)
    force: bool = False
    site_delay_seconds: float = 3.0
    hard_site_timeout_seconds: int = 240
    search_tool_retries: int = SEARCH_TOOL_RETRY_ATTEMPTS
    max_web_searches: int = DEFAULT_MAX_WEB_SEARCHES
    geocode_cache: Path | None = None
    use_geocode_cache: bool = False
    pilot_size: int = 0
    include_excluded_countries: bool = False
    # WP-533. Fetch cited URLs at the end of the search phase (network); arbitration only reads
    # the verdicts. keep_broken_links lets a dead source stay promotable instead of going to leads.
    check_links: bool = True
    keep_broken_links: bool = False


def default_run_id() -> str:
    return f"run_{datetime.now().strftime('%Y%m%d_%H%M%S')}"


def tool_root() -> Path:
    return Path(__file__).resolve().parents[2]


def default_run_dir(run_id: str) -> Path:
    return tool_root() / "outputs" / "runs" / run_id


def raw_dir(run_dir: Path) -> Path:
    return run_dir / "raw_foundry_responses"


# A follow-up pass searches a site a second time for a different set of attributes. Writing it to
# the same filename would overwrite the first pass's findings, so passes are kept side by side and
# arbitration merges them.
PASS_SEPARATOR = "__"


def raw_response_path(run_dir: Path, site_id: Any, pass_label: str = "") -> Path:
    name = f"site_{normalize_scalar(site_id)}"
    if pass_label:
        name = f"{name}{PASS_SEPARATOR}{pass_label}"
    return raw_dir(run_dir) / f"{name}.json"


def site_id_from_path(path: Path) -> str:
    """Recover the site id from a cached response name, ignoring any pass label."""
    return path.stem.replace("site_", "", 1).split(PASS_SEPARATOR, 1)[0]


def save_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")


def load_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def get_client(config: PipelineConfig) -> FoundryClient:
    client_config = FoundryClientConfig.from_env()
    client_config.max_tool_calls = config.max_web_searches
    return AzureFoundryAgentClient(client_config)


def agent_identity(client: Any) -> str:
    """Identify the agent that answered. The id is often unset, so fall back to the name."""
    config = getattr(client, "config", None)
    if config is None:
        return ""
    return normalize_scalar(getattr(config, "agent_id", "")) or normalize_scalar(
        getattr(config, "agent_name", "")
    )


class hard_timeout:  # noqa: N801
    def __init__(self, seconds: int):
        self.seconds = seconds

    def __enter__(self):
        if self.seconds and self.seconds > 0 and hasattr(signal, "SIGALRM"):
            self._previous = signal.signal(signal.SIGALRM, self._raise)
            signal.alarm(self.seconds)
        return self

    def __exit__(self, *exc):
        if self.seconds and self.seconds > 0 and hasattr(signal, "SIGALRM"):
            signal.alarm(0)
            signal.signal(signal.SIGALRM, self._previous)
        return False

    def _raise(self, signum, frame):  # noqa: ARG002
        raise TimeoutError(f"Site request exceeded {self.seconds}s.")


# The agent reports its own web-search tool failing inside search_notes, while the API call itself
# succeeds and returns valid JSON. Without this, "the tool was broken" is indistinguishable from
# "searched properly and this facility is undocumented" - and only the first is worth retrying.
SEARCH_TOOL_FAILURE_PATTERN = re.compile(
    r"(search_query tool|browsing tool|web[- ]search tool|search tool)[^.]{0,80}"
    r"(did not return|returned an invalid|invalid[- ]arguments|error|not return usable)"
    r"|could not complete the required web search"
    r"|did not return usable (?:indexed )?(?:results|search-result)",
    re.IGNORECASE,
)


def search_tool_failed(payload: dict[str, Any]) -> bool:
    """Whether the agent said its search tool broke, rather than that it found nothing."""
    notes = normalize_scalar(payload.get("search_notes"))
    return bool(notes) and bool(SEARCH_TOOL_FAILURE_PATTERN.search(notes))
