#!/usr/bin/env python3
"""Create the AI search's Foundry agent, or a new version of it.

The agent is a prompt on the model deployment infra/ creates, with Foundry's web search tool, which
runs on Bing without a Bing resource of its own. Everything a search needs is in each site's
prompt (prompt_builder.py), so the agent's own instructions only say to follow it. Run it once after
`terraform apply`, and again to change the agent: each run adds a version, and the search uses the
latest.

Signs in as you (`az login`), like the search. Reads AZURE_AI_PROJECT_ENDPOINT and
AZURE_AI_AGENT_NAME from .env unless given.

Usage:
  uv run python scripts/create_foundry_agent.py --dry-run     # show what it would create
  uv run python scripts/create_foundry_agent.py
  uv run python scripts/create_foundry_agent.py --endpoint https://...services.ai.azure.com/api/projects/waste-ai-search
"""
from __future__ import annotations

import argparse
import json
import os
import sys


DEFAULT_NAME = "waste-ai-search"
DEFAULT_MODEL = "gpt-5.4-mini"
INSTRUCTIONS = (
    "You research waste disposal facilities on the web for WasteMAP. Each message describes one "
    "facility and says exactly what to find, how to search and how to answer. Follow it, use the "
    "web search tool, and answer with strict JSON only."
)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    try:
        from dotenv import load_dotenv

        load_dotenv()
    except ImportError:
        pass
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--endpoint", default=os.environ.get("AZURE_AI_PROJECT_ENDPOINT", ""),
                        help="The Foundry project's endpoint (terraform output project_endpoint).")
    parser.add_argument("--name", default=os.environ.get("AZURE_AI_AGENT_NAME", "") or DEFAULT_NAME)
    parser.add_argument("--model", default=DEFAULT_MODEL, help="The model deployment's name.")
    parser.add_argument("--dry-run", action="store_true", help="Print the definition; create nothing.")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    from azure.ai.projects.models import PromptAgentDefinition, WebSearchTool

    args = parse_args(argv)
    definition = PromptAgentDefinition(model=args.model, instructions=INSTRUCTIONS, tools=[WebSearchTool()])
    if args.dry_run:
        print(json.dumps({"agent_name": args.name, "endpoint": args.endpoint, "definition": definition.as_dict()}, indent=2))
        return 0
    if not args.endpoint:
        raise SystemExit("Set AZURE_AI_PROJECT_ENDPOINT in .env, or pass --endpoint (terraform output project_endpoint).")

    from azure.ai.projects import AIProjectClient
    from azure.identity import DefaultAzureCredential

    client = AIProjectClient(endpoint=args.endpoint, credential=DefaultAzureCredential())
    version = client.agents.create_version(
        agent_name=args.name,
        definition=definition,
        description="WasteMAP AI search: finds missing facility data on the web (RMI/waste-ai-search).",
    )
    print(f"Agent {version.name}, version {version.version}, on {args.model}.")
    print(f"In .env:\n  AZURE_AI_PROJECT_ENDPOINT={args.endpoint}\n  AZURE_AI_AGENT_NAME={version.name}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
