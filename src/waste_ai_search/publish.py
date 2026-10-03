"""Publish a run's review workbook to the team's SharePoint folder (WP-534).

The folder is a SharePoint library synced to this machine by OneDrive, so publishing is a file
copy and OneDrive does the upload: no Graph API and no app registration. It is set with
`WASTE_AI_SEARCH_REVIEW_DIR`; each run gets `<review dir>/<run_id>/<run_id>_review.xlsx`.

The rule that matters: **an SME's work is never overwritten.** Once a workbook is in SharePoint,
reviewers record decisions in it. Re-arbitrating and publishing again must not replace that copy.
Every file this module writes is fingerprinted in the run's `published.json`, and a SharePoint
copy is overwritten only if it is byte-identical to what was published - i.e. nobody has saved
it since. An edited copy, or one open in Excel right now, is left alone and the new version is
written beside it with a timestamp.
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
from datetime import datetime
from pathlib import Path


PUBLISH_LEDGER = "published.json"


def review_dir() -> Path | None:
    """The synced SharePoint folder, or None when publishing is not configured."""
    try:
        from dotenv import load_dotenv  # type: ignore

        load_dotenv()
    except ImportError:
        pass
    value = (os.environ.get("WASTE_AI_SEARCH_REVIEW_DIR") or "").strip()
    return Path(value).expanduser() if value else None


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _ledger(run_dir: Path) -> dict[str, str]:
    path = run_dir / PUBLISH_LEDGER
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}


def _record(run_dir: Path, destination: Path, digest: str) -> None:
    ledger = _ledger(run_dir)
    ledger[str(destination)] = digest
    (run_dir / PUBLISH_LEDGER).write_text(json.dumps(ledger, indent=2, sort_keys=True), encoding="utf-8")


def _copy_atomically(source: Path, destination: Path) -> None:
    """Never leave a half-written workbook for OneDrive to upload. OneDrive ignores *.tmp."""
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(f".{destination.name}.tmp")
    try:
        shutil.copyfile(source, temporary)
        os.replace(temporary, destination)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise


def _open_in_excel(path: Path) -> bool:
    """Excel keeps a `~$<name>` lock file beside a workbook while it is open."""
    return (path.parent / f"~${path.name}").exists()


def _unused_sibling(destination: Path, workbook: Path) -> Path:
    """A timestamped name beside `destination` that nothing occupies yet.

    The timestamp alone is not enough: two publishes in the same second would produce the same
    name, and replacing that file could destroy edits an SME has made to it. A counter is added
    until the name is free.
    """
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    candidate = destination.with_name(f"{workbook.stem}_{stamp}{workbook.suffix}")
    counter = 2
    while True:
        if not (candidate.parent / f"~${candidate.name}").exists():
            try:
                # Reserve the name atomically, so a concurrent publish cannot pick it too.
                os.close(os.open(candidate, os.O_CREAT | os.O_EXCL | os.O_WRONLY))
                return candidate
            except FileExistsError:
                pass
        candidate = destination.with_name(f"{workbook.stem}_{stamp}_{counter}{workbook.suffix}")
        counter += 1


def publish_review(run_dir: Path, run_id: str, target_dir: Path | None = None) -> Path | None:
    """Copy the run's review workbook into SharePoint. Returns where it went, or None if skipped."""
    target_dir = target_dir or review_dir()
    workbook = run_dir / f"{run_id}_review.xlsx"
    if target_dir is None:
        print("SharePoint publishing not configured (WASTE_AI_SEARCH_REVIEW_DIR unset); skipped.")
        return None
    if not workbook.exists():
        print(f"No review workbook at {workbook}; nothing to publish.")
        return None
    if not target_dir.exists():
        raise FileNotFoundError(
            f"SharePoint folder {target_dir} does not exist. Check WASTE_AI_SEARCH_REVIEW_DIR, and "
            "that OneDrive is syncing that library."
        )

    destination = target_dir / run_id / workbook.name
    new_digest = _sha256(workbook)

    if destination.exists():
        current = _sha256(destination)
        if current == new_digest:
            print(f"SharePoint copy is already current: {destination}")
            return destination
        ours_and_untouched = current == _ledger(run_dir).get(str(destination))
        if not ours_and_untouched or _open_in_excel(destination):
            reason = "is open in Excel" if _open_in_excel(destination) else "has been edited since it was published"
            destination = _unused_sibling(destination, workbook)
            print(f"The SharePoint copy {reason}; leaving it untouched and publishing beside it.")

    _copy_atomically(workbook, destination)
    _record(run_dir, destination, new_digest)
    print(f"Published review workbook: {destination}")
    return destination
