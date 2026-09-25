from __future__ import annotations

import json
import os
import re
import time
from dataclasses import dataclass
from typing import Any, Protocol


class FoundryClient(Protocol):
    def search_site(self, site: dict[str, Any], prompt: str) -> dict[str, Any]:
        """Run a source-backed waste-site search and return a JSON-like payload."""


@dataclass
class FoundryClientConfig:
    project_endpoint: str = ""
    agent_id: str = ""
    agent_name: str = ""
    timeout_seconds: int = 120
    max_retries: int = 3

    @classmethod
    def from_env(cls) -> "FoundryClientConfig":
        try:
            from dotenv import load_dotenv  # type: ignore

            load_dotenv()
        except ImportError:
            pass
        for optional_key in ("AZURE_TENANT_ID", "AZURE_CLIENT_ID", "AZURE_CLIENT_SECRET"):
            if os.environ.get(optional_key) == "":
                os.environ.pop(optional_key, None)
        return cls(
            project_endpoint=os.environ.get("AZURE_AI_PROJECT_ENDPOINT", ""),
            agent_id=os.environ.get("AZURE_AI_AGENT_ID", ""),
            agent_name=os.environ.get("AZURE_AI_AGENT_NAME", "") or os.environ.get("AZURE_AI_AGENT_name", ""),
            timeout_seconds=int(os.environ.get("AZURE_FOUNDRY_TIMEOUT_SECONDS", "120")),
            max_retries=int(os.environ.get("AZURE_FOUNDRY_MAX_RETRIES", "3")),
        )


class AzureFoundryAgentClient:
    def __init__(self, config: FoundryClientConfig | None = None):
        self.config = config or FoundryClientConfig.from_env()
        if not self.config.project_endpoint:
            raise ValueError("AZURE_AI_PROJECT_ENDPOINT is required for live Foundry mode.")
        if not self.config.agent_id and not self.config.agent_name:
            raise ValueError("AZURE_AI_AGENT_ID or AZURE_AI_AGENT_NAME is required for live Foundry mode.")

        try:
            from azure.ai.projects import AIProjectClient  # type: ignore
            from azure.identity import DefaultAzureCredential  # type: ignore
        except ImportError as exc:
            raise ImportError(
                "Live Foundry mode requires azure-ai-projects and azure-identity. "
                "Run `uv sync` first."
            ) from exc

        credential = DefaultAzureCredential()
        self.project_client = AIProjectClient(
            endpoint=self.config.project_endpoint,
            credential=credential,
            allow_preview=True,
        )

    def search_site(self, site: dict[str, Any], prompt: str) -> dict[str, Any]:
        last_error: Exception | None = None
        for attempt in range(1, self.config.max_retries + 1):
            try:
                text = self._invoke_agent(prompt)
                return parse_json_response(text)
            except Exception as exc:  # noqa: BLE001 - preserve retries around SDK/network calls
                last_error = exc
                if attempt >= self.config.max_retries:
                    break
                time.sleep(min(2 * attempt, 10))
        assert last_error is not None
        raise last_error

    def _invoke_agent(self, prompt: str) -> str:
        agents = self.project_client.agents
        if not hasattr(agents, "threads"):
            return self._invoke_foundry_v2_agent(prompt)

        agent_id = self.config.agent_id or self._resolve_agent_id_from_name()
        thread = agents.threads.create()
        agents.messages.create(thread_id=thread.id, role="user", content=prompt)
        run = agents.runs.create_and_process(thread_id=thread.id, agent_id=agent_id)
        status = getattr(run, "status", "")
        if str(status).lower() in {"failed", "cancelled", "expired"}:
            raise RuntimeError(f"Foundry agent run failed with status={status}: {run}")

        messages = list(agents.messages.list(thread_id=thread.id))
        for message in messages:
            if str(getattr(message, "role", "")).lower() != "assistant":
                continue
            text = message_to_text(message)
            if text:
                return text
        raise RuntimeError("Foundry agent returned no assistant message text.")

    def _resolve_agent_name(self) -> str:
        if self.config.agent_name:
            return self.config.agent_name
        if self.config.agent_id and not looks_like_uuid(self.config.agent_id):
            return self.config.agent_id
        agents = list(self.project_client.agents.list())
        for agent in agents:
            if str(getattr(agent, "id", "")) == str(self.config.agent_id):
                return str(getattr(agent, "name", ""))
        if len(agents) == 1:
            return str(getattr(agents[0], "name", ""))
        raise RuntimeError("Foundry SDK 2.x requires an agent name. Set AZURE_AI_AGENT_NAME.")

    def _resolve_agent_id_from_name(self) -> str:
        if self.config.agent_id:
            return self.config.agent_id
        if not self.config.agent_name:
            raise RuntimeError("Set AZURE_AI_AGENT_ID or AZURE_AI_AGENT_NAME.")
        agents = list(self.project_client.agents.list())
        matches = [agent for agent in agents if str(getattr(agent, "name", "")) == str(self.config.agent_name)]
        if len(matches) == 1:
            return str(getattr(matches[0], "id", ""))
        if not matches:
            raise RuntimeError(f"No Foundry agent found with name {self.config.agent_name!r}.")
        raise RuntimeError(f"Multiple Foundry agents found with name {self.config.agent_name!r}; set AZURE_AI_AGENT_ID.")

    def _invoke_foundry_v2_agent(self, prompt: str) -> str:
        agent_name = self._resolve_agent_name()
        openai_client = self.project_client.get_openai_client(agent_name=agent_name).with_options(
            timeout=self.config.timeout_seconds,
            max_retries=0,
        )
        conversation = openai_client.conversations.create()
        response = openai_client.responses.create(
            conversation=conversation.id,
            input=prompt,
            timeout=self.config.timeout_seconds,
        )
        text = getattr(response, "output_text", "")
        if text:
            return text
        chunks = []
        for item in getattr(response, "output", []) or []:
            for content in getattr(item, "content", []) or []:
                content_text = getattr(content, "text", "")
                if content_text:
                    chunks.append(content_text)
        if chunks:
            return "\n".join(chunks).strip()
        raise RuntimeError("Foundry agent returned no response output text.")


def message_to_text(message: Any) -> str:
    content = getattr(message, "content", None)
    if isinstance(content, str):
        return content
    chunks = []
    if isinstance(content, list):
        for item in content:
            if isinstance(item, str):
                chunks.append(item)
                continue
            text = getattr(item, "text", None)
            if isinstance(text, str):
                chunks.append(text)
                continue
            value = getattr(text, "value", None)
            if isinstance(value, str):
                chunks.append(value)
                continue
            if isinstance(item, dict):
                if isinstance(item.get("text"), str):
                    chunks.append(item["text"])
                elif isinstance(item.get("text"), dict) and isinstance(item["text"].get("value"), str):
                    chunks.append(item["text"]["value"])
    return "\n".join(chunks).strip()


def parse_json_response(text: str) -> dict[str, Any]:
    stripped = text.strip()
    if stripped.startswith("```"):
        stripped = re.sub(r"^```(?:json)?\s*", "", stripped)
        stripped = re.sub(r"\s*```$", "", stripped)
    try:
        payload = json.loads(stripped)
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", stripped, re.DOTALL)
        if not match:
            raise
        payload = json.loads(match.group(0))
    if not isinstance(payload, dict):
        raise ValueError("Foundry response JSON must be an object.")
    return payload


def looks_like_uuid(value: str) -> bool:
    return bool(re.fullmatch(r"[0-9a-fA-F-]{36}", str(value).strip()))
