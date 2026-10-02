"""Keep run results in Azure Blob Storage, so they outlive one laptop (WP-534).

Adapted from the sibling refining-ai-search repo (RDP-52, `storage.py`). The container mirrors the
repo layout - `outputs/runs/<run_id>/...` - so a run folder round-trips unchanged.

Credentials come from `AZURE_STORAGE_CONNECTION_STRING`, otherwise `DefaultAzureCredential`
against `AZURE_STORAGE_ACCOUNT` - the same identity this repo already uses for Foundry, so no
account key needs to exist anywhere. RDP-52 used a key that ended up shared in plain text and had
to be rotated.

Two deliberate differences from RDP-52:

- **Blob is used when configured, not unconditionally.** The storage account is not chosen yet,
  and making blob mandatory now would fail every run. Until `AZURE_STORAGE_ACCOUNT` (or a
  connection string) is set, runs stay local and say so on every run, so nobody mistakes local
  results for persisted ones. `--local` or `WASTE_AI_SEARCH_LOCAL=1` forces local even then.
- **Transfers are parallel from the start.** RDP-52 left it as a follow-up after a sequential pull
  took about seven minutes before a full run could begin.

The storage account is expected to sit behind a network firewall, as RDP-52's does. Off VPN Azure
answers `AuthorizationFailure`, which reads like a credential problem; it cost RDP-52 two detours.
It is reported as VpnRequiredError instead, which says plainly that credentials are not at fault.
"""
from __future__ import annotations

import os
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any


DEFAULT_CONTAINER = "waste-ai-search"
MAX_WORKERS = 16
VPN_NAME = "RMI-SP-FLEX-VNET"


class StorageError(RuntimeError):
    """Blob storage could not be used; the message says what to do."""


class VpnRequiredError(StorageError):
    """The storage firewall refused the connection - a network problem, not a credential one."""


def _load_env() -> None:
    try:
        from dotenv import load_dotenv  # type: ignore

        load_dotenv()
    except ImportError:
        pass


def _env(name: str) -> str:
    return (os.environ.get(name) or "").strip()


def forced_local() -> bool:
    """True when the environment asks for local-only reads and writes."""
    _load_env()
    return _env("WASTE_AI_SEARCH_LOCAL").lower() in {"1", "true", "yes"}


def configured() -> bool:
    """Whether a storage account or connection string has been set at all."""
    _load_env()
    return bool(_env("AZURE_STORAGE_CONNECTION_STRING") or _env("AZURE_STORAGE_ACCOUNT"))


def container_name() -> str:
    return _env("AZURE_STORAGE_CONTAINER") or DEFAULT_CONTAINER


def run_prefix(run_id: str) -> str:
    """Where a run lives in the container - the same relative path as in the repo."""
    return f"outputs/runs/{run_id}"


def uri(name: str) -> str:
    account = _env("AZURE_STORAGE_ACCOUNT") or "<account>"
    return f"https://{account}.blob.core.windows.net/{container_name()}/{name}"


def _is_firewall_block(exc: Exception) -> bool:
    # The SDK spells the code either way depending on version.
    text = f"{getattr(exc, 'error_code', '')} {exc}"
    return "AuthorizationFailure" in text or "AUTHORIZATION_FAILURE" in text


def container_client() -> Any:
    """A ContainerClient, with the connection checked once before any real work."""
    from azure.core.exceptions import HttpResponseError, ResourceNotFoundError
    from azure.storage.blob import BlobServiceClient

    connection_string = _env("AZURE_STORAGE_CONNECTION_STRING")
    if connection_string:
        service = BlobServiceClient.from_connection_string(connection_string)
    else:
        from azure.identity import DefaultAzureCredential

        service = BlobServiceClient(
            account_url=f"https://{_env('AZURE_STORAGE_ACCOUNT')}.blob.core.windows.net",
            credential=DefaultAzureCredential(),
        )
    client = service.get_container_client(container_name())
    try:
        client.get_container_properties()
    except ResourceNotFoundError as exc:
        raise StorageError(
            f"Container {container_name()!r} does not exist. Create it, or set "
            "AZURE_STORAGE_CONTAINER to the correct name. To work without blob, pass --local."
        ) from exc
    except HttpResponseError as exc:
        if _is_firewall_block(exc):
            raise VpnRequiredError(
                f"Blob storage refused the connection (AuthorizationFailure). This is the storage "
                f"firewall, not your credentials: connect to the {VPN_NAME} VPN and retry, or pass "
                "--local to work without blob."
            ) from exc
        raise
    return client


def _skip(path: Path) -> bool:
    """Files that must never be uploaded: Excel lock files, dotfiles and temporaries."""
    name = path.name
    return name.startswith("~$") or name.startswith(".") or name.endswith(".tmp")


def push(source: Path, prefix: str, client: Any = None) -> int:
    """Upload every file under `source` to `prefix/...`, overwriting. Returns the count."""
    client = client or container_client()
    files = [p for p in sorted(source.rglob("*")) if p.is_file() and not _skip(p)]

    def upload(path: Path) -> None:
        name = f"{prefix}/{path.relative_to(source).as_posix()}"
        with path.open("rb") as handle:
            client.upload_blob(name, handle, overwrite=True)

    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        list(executor.map(upload, files))
    return len(files)


def pull(prefix: str, target: Path, client: Any = None) -> int:
    """Download what is under `prefix/` into `target`, without overwriting local files.

    Local wins: a file already on disk is newer than or the same as what is in storage - this
    machine wrote it, or pulled it before - and overwriting it could undo work, such as a seed
    snapshot that pins the run. Only files missing locally come down. Returns the count.
    """
    client = client or container_client()
    names = [blob.name for blob in client.list_blobs(name_starts_with=f"{prefix}/")]
    wanted = [(name, target / name[len(prefix) + 1:]) for name in names]
    wanted = [(name, path) for name, path in wanted if not path.exists()]

    def download(item: tuple[str, Path]) -> None:
        name, path = item
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(f".{path.name}.tmp")
        with temporary.open("wb") as handle:
            client.download_blob(name).readinto(handle)
        os.replace(temporary, path)

    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        list(executor.map(download, wanted))
    return len(wanted)
