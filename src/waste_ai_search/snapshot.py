"""Keep a copy of the consolidation tables a run searched against (F32).

The database is rebuilt by consolidation, and `internal_facility_id` - which every output of a
run uses as `site_id` - is reassigned each time. Without a copy, a finished run's results cannot
be tied back to their facilities once the tables move on, and there is no way to tell which
facilities changed and need searching again.

So a run that seeds from the database first saves, inside its run directory:

- `consolidated_facility` - the baseline the search filled gaps in;
- `value_resolution_ledger` - which source, and which record in it (`data_source_facility_id`),
  supplied each baseline value;
- every `entity_linkage.crosswalk_*` table - all source records linked to each facility, including
  those that supplied no winning value. `data_source` + `facility_id` is the key that survives a
  rebuild, so this is what maps a run's `site_id` onto a later consolidation.

All tables are read whole - every country, not just the ones searched - in one read-only
REPEATABLE READ transaction, so they describe the same consolidation state. Each is a gzipped CSV
in Postgres' own COPY format, with a manifest of row counts and checksums.
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

# Table -> ORDER BY, so the same database state always produces the same bytes and checksum.
CONSOLIDATION_TABLES = {
    "consolidation.consolidated_facility": "internal_facility_id, year",
    "consolidation.value_resolution_ledger": (
        "internal_facility_id, year NULLS FIRST, column_name, data_source, data_source_facility_id"
    ),
}
CROSSWALK_SCHEMA = "entity_linkage"
CROSSWALK_ORDER = "internal_facility_id, data_source, facility_id"


def snapshot_dir(run_dir: Path) -> Path:
    return Path(run_dir) / SNAPSHOT_DIR_NAME


def crosswalk_tables(cursor: Any) -> list[str]:
    """Every entity-linkage crosswalk, whichever strategy consolidation used this time."""
    cursor.execute(
        "SELECT table_name FROM information_schema.tables "
        "WHERE table_schema = %s AND table_name LIKE 'crosswalk\\_%%' ORDER BY table_name",
        (CROSSWALK_SCHEMA,),
    )
    return [f"{CROSSWALK_SCHEMA}.{row[0]}" for row in cursor.fetchall()]


def consolidation_run_ids(cursor: Any) -> list[str]:
    cursor.execute(
        "SELECT DISTINCT consolidation_run_id FROM consolidation.consolidated_facility ORDER BY 1"
    )
    return [str(row[0]) for row in cursor.fetchall()]


def copy_table(cursor: Any, table: str, order_by: str, path: Path) -> dict[str, Any]:
    """Stream one table to a gzipped CSV. Returns its row count and the checksum of the CSV.

    Rows are counted by the database in the same transaction: values can hold line breaks, so
    counting lines in the CSV overcounts.
    """
    cursor.execute(f"SELECT count(*) FROM {table}")
    rows = int(cursor.fetchall()[0][0])
    digest = hashlib.sha256()
    # mtime=0 keeps the gzip header free of the time, so equal data gives equal files.
    with path.open("wb") as raw, gzip.GzipFile(fileobj=raw, mode="wb", mtime=0) as handle:
        sql = f"COPY (SELECT * FROM {table} ORDER BY {order_by}) TO STDOUT WITH (FORMAT csv, HEADER true)"
        with cursor.copy(sql) as copy:
            for chunk in copy:
                data = bytes(chunk)
                handle.write(data)
                digest.update(data)
    return {"file": path.name, "rows": rows, "csv_sha256": digest.hexdigest()}


def take_snapshot(run_dir: Path, config: Any = None) -> dict[str, Any]:
    """Save the consolidation tables into the run directory and return the manifest.

    Written to a temporary directory and renamed into place, so a half-taken snapshot is never
    mistaken for a whole one.
    """
    import psycopg  # type: ignore

    from .db import DatabaseConfig, connect

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
                run_ids = consolidation_run_ids(cursor)
                tables = dict(CONSOLIDATION_TABLES)
                tables.update({name: CROSSWALK_ORDER for name in crosswalk_tables(cursor)})
                manifest = {
                    "taken_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
                    "database": f"{config.host}/{config.dbname}",
                    "consolidation_run_ids": run_ids,
                    "tables": {
                        table: copy_table(cursor, table, order_by, staging / f"{table}.csv.gz")
                        for table, order_by in tables.items()
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

    The manifest is written, and pushed to blob, last, so a folder without one - or without a file
    it lists - is an interrupted copy or upload, not a whole snapshot.
    """
    folder = snapshot_dir(run_dir)
    if not folder.exists():
        return None
    manifest_path = folder / MANIFEST_NAME
    if not manifest_path.exists():
        return [MANIFEST_NAME]
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    return [info["file"] for info in manifest["tables"].values() if not (folder / info["file"]).exists()]


def current_consolidation_run_ids(config: Any = None) -> list[str]:
    from .db import connect

    with connect(config, autocommit=True) as conn, conn.cursor() as cursor:
        return consolidation_run_ids(cursor)
