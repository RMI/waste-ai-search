"""Test-wide guards."""
from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def no_real_link_checks(monkeypatch, request):
    """The suite must never reach the network.

    The search phase checks cited URLs by default (WP-533), so any test whose fake agent response
    contains a URL would otherwise make a real HTTP request - slow, flaky, and not offline. Every
    fetch is answered with a 200 instead. A test that exercises the checker itself passes its own
    fake `fetch`, which takes precedence.
    """
    import waste_ai_search.link_check as link_check

    # Kept so the guard's own tests can exercise the real fetch against a loopback-only server.
    link_check.__dict__.setdefault("_original_fetch", link_check._fetch)
    monkeypatch.setattr(link_check, "_fetch", lambda session, url, throttle: 200)
    monkeypatch.setattr(link_check, "PER_HOST_DELAY_SECONDS", 0.0)


@pytest.fixture(autouse=True)
def no_real_storage_or_publishing(monkeypatch):
    """No test may reach blob storage or write into the team's SharePoint folder (WP-534).

    Every CLI command loads `.env`, which on a developer machine names a real storage account and a
    real, OneDrive-synced SharePoint folder - so a test that runs the CLI would otherwise publish
    into it, where the team sees it. The variables are set to empty rather than deleted: `.env`
    loading only skips variables that already exist, so a deleted one would be read straight back.
    Tests of storage and publishing pass their own fake client and target directory.
    """
    for name in ("AZURE_STORAGE_ACCOUNT", "AZURE_STORAGE_CONNECTION_STRING", "WASTE_AI_SEARCH_REVIEW_DIR"):
        monkeypatch.setenv(name, "")
    monkeypatch.setenv("WASTE_AI_SEARCH_LOCAL", "1")
