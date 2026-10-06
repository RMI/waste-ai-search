from __future__ import annotations

import csv
from pathlib import Path
from typing import Any

from .schema import GEOCODE_FIELDS, WASTE_SITE_BASE_EXTRA_FIELDS, is_blank, normalize_scalar


def read_csv_records(path: Path) -> tuple[list[dict[str, Any]], list[str]]:
    with path.open(newline="", encoding="utf-8-sig") as handle:
        reader = csv.DictReader(handle)
        rows = [dict(row) for row in reader]
        return rows, list(reader.fieldnames or [])


def first_present(row: dict[str, Any], *keys: str) -> Any:
    """The first key whose value is not blank, treating 0 and False as present."""
    for key in keys:
        if not is_blank(row.get(key)):
            return row.get(key)
    return None


def normalize_site_record(row: dict[str, Any]) -> dict[str, Any]:
    normalized = dict(row)
    site_id = row.get("facility_id") or row.get("site_id") or row.get("id")
    site_name = row.get("facility_name") or row.get("site_name") or row.get("name")
    country_iso3 = row.get("iso3c_plus") or row.get("country_iso3") or row.get("iso3")
    # `area` is the pre-8c0bb3fe spelling; seed CSVs generated before the rename still use
    # it, so both are accepted on read and only the new name is written back out.
    #
    # Picked on normalized emptiness rather than with `or`, because a zero area is a real value
    # the schema permits and `seed_source.is_empty` is careful to preserve. Reading a CSV these
    # arrive as the string "0", which is truthy, so `or` happened to work; an int 0 from any
    # other caller would silently fall through to an older alias.
    area = first_present(row, "area_square_meters", "input_area_square_meters", "area")

    normalized["site_id"] = normalize_scalar(site_id)
    normalized["site_name"] = normalize_scalar(site_name)
    normalized["country_iso3"] = normalize_scalar(country_iso3)
    normalized["input_area_square_meters"] = normalize_scalar(area)
    normalized.setdefault("pilot_selection_reason", "")
    for field in GEOCODE_FIELDS:
        normalized.setdefault(field, "")
    return normalized


def load_sites(path: Path) -> tuple[list[dict[str, Any]], list[str]]:
    rows, headers = read_csv_records(path)
    normalized = [normalize_site_record(row) for row in rows]
    output_headers = merge_headers(headers, WASTE_SITE_BASE_EXTRA_FIELDS)
    return normalized, output_headers


def merge_headers(*header_groups: list[str]) -> list[str]:
    headers: list[str] = []
    seen: set[str] = set()
    for group in header_groups:
        for header in group:
            if header and header not in seen:
                headers.append(header)
                seen.add(header)
    return headers


def write_csv_records(path: Path, rows: list[dict[str, Any]], headers: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=headers, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)



REFRESHED_SEED_NAME = "refreshed_seed.csv"


def refreshed_seed_path(config: Any) -> Path:
    """Where the gas-collection follow-up writes the seed it rewrote between passes."""
    return config.run_dir / REFRESHED_SEED_NAME


def run_seed_path(config: Any) -> Path:
    """Where a database-seeded run keeps the corpus it read."""
    return config.run_dir / "seed.csv"


def resolve_sites(config: Any) -> tuple[list[dict[str, Any]], list[str]]:
    """The run's seed: an explicit CSV, else this run's own snapshot, else the live database.

    The database is the source of truth, so a new run reads it directly rather than depending on
    a checked-in export that is stale the moment consolidation moves. But it reads it ONCE. The
    first read is written to the run directory, and every later phase of the same run - both
    arbitrations, the refresh between passes, a resumed search, a standalone `arbitrate` -
    reads that snapshot instead of querying again.

    Without that, each phase could see a different corpus from the one the search ran against,
    the snapshot would record something nothing used, and `arbitrate`, which the CLI promises is
    offline, would need the database.

    `--input-csv` still wins for the two cases that need a file: pinning a corpus, and the
    gas-collection follow-up, which rewrites the seed between passes. Country filtering on the
    first read is pushed into SQL rather than applied after loading.
    """
    snapshot = run_seed_path(config)
    if config.input_csv is not None:
        # The one file that bypasses the snapshot is the pass-2 follow-up's refreshed seed: the
        # gas-collection pass rewrites the seed between passes and must read that rewrite, while
        # seed.csv stays the corpus pass 1 searched. It applies only to that exact file, and only
        # once the original snapshot exists. Any other CSV - even one stored inside the run
        # directory - is pinned and guarded like any supplied file; exempting all of them let a
        # first run skip writing seed.csv, so a later arbitrate fell through to the live database.
        if Path(config.input_csv).resolve() == refreshed_seed_path(config).resolve() and snapshot.exists():
            return load_sites(config.input_csv)

        # A supplied file is pinned into the run on first use, since it often lives somewhere
        # temporary and the run must stay re-arbitrable after it is gone.
        if not snapshot.exists():
            copy_snapshot_atomically(config.input_csv, snapshot)
            return load_sites(snapshot)

        # Once pinned, the run reads its snapshot, exactly as a database-seeded run does. Reading
        # the supplied file instead would let a rerun search one corpus while seed.csv records
        # another, and a later arbitrate would then load the wrong one. A DIFFERENT file is an
        # error rather than silently ignored: the run id is pinned, and searching another corpus
        # needs a new one.
        supplied = Path(config.input_csv)
        if supplied.exists() and supplied.read_bytes() != snapshot.read_bytes():
            raise ValueError(
                f"Run {config.run_id!r} is pinned to {snapshot}, which differs from "
                f"--input-csv {supplied}. Use a new --run-id to search a different corpus."
            )
        return load_sites(snapshot)


    if snapshot.exists():
        return load_sites(snapshot)

    from . import snapshot as consolidation
    from .seed_source import load_seed_sites, seed_headers

    # F32: keep the consolidation tables this run searches against, because every rebuild
    # reassigns internal_facility_id - the run's site_id. Taken before seeding and checked after,
    # so the seed and the copy describe the same consolidation run.
    manifest = consolidation.take_snapshot(config.run_dir)
    sites = load_seed_sites(iso3=config.iso3 or None)
    if consolidation.current_consolidation_run_ids() != manifest["consolidation_run_ids"]:
        raise RuntimeError("Consolidation was rebuilt while this run was seeding; run it again.")
    headers = seed_headers()
    write_snapshot_atomically(snapshot, sites, headers)
    return sites, headers


def copy_snapshot_atomically(source: Path, path: Path) -> None:
    """Pin a supplied seed file into the run, byte for byte, never as a partial file."""
    import os
    import shutil

    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    try:
        shutil.copyfile(source, temporary)
        os.replace(temporary, path)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise


def write_snapshot_atomically(path: Path, rows: list[dict[str, Any]], headers: list[str]) -> None:
    """Write the run's seed so that `path` only ever exists complete.

    The snapshot's existence is what pins a run, so a write interrupted halfway - Ctrl-C, a full
    disk, or the OneDrive client this repository lives under touching the file mid-write - would
    otherwise leave a truncated corpus that every later phase trusts and never re-queries. The rows
    go to a temporary file in the same directory, which is then renamed over `path`; a rename on
    one filesystem is atomic, so a reader sees the old state or the finished file, never a partial
    one. On failure the temporary file is removed and `path` is left untouched.
    """
    import os

    temporary = path.with_name(f".{path.name}.tmp")
    try:
        write_csv_records(temporary, rows, headers)
        os.replace(temporary, path)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise


def describe_seed(config: Any) -> str:
    """Where resolve_sites WILL read from, for logs and the workbook's Run_Config."""
    if config.input_csv is not None:
        is_followup = Path(config.input_csv).resolve() == refreshed_seed_path(config).resolve()
        if run_seed_path(config).exists() and not is_followup:
            return f"{config.input_csv}, pinned in {run_seed_path(config)}"
        return str(config.input_csv)
    if run_seed_path(config).exists():
        return f"consolidation.consolidated_facility, pinned in {run_seed_path(config)}"
    return "consolidation.consolidated_facility (live)"
