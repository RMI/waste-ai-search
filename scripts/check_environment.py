#!/usr/bin/env python3
from __future__ import annotations

import importlib.util
import os
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))


def load_dotenv_if_available() -> None:
    try:
        from dotenv import load_dotenv  # type: ignore

        load_dotenv(ROOT / ".env")
    except ImportError:
        pass


def check_import(module_name: str) -> tuple[str, bool, str]:
    try:
        found = importlib.util.find_spec(module_name) is not None
    except ModuleNotFoundError:
        found = False
    return module_name, found, "found" if found else "missing"


def masked(value: str) -> str:
    return "set" if value else "missing"


def main() -> int:
    load_dotenv_if_available()
    checks = [
        check_import("openpyxl"),
        check_import("dotenv"),
        check_import("azure.ai.projects"),
        check_import("azure.identity"),
        check_import("waste_ai_search"),
    ]
    print("Python:", sys.executable)
    print("Repository:", ROOT)
    print("\nPackages:")
    failed = False
    for module_name, ok, status in checks:
        print(f"  {status:>7}  {module_name}")
        failed = failed or not ok

    print("\nFoundry environment:")
    for key in [
        "AZURE_AI_PROJECT_ENDPOINT",
        "AZURE_AI_AGENT_NAME",
        "AZURE_AI_AGENT_name",
        "AZURE_AI_AGENT_ID",
        "AZURE_FOUNDRY_MAX_RETRIES",
        "AZURE_FOUNDRY_TIMEOUT_SECONDS",
    ]:
        print(f"  {key}: {masked(os.environ.get(key, ''))}")

    has_agent = bool(
        os.environ.get("AZURE_AI_AGENT_NAME")
        or os.environ.get("AZURE_AI_AGENT_name")
        or os.environ.get("AZURE_AI_AGENT_ID")
    )
    if not os.environ.get("AZURE_AI_PROJECT_ENDPOINT") or not has_agent:
        print("\nLive Foundry runs need AZURE_AI_PROJECT_ENDPOINT and AZURE_AI_AGENT_NAME or AZURE_AI_AGENT_ID.")
        failed = True

    has_sp = all(os.environ.get(key) for key in ["AZURE_TENANT_ID", "AZURE_CLIENT_ID", "AZURE_CLIENT_SECRET"])
    if has_sp:
        print("Authentication: service principal variables are set.")
    else:
        print("Authentication: service principal variables not fully set; local runs can use `az login`.")

    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
