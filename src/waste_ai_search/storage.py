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


def _skip(path: Path, root: Path) -> bool:
    """Files that must never be uploaded: Excel lock files, hidden content and temporaries.

    Every component of the path below the run folder is checked, not just the file name, so
    nothing inside a hidden directory such as `.cache/` is uploaded either.
    """
    parts = path.relative_to(root).parts
    if any(part.startswith(".") for part in parts):
        return True
    name = path.name
    return name.startswith("~$") or name.endswith(".tmp")


def _safe_local_path(name: str, prefix: str, target: Path) -> Path | None:
    """Where a blob may be written under `target`, or None if its name would escape it.

    Blob names are not trusted as paths: one such as `outputs/runs/r/../../outside` matches the
    prefix but would write outside the run folder. Empty, absolute and `..` names are refused, and
    the result must still sit inside `target` once resolved.
    """
    relative = name[len(prefix) + 1:]
    if not relative or relative.startswith(("/", "\\")):
        return None
    parts = Path(relative).parts
    if any(part in ("..", "") for part in parts) or Path(relative).is_absolute():
        return None
    path = (target / relative)
    try:
        path.resolve().relative_to(target.resolve())
    except ValueError:
        return None
    return path


# Seconds a blob must be newer than the local file before it replaces it, so clock skew between
# machines and filesystem timestamp rounding never make an unchanged file look newer.
CLOCK_TOLERANCE_SECONDS = 5.0


def transfer_stage(relative: str) -> int:
    """Files move in stages, each starting only once the one before it has finished.

    Data first; then each snapshot manifest, which marks its folder complete (WP-548); then the
    run's seed. So a seed in blob, or pulled to disk, proves the snapshot taken before it is whole,
    and a resumed run with a seed but no snapshot folder at all is a run that never had one.
    """
    from .input_loader import SEED_NAME
    from .snapshot import MANIFEST_NAME

    if relative == SEED_NAME:
        return 2
    return 1 if Path(relative).name == MANIFEST_NAME else 0


def pinned(relative: str) -> bool:
    """Files that never change once written, and that a pull must therefore never replace."""
    from .input_loader import SEED_NAME
    from .snapshot import SNAPSHOT_DIR_NAME

    return relative == SEED_NAME or relative.startswith(f"{SNAPSHOT_DIR_NAME}/")


def _in_stages(items: list[Any], relative: Any, work: Any) -> None:
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        for stage in (0, 1, 2):
            list(executor.map(work, [item for item in items if transfer_stage(relative(item)) == stage]))


def push(source: Path, prefix: str, client: Any = None) -> int:
    """Upload every file under `source` to `prefix/...`, overwriting. Returns the count."""
    client = client or container_client()
    files = [p for p in sorted(source.rglob("*")) if p.is_file() and not _skip(p, source)]

    def upload(path: Path) -> None:
        name = f"{prefix}/{path.relative_to(source).as_posix()}"
        with path.open("rb") as handle:
            client.upload_blob(name, handle, overwrite=True)

    _in_stages(files, lambda path: path.relative_to(source).as_posix(), upload)
    return len(files)


def pull(prefix: str, target: Path, client: Any = None) -> int:
    """Download what is under `prefix/` into `target`: missing files, and newer copies.

    A file missing locally always comes down. One already on disk is replaced only when the blob
    is newer - another machine changed it since, such as fresh link verdicts or run-log counts -
    and never when it is pinned: a run's seed and consolidation snapshot do not change once
    written, and replacing them could re-point the run at another corpus. Keeping every stale local
    file instead let the next push overwrite another machine's newer work. Returns the count.
    """
    client = client or container_client()
    wanted = []
    for blob in client.list_blobs(name_starts_with=f"{prefix}/"):
        name = blob.name
        path = _safe_local_path(name, prefix, target)
        if path is None:
            print(f"Skipped blob {name!r}: its name would write outside {target}.")
            continue
        if not path.exists():
            wanted.append((name, path))
        elif not pinned(name[len(prefix) + 1:]) and _blob_is_newer(blob, path):
            wanted.append((name, path))

    def download(item: tuple[str, Path]) -> None:
        name, path = item
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(f".{path.name}.tmp")
        with temporary.open("wb") as handle:
            client.download_blob(name).readinto(handle)
        os.replace(temporary, path)

    _in_stages(wanted, lambda item: item[0][len(prefix) + 1:], download)
    return len(wanted)


def _blob_is_newer(blob: Any, path: Path) -> bool:
    modified = getattr(blob, "last_modified", None)
    if modified is None:
        return False
    return modified.timestamp() > path.stat().st_mtime + CLOCK_TOLERANCE_SECONDS
