"""Test-wide guards."""
from __future__ import annotations

import pytest


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
