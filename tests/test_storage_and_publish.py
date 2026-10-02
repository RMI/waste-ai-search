"""WP-534: run results in Azure Blob Storage, review workbooks in SharePoint.

Blob is exercised against an in-memory fake container, as RDP-52 did; SharePoint against temporary
folders. Nothing here reaches Azure or the real, OneDrive-synced folder (tests/conftest.py guards
the environment).
"""
from __future__ import annotations

import io
import json
from types import SimpleNamespace

import pytest

from waste_ai_search import storage
from waste_ai_search.publish import PUBLISH_LEDGER, publish_review


class FakeContainer:
    """Just enough of azure.storage.blob.ContainerClient."""

    def __init__(self, blobs=None):
        self.blobs = dict(blobs or {})
        self.uploads = []

    def get_container_properties(self):
        return {}

    def upload_blob(self, name, data, overwrite=False):
        self.uploads.append(name)
        self.blobs[name] = data.read()

    def list_blobs(self, name_starts_with=""):
        return [SimpleNamespace(name=n) for n in self.blobs if n.startswith(name_starts_with)]

    def download_blob(self, name):
        payload = self.blobs[name]
        return SimpleNamespace(readinto=lambda handle: handle.write(payload))


def run_folder(tmp_path):
    run = tmp_path / "run"
    (run / "raw_foundry_responses").mkdir(parents=True)
    (run / "raw_foundry_responses" / "site_1.json").write_text('{"attributes": []}')
    (run / "seed.csv").write_text("site_id\n1\n")
    (run / "r_review.xlsx").write_bytes(b"workbook")
    return run


# --- push -------------------------------------------------------------------------------------
def test_push_mirrors_the_run_folder(tmp_path):
    container = FakeContainer()
    count = storage.push(run_folder(tmp_path), "outputs/runs/r", client=container)
    assert count == 3
    assert set(container.blobs) == {
        "outputs/runs/r/raw_foundry_responses/site_1.json",
        "outputs/runs/r/seed.csv",
        "outputs/runs/r/r_review.xlsx",
    }


def test_push_never_uploads_excel_locks_dotfiles_or_temporaries(tmp_path):
    """An SME with the workbook open leaves a ~$ lock file beside it."""
    run = run_folder(tmp_path)
    for junk in ("~$r_review.xlsx", ".DS_Store", ".seed.csv.tmp"):
        (run / junk).write_text("x")
    container = FakeContainer()
    storage.push(run, "outputs/runs/r", client=container)
    assert not [n for n in container.blobs if "~$" in n or "/." in n or n.endswith(".tmp")]


# --- pull -------------------------------------------------------------------------------------
def test_pull_brings_down_an_earlier_attempt(tmp_path):
    """So responses already in storage are reused instead of paid for again."""
    container = FakeContainer({"outputs/runs/r/raw_foundry_responses/site_1.json": b'{"a": 1}'})
    target = tmp_path / "fresh"
    assert storage.pull("outputs/runs/r", target, client=container) == 1
    assert (target / "raw_foundry_responses" / "site_1.json").read_bytes() == b'{"a": 1}'


def test_pull_never_overwrites_a_local_file(tmp_path):
    """Local wins: overwriting could undo work, such as a seed snapshot that pins the run."""
    run = run_folder(tmp_path)
    container = FakeContainer({"outputs/runs/r/seed.csv": b"STALE FROM BLOB"})
    assert storage.pull("outputs/runs/r", run, client=container) == 0
    assert (run / "seed.csv").read_text() == "site_id\n1\n"


def test_push_then_pull_round_trips_a_run(tmp_path):
    run = run_folder(tmp_path)
    container = FakeContainer()
    storage.push(run, "outputs/runs/r", client=container)
    elsewhere = tmp_path / "other_machine"
    storage.pull("outputs/runs/r", elsewhere, client=container)
    for path in run.rglob("*"):
        if path.is_file():
            assert (elsewhere / path.relative_to(run)).read_bytes() == path.read_bytes()


def test_pull_leaves_no_temporary_files(tmp_path):
    container = FakeContainer({"outputs/runs/r/a.json": b"{}"})
    target = tmp_path / "t"
    storage.pull("outputs/runs/r", target, client=container)
    assert not list(target.rglob("*.tmp"))


# --- configuration and errors -----------------------------------------------------------------
def test_unconfigured_means_local(monkeypatch):
    monkeypatch.setenv("AZURE_STORAGE_ACCOUNT", "")
    monkeypatch.setenv("AZURE_STORAGE_CONNECTION_STRING", "")
    assert not storage.configured()


def test_an_account_name_counts_as_configured(monkeypatch):
    monkeypatch.setenv("AZURE_STORAGE_ACCOUNT", "someaccount")
    assert storage.configured()


@pytest.mark.parametrize("value,forced", [("1", True), ("true", True), ("", False), ("0", False)])
def test_waste_ai_search_local_forces_local(monkeypatch, value, forced):
    monkeypatch.setenv("WASTE_AI_SEARCH_LOCAL", value)
    assert storage.forced_local() is forced


def _service_raising(exc):
    container = SimpleNamespace(get_container_properties=lambda: (_ for _ in ()).throw(exc))
    return SimpleNamespace(get_container_client=lambda name: container)


def test_a_firewall_block_is_reported_as_a_vpn_problem_not_a_credential_one(monkeypatch):
    """RDP-52 lost two detours to AuthorizationFailure reading like a bad key."""
    from azure.core.exceptions import HttpResponseError
    import azure.storage.blob as blob

    monkeypatch.setenv("AZURE_STORAGE_CONNECTION_STRING", "UseDevelopmentStorage=true")
    monkeypatch.setattr(blob.BlobServiceClient, "from_connection_string",
                        staticmethod(lambda cs: _service_raising(HttpResponseError(message="AuthorizationFailure"))))
    with pytest.raises(storage.VpnRequiredError, match="not your credentials.*RMI-SP-FLEX-VNET"):
        storage.container_client()


def test_a_missing_container_says_so(monkeypatch):
    from azure.core.exceptions import ResourceNotFoundError
    import azure.storage.blob as blob

    monkeypatch.setenv("AZURE_STORAGE_CONNECTION_STRING", "UseDevelopmentStorage=true")
    monkeypatch.setattr(blob.BlobServiceClient, "from_connection_string",
                        staticmethod(lambda cs: _service_raising(ResourceNotFoundError(message="gone"))))
    with pytest.raises(storage.StorageError, match="does not exist"):
        storage.container_client()


# --- the CLI's sync --------------------------------------------------------------------------
def config(tmp_path):
    from waste_ai_search.run_context import PipelineConfig

    return PipelineConfig(input_csv=None, run_dir=run_folder(tmp_path), run_id="r")


def test_unconfigured_blob_says_results_stay_local(tmp_path, monkeypatch, capsys):
    from waste_ai_search.cli import RunSync

    monkeypatch.setenv("WASTE_AI_SEARCH_LOCAL", "")
    sync = RunSync(config(tmp_path), local=False)
    assert sync.client is None
    assert "results stay local only" in capsys.readouterr().out


def test_local_never_opens_a_container(tmp_path, monkeypatch):
    from waste_ai_search.cli import RunSync

    monkeypatch.setenv("AZURE_STORAGE_ACCOUNT", "someaccount")
    monkeypatch.setattr(storage, "container_client", lambda: pytest.fail("opened a container in local mode"))
    assert RunSync(config(tmp_path), local=True).client is None


def test_configured_blob_pulls_before_and_pushes_after(tmp_path, monkeypatch):
    from waste_ai_search.cli import RunSync

    container = FakeContainer({"outputs/runs/r/raw_foundry_responses/site_9.json": b"{}"})
    monkeypatch.setenv("WASTE_AI_SEARCH_LOCAL", "")
    monkeypatch.setenv("AZURE_STORAGE_ACCOUNT", "someaccount")
    monkeypatch.setattr(storage, "container_client", lambda: container)

    cfg = config(tmp_path)
    sync = RunSync(cfg, local=False)
    sync.down()
    assert (cfg.run_dir / "raw_foundry_responses" / "site_9.json").exists()
    sync.up()
    assert "outputs/runs/r/seed.csv" in container.blobs


def test_a_storage_error_exits_cleanly_without_a_traceback(tmp_path, monkeypatch, capsys):
    from waste_ai_search import cli

    monkeypatch.setenv("WASTE_AI_SEARCH_LOCAL", "")
    monkeypatch.setenv("AZURE_STORAGE_ACCOUNT", "someaccount")
    monkeypatch.setattr(storage, "container_client",
                        lambda: (_ for _ in ()).throw(storage.VpnRequiredError("connect to the VPN")))
    code = cli.main(["arbitrate", "--run-id", "r", "--run-dir", str(tmp_path / "run")])
    assert code == 1
    assert "connect to the VPN" in capsys.readouterr().err


# --- publishing to SharePoint -----------------------------------------------------------------
def test_publishing_is_skipped_when_not_configured(tmp_path, capsys):
    assert publish_review(run_folder(tmp_path), "r") is None
    assert "not configured" in capsys.readouterr().out


def test_the_first_publish_copies_into_a_folder_named_for_the_run(tmp_path):
    run, sharepoint = run_folder(tmp_path), tmp_path / "sp"
    sharepoint.mkdir()
    dest = publish_review(run, "r", target_dir=sharepoint)
    assert dest == sharepoint / "r" / "r_review.xlsx"
    assert dest.read_bytes() == b"workbook"
    assert str(dest) in json.loads((run / PUBLISH_LEDGER).read_text())


def test_an_untouched_copy_is_replaced_in_place(tmp_path):
    run, sharepoint = run_folder(tmp_path), tmp_path / "sp"
    sharepoint.mkdir()
    publish_review(run, "r", target_dir=sharepoint)
    (run / "r_review.xlsx").write_bytes(b"re-arbitrated")

    dest = publish_review(run, "r", target_dir=sharepoint)
    assert dest == sharepoint / "r" / "r_review.xlsx"
    assert dest.read_bytes() == b"re-arbitrated"
    assert len(list((sharepoint / "r").iterdir())) == 1


def test_an_sme_edited_copy_is_never_overwritten(tmp_path):
    """The rule that matters: reviewers record decisions in the SharePoint copy."""
    run, sharepoint = run_folder(tmp_path), tmp_path / "sp"
    sharepoint.mkdir()
    published = publish_review(run, "r", target_dir=sharepoint)
    published.write_bytes(b"workbook with SME decisions")
    (run / "r_review.xlsx").write_bytes(b"re-arbitrated")

    dest = publish_review(run, "r", target_dir=sharepoint)
    assert published.read_bytes() == b"workbook with SME decisions"
    assert dest != published and dest.name.startswith("r_review_") and dest.read_bytes() == b"re-arbitrated"


def test_a_copy_open_in_excel_is_not_replaced(tmp_path):
    run, sharepoint = run_folder(tmp_path), tmp_path / "sp"
    sharepoint.mkdir()
    published = publish_review(run, "r", target_dir=sharepoint)
    (published.parent / f"~${published.name}").write_text("lock")
    (run / "r_review.xlsx").write_bytes(b"re-arbitrated")

    dest = publish_review(run, "r", target_dir=sharepoint)
    assert published.read_bytes() == b"workbook"
    assert dest != published


def test_a_copy_nobody_published_from_this_run_is_treated_as_edited(tmp_path):
    """No ledger entry means it cannot be shown to be ours, so it is not overwritten."""
    run, sharepoint = run_folder(tmp_path), tmp_path / "sp"
    (sharepoint / "r").mkdir(parents=True)
    foreign = sharepoint / "r" / "r_review.xlsx"
    foreign.write_bytes(b"someone else's file")

    dest = publish_review(run, "r", target_dir=sharepoint)
    assert foreign.read_bytes() == b"someone else's file" and dest != foreign


def test_republishing_identical_content_does_nothing(tmp_path):
    run, sharepoint = run_folder(tmp_path), tmp_path / "sp"
    sharepoint.mkdir()
    first = publish_review(run, "r", target_dir=sharepoint)
    assert publish_review(run, "r", target_dir=sharepoint) == first
    assert len(list((sharepoint / "r").iterdir())) == 1


def test_a_missing_sharepoint_folder_is_an_error_not_a_silent_skip(tmp_path):
    with pytest.raises(FileNotFoundError, match="OneDrive is syncing"):
        publish_review(run_folder(tmp_path), "r", target_dir=tmp_path / "not_synced")


def test_publishing_leaves_no_temporary_file_for_onedrive_to_upload(tmp_path):
    run, sharepoint = run_folder(tmp_path), tmp_path / "sp"
    sharepoint.mkdir()
    publish_review(run, "r", target_dir=sharepoint)
    assert not list(sharepoint.rglob("*.tmp"))


# --- Copilot review on WP-534 -------------------------------------------------------------------
def test_two_versioned_publishes_in_one_second_never_collide(tmp_path, monkeypatch):
    """A timestamp alone repeats within a second; replacing that file could destroy SME edits."""
    import waste_ai_search.publish as pub

    class FrozenNow:
        @staticmethod
        def now():
            from datetime import datetime as real
            return real(2026, 10, 2, 9, 0, 0)

    monkeypatch.setattr(pub, "datetime", FrozenNow)
    run, sharepoint = run_folder(tmp_path), tmp_path / "sp"
    sharepoint.mkdir()
    published = publish_review(run, "r", target_dir=sharepoint)
    published.write_bytes(b"SME decisions")                 # forces versioned publishing

    (run / "r_review.xlsx").write_bytes(b"version 2")
    first = publish_review(run, "r", target_dir=sharepoint)
    first.write_bytes(b"SME edited version 2 too")           # an SME works in the versioned copy

    (run / "r_review.xlsx").write_bytes(b"version 3")
    second = publish_review(run, "r", target_dir=sharepoint)

    assert first.name == "r_review_20261002_090000.xlsx"
    assert second.name == "r_review_20261002_090000_2.xlsx"
    assert first.read_bytes() == b"SME edited version 2 too"
    assert published.read_bytes() == b"SME decisions"


@pytest.mark.parametrize("hostile", [
    "outputs/runs/r/../../outside.txt",
    "outputs/runs/r/sub/../../../escape.txt",
    "outputs/runs/r/",
    "outputs/runs/r//etc/passwd",
])
def test_a_blob_name_cannot_write_outside_the_run_folder(tmp_path, hostile):
    target = tmp_path / "base" / "run"
    container = FakeContainer({hostile: b"payload", "outputs/runs/r/ok.json": b"{}"})
    storage.pull("outputs/runs/r", target, client=container)

    assert (target / "ok.json").exists()
    written = [p for p in tmp_path.rglob("*") if p.is_file()]
    assert all(target in p.parents for p in written), written
    assert not (tmp_path / "base" / "outside.txt").exists()
    assert not (tmp_path / "escape.txt").exists()


@pytest.mark.parametrize("hidden", [".cache/token", "sub/.git/config", "raw_foundry_responses/.secret/a.json"])
def test_nothing_inside_a_hidden_directory_is_uploaded(tmp_path, hidden):
    run = run_folder(tmp_path)
    (run / hidden).parent.mkdir(parents=True, exist_ok=True)
    (run / hidden).write_text("x")
    container = FakeContainer()
    storage.push(run, "outputs/runs/r", client=container)
    assert f"outputs/runs/r/{hidden}" not in container.blobs
    assert "outputs/runs/r/raw_foundry_responses/site_1.json" in container.blobs  # ordinary nesting kept


def test_the_cli_no_longer_promises_an_unconditionally_offline_arbitrate():
    """Once blob is configured, arbitrate pulls from storage, so it is not offline unless --local."""
    from waste_ai_search.cli import build_parser

    text = " ".join(build_parser().format_help().split())
    assert "Offline, free, and repeatable" not in text
    assert "arbitrate is offline" not in text
    assert "pass --local to stay fully offline" in text
