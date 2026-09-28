#!/usr/bin/env python3
"""Vendor the standardized facility spec from the upstream ETL repository.

The spec is owned by RMI/waste_data_ingestion_pipeline, which generates the
standardized facility table this repository searches against. Rather than track a
floating pointer at upstream `main`, the spec is vendored into `inputs/` and pinned to
the exact upstream commit recorded in `inputs/SCHEMA_SOURCE.json`. A rename or a type
change upstream therefore lands as a reviewable diff here instead of silently
invalidating a run already in flight.

Usage:
  uv run python scripts/sync_schema.py            # pull upstream main, rewrite vendored copy
  uv run python scripts/sync_schema.py --check    # exit 1 if the pin is behind upstream
  uv run python scripts/sync_schema.py --verify   # offline; vendored bytes match the pin
  uv run python scripts/sync_schema.py --ref SHA  # pin to one upstream commit

`--check` and the default sync both read a private repository, so they need a GitHub
token that can see it: an authenticated `gh` locally, or GH_TOKEN in CI.
"""
from __future__ import annotations

import argparse
import base64
import datetime as dt
import difflib
import hashlib
import json
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]

UPSTREAM_REPO = "RMI/waste_data_ingestion_pipeline"
UPSTREAM_PATH = "facility_etl/StandardizedFacilityTableSpecification.md"

TARGET = ROOT / "inputs" / "StandardizedFacilityTableSpecification.md"
PROVENANCE = ROOT / "inputs" / "SCHEMA_SOURCE.json"


class UpstreamError(RuntimeError):
    """Raised when the upstream repository cannot be read."""


def gh_api(endpoint: str) -> object:
    """Call the GitHub API through `gh`, which already holds credentials for the org."""
    try:
        completed = subprocess.run(
            ["gh", "api", endpoint],
            capture_output=True,
            text=True,
            check=False,
        )
    except FileNotFoundError as exc:
        raise UpstreamError(
            "`gh` was not found on PATH. Install the GitHub CLI, or set GH_TOKEN and "
            "run this in an environment that provides `gh`."
        ) from exc

    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout).strip()
        raise UpstreamError(
            f"GitHub API call failed for {endpoint}.\n"
            f"  {detail}\n"
            f"Confirm you can read {UPSTREAM_REPO}; it is private, so a token without "
            f"access to the RMI org will 404 rather than 403."
        )
    return json.loads(completed.stdout)


def fetch_upstream(ref: str) -> tuple[str, str]:
    """Return the spec text at `ref` and the commit that last changed it at that ref."""
    payload = gh_api(f"repos/{UPSTREAM_REPO}/contents/{UPSTREAM_PATH}?ref={ref}")
    if not isinstance(payload, dict) or "content" not in payload:
        raise UpstreamError(f"{UPSTREAM_PATH} was not found in {UPSTREAM_REPO}@{ref}.")
    text = base64.b64decode(payload["content"]).decode("utf-8")

    # Pin to the commit that last touched the spec, not to the branch head. The branch
    # moves on every unrelated merge; this SHA changes only when the schema does.
    commits = gh_api(
        f"repos/{UPSTREAM_REPO}/commits?path={UPSTREAM_PATH}&sha={ref}&per_page=1"
    )
    if not isinstance(commits, list) or not commits:
        raise UpstreamError(f"No commit history for {UPSTREAM_PATH} at {ref}.")
    return text, commits[0]["sha"]


def digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def read_provenance() -> dict | None:
    if not PROVENANCE.exists():
        return None
    return json.loads(PROVENANCE.read_text())


def summarize(old: str, new: str) -> str:
    """Report the spec lines this codebase binds to: column definitions AND allowed values.

    Both matter and they look different in the markdown. A column row is "| `name` | type | ... |";
    an allowed value is "| 'Value' |" in one of the enum tables. Reporting only the first missed
    'Transfer Station' being added to `facility_type`, which is exactly the kind of change that
    needs a decision here.
    """
    diff = list(
        difflib.unified_diff(
            old.splitlines(), new.splitlines(), lineterm="", n=0
        )
    )
    bindings = [
        line
        for line in diff
        if line[:1] in "+-"
        and not line.startswith(("---", "+++"))
        and (line[1:4] == "| `" or line[1:4] == "| '")
    ]
    if not bindings:
        return "  (prose changed; no column definitions or allowed values changed)"
    return "\n".join(f"  {line}" for line in bindings)


def do_sync(ref: str) -> int:
    text, sha = fetch_upstream(ref)
    previous = TARGET.read_text() if TARGET.exists() else ""

    if previous == text:
        print(f"Already current with {UPSTREAM_REPO}@{sha[:8]}; no change to the spec.")
    else:
        print(f"Updating vendored spec to {UPSTREAM_REPO}@{sha[:8]}.")
        print(summarize(previous, text))
        TARGET.write_text(text)

    PROVENANCE.write_text(
        json.dumps(
            {
                "repo": UPSTREAM_REPO,
                "path": UPSTREAM_PATH,
                "ref": sha,
                "sha256": digest(text),
                "synced_at": dt.datetime.now(dt.timezone.utc)
                .replace(microsecond=0)
                .isoformat(),
            },
            indent=2,
        )
        + "\n"
    )
    print(f"Pin recorded in {PROVENANCE.relative_to(ROOT)}.")
    return 0


def do_check(ref: str) -> int:
    if not TARGET.exists():
        print(f"No vendored spec at {TARGET.relative_to(ROOT)}; run without --check.")
        return 1

    text, sha = fetch_upstream(ref)
    vendored = TARGET.read_text()
    if vendored == text:
        print(f"Vendored spec matches {UPSTREAM_REPO}@{sha[:8]}.")
        return 0

    print(f"Vendored spec is BEHIND {UPSTREAM_REPO}@{sha[:8]}.")
    print(summarize(vendored, text))
    print("\nRun `uv run python scripts/sync_schema.py` to pull it in, then update the")
    print("code that binds to the changed columns before committing.")
    return 1


def do_verify() -> int:
    """Offline integrity check: the vendored bytes are the ones the pin describes."""
    provenance = read_provenance()
    if provenance is None:
        print(f"No {PROVENANCE.relative_to(ROOT)}; nothing to verify against.")
        return 1
    if not TARGET.exists():
        print(f"No vendored spec at {TARGET.relative_to(ROOT)}.")
        return 1

    actual = digest(TARGET.read_text())
    expected = provenance.get("sha256")
    if actual == expected:
        print(f"Vendored spec matches its pin ({provenance['ref'][:8]}).")
        return 0

    print("Vendored spec has been edited locally and no longer matches its pin.")
    print(f"  expected sha256 {expected}")
    print(f"  actual   sha256 {actual}")
    print("\nThe spec is owned upstream; change it there, then re-run this script.")
    return 1


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="fail if behind upstream")
    parser.add_argument("--verify", action="store_true", help="offline pin integrity check")
    parser.add_argument("--ref", default="main", help="upstream ref to read (default: main)")
    args = parser.parse_args()

    if args.check and args.verify:
        parser.error("--check and --verify are separate modes; pass one.")

    try:
        if args.verify:
            return do_verify()
        if args.check:
            return do_check(args.ref)
        return do_sync(args.ref)
    except UpstreamError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
