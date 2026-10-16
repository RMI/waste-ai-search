"""Keep a copy of the consolidation tables a run searched against (F32).

The database is rebuilt by consolidation. Since RMI/waste_data_ingestion_pipeline#196 a facility
keeps its `wastemap_id` across rebuilds, and that is what every output of a run uses as `site_id`.
Runs from before then used `internal_facility_id`, which was reassigned each time. Either way,
without a copy there is no way to tell which facilities changed and need searching again.

So a run that seeds from the database first saves, inside its run directory:

- `consolidated_facility` - the baseline the search filled gaps in;
- `value_resolution_ledger` - which source, and which record in it (`data_source_facility_id`),
  supplied each baseline value;
- `entity_linkage.crosswalk` - all source records linked to each facility, including those that
  supplied no winning value. `data_source` + `facility_id` is the key that survives a rebuild, so
  this is what maps a run's `site_id` onto a later consolidation.

Each table holds every run side by side, so only the run the seed reads is copied: that
consolidation run of the first two, and the crosswalk run it consumed. Every country is copied, not
just the ones searched, in one read-only REPEATABLE READ transaction. Each table is a gzipped CSV in
Postgres' own COPY format, and the manifest records the run with each table's row count and
checksum.
"""
from __future__ import annotations

import gzip
import hashlib
import json
import os
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

SNAPSHOT_DIR_NAME = "consolidation_snapshot"
MANIFEST_NAME = "manifest.json"

# Table -> ORDER BY, so the same database state always produces the same bytes and checksum. Each
# is filtered to one consolidation run (CONSOLIDATION_RUN_COLUMN).
CONSOLIDATION_TABLES = {
    "consolidation.consolidated_facility": "wastemap_id, year",
    "consolidation.value_resolution_ledger": (
        "wastemap_id, year NULLS FIRST, column_name, data_source, data_source_facility_id"
    ),
}
CONSOLIDATION_RUN_COLUMN = "consolidation_run_id"
# Every entity-linkage run is appended to one table; the one wanted is the run consolidation consumed.
CROSSWALK_TABLE = "entity_linkage.crosswalk"
CROSSWALK_RUN_COLUMN = "entity_linkage_run_id"
CROSSWALK_ORDER = "wastemap_id, data_source, facility_id"


def snapshot_dir(run_dir: Path) -> Path:
    return Path(run_dir) / SNAPSHOT_DIR_NAME


def copy_table(
    cursor: Any, table: str, run_column: str, run_id: str, order_by: str, path: Path
) -> dict[str, Any]:
    """Stream one run of one table to a gzipped CSV. Returns its row count and the CSV's checksum.

    Rows are counted by the database in the same transaction: values can hold line breaks, so
    counting lines in the CSV overcounts.
    """
    cursor.execute(f"SELECT count(*) FROM {table} WHERE {run_column} = %s", (run_id,))
    rows = int(cursor.fetchall()[0][0])
    digest = hashlib.sha256()
    # mtime=0 keeps the gzip header free of the time, so equal data gives equal files.
    with path.open("wb") as raw, gzip.GzipFile(fileobj=raw, mode="wb", mtime=0) as handle:
        sql = (
            f"COPY (SELECT * FROM {table} WHERE {run_column} = %s ORDER BY {order_by}) "
            "TO STDOUT WITH (FORMAT csv, HEADER true)"
        )
        with cursor.copy(sql, (run_id,)) as copy:
            for chunk in copy:
                data = bytes(chunk)
                handle.write(data)
                digest.update(data)
    return {"file": path.name, "rows": rows, "csv_sha256": digest.hexdigest()}


def take_snapshot(run_dir: Path, run: Any, config: Any = None) -> dict[str, Any]:
    """Save one consolidation run's tables into the run directory and return the manifest.

    `run` is the consolidation_run.ConsolidationRun the seed reads. Written to a temporary
    directory and renamed into place, so a half-taken snapshot is never mistaken for a whole one.
    """
    import psycopg  # type: ignore

    from .db import DatabaseConfig, connect

    if not run.entity_linkage_run_id:
        raise RuntimeError(
            f"Consolidation run {run.run_id} records no entity_linkage_run_id, so the crosswalk it "
            "consumed cannot be identified. Pin another run with CONSOLIDATION_RUN_ID."
        )
    tables = {
        table: (CONSOLIDATION_RUN_COLUMN, run.run_id, order_by)
        for table, order_by in CONSOLIDATION_TABLES.items()
    }
    tables[CROSSWALK_TABLE] = (CROSSWALK_RUN_COLUMN, run.entity_linkage_run_id, CROSSWALK_ORDER)

    config = config or DatabaseConfig.from_env()
    target = snapshot_dir(run_dir)
    staging = target.with_name(f".{target.name}.tmp")
    shutil.rmtree(staging, ignore_errors=True)
    staging.mkdir(parents=True)

    try:
        with connect(config) as conn:
            conn.isolation_level = psycopg.IsolationLevel.REPEATABLE_READ
            conn.read_only = True
            with conn.cursor() as cursor:
                manifest = {
                    "taken_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
                    "database": f"{config.host}/{config.dbname}",
                    "consolidation_run": run.as_dict(),
                    "tables": {
                        table: copy_table(
                            cursor, table, column, value, order_by, staging / f"{table}.csv.gz"
                        )
                        for table, (column, value, order_by) in tables.items()
                    },
                }
        (staging / MANIFEST_NAME).write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
        shutil.rmtree(target, ignore_errors=True)
        os.replace(staging, target)
    except BaseException:
        shutil.rmtree(staging, ignore_errors=True)
        raise
    return manifest


def missing_snapshot_files(run_dir: Path) -> list[str] | None:
    """What a run's snapshot lacks; None when the run has no snapshot folder at all.

    The manifest is written, and moved through blob, after the tables, so a folder without one - or
    without a file it lists - is an interrupted copy or transfer, not a whole snapshot. The seed
    moves after the manifest (storage.transfer_stage), so a run that has its seed but no folder at
    all never had a snapshot: seeded from a file, or from before WP-548.
    """
    folder = snapshot_dir(run_dir)
    if not folder.exists():
        return None
    manifest_path = folder / MANIFEST_NAME
    if not manifest_path.exists():
        return [MANIFEST_NAME]
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    return [info["file"] for info in manifest["tables"].values() if not (folder / info["file"]).exists()]


def recorded_run(run_dir: Path) -> str:
    """Which consolidation run a run's seed read, as its snapshot manifest records it.

    Manifests written before a seed read one run list every run in the table instead, under
    `consolidation_run_ids`; more than one there means that seed blended them.
    """
    path = snapshot_dir(run_dir) / MANIFEST_NAME
    manifest = json.loads(path.read_text(encoding="utf-8")) if path.exists() else None
    return describe_manifest_run(manifest)


def describe_manifest_run(manifest: dict[str, Any] | None) -> str:
    """recorded_run for a manifest already read, e.g. from blob."""
    if not manifest:
        return "not recorded: seeded from a file, or before snapshots were kept"
    run = manifest.get("consolidation_run")
    if run:
        return (
            f"{run['run_id']} ({run['consolidation_strategy']}/{run['entity_linkage_strategy']}, "
            f"{run['chosen_by']})"
        )
    run_ids = manifest.get("consolidation_run_ids") or []
    if len(run_ids) == 1:
        return f"{run_ids[0]} (the only run in the table)"
    return f"a blend of {len(run_ids)} runs: {', '.join(run_ids)}"
