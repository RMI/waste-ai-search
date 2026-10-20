#!/usr/bin/env python3
"""Re-key a pre-wastemap_id run's resolved.csv and review workbook to wastemap_id.

Batch 1 ran before facilities had a permanent ID, so its site_ids are internal_facility_ids that
no longer name a facility. This writes copies of its two loadable files - resolved.csv and
<run_id>_review.xlsx - keyed by wastemap_id, into a new folder, plus relabel_report.csv with every
old ID's outcome. An ID that maps to no wastemap_id, to more than one, or to one another of the
run's IDs also maps to, stops it: only the report is written.

Today's crosswalk comes from the database (--from-db, on the VPN) or from an export (--crosswalk-csv).

Usage:
  uv run python scripts/relabel_to_wastemap_id.py RUN_DIR --from-db
  uv run python scripts/relabel_to_wastemap_id.py RUN_DIR --from-db --workbook ~/Downloads/batch1_review.xlsx
  uv run python scripts/relabel_to_wastemap_id.py RUN_DIR --crosswalk-csv crosswalk.csv --dry-run
Exit:   0 written (or a clean dry run), 1 stopped, 2 could not read the crosswalk from the database
"""
from __future__ import annotations

import argparse
import sys
from collections import Counter
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("run_dir", help="The run's folder, e.g. batch1/ from batch1.zip. Read only.")
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--from-db", action="store_true", help="Read entity_linkage.crosswalk from the database.")
    source.add_argument("--crosswalk-csv", help="Read an export of entity_linkage.crosswalk (one run) instead.")
    parser.add_argument(
        "--workbook",
        help="The review workbook to re-key. Defaults to the run's own <run_id>_review.xlsx; pass the "
        "SMEs' copy downloaded from SharePoint to carry their decisions across.",
    )
    parser.add_argument("--out-dir", help="Where the copies go. Defaults to RUN_DIR/wastemap_id/.")
    parser.add_argument(
        "--consolidation-run-id",
        default="",
        help="With --from-db: the consolidation run whose crosswalk to read. Defaults to "
        "CONSOLIDATION_RUN_ID, else the newest legacy_replica_v1 run - the one a seed reads.",
    )
    parser.add_argument(
        "--snapshot-crosswalk",
        help="The run's own crosswalk. Defaults to RUN_DIR/consolidation_snapshot/"
        "entity_linkage.crosswalk_legacy_replica_v1.csv.gz.",
    )
    parser.add_argument("--dry-run", action="store_true", help="Write only the report.")
    return parser.parse_args()


def main() -> int:
    from waste_ai_search.relabel import (
        STOPPING_OUTCOMES,
        RelabelError,
        read_crosswalk_csv,
        read_crosswalk_db,
        relabel_run,
    )

    args = parse_args()
    try:
        if args.from_db:
            from waste_ai_search.db import DatabaseConfig

            config = DatabaseConfig.from_env()
            try:
                config.validate()
                print("Database:", config.describe())
                crosswalk = read_crosswalk_db(config, args.consolidation_run_id)
            except RelabelError:
                raise
            except Exception as exc:
                print(f"Could not read the crosswalk from the database: {type(exc).__name__}: {exc}")
                print("Connect to the VPN and check WASTEMAP_DB_* in .env, or export it and pass --crosswalk-csv.")
                return 2
        else:
            crosswalk = read_crosswalk_csv(Path(args.crosswalk_csv).expanduser())
        print(f"Crosswalk: {crosswalk.description}; {len(crosswalk.wastemap_ids):,} source records.")

        result = relabel_run(
            Path(args.run_dir),
            crosswalk,
            out_dir=args.out_dir and Path(args.out_dir),
            workbook_path=args.workbook and Path(args.workbook),
            snapshot_crosswalk_path=args.snapshot_crosswalk and Path(args.snapshot_crosswalk),
            dry_run=args.dry_run,
        )
    except RelabelError as exc:
        print(f"Stopped: {exc}")
        return 1

    outcomes = Counter(mapping.outcome for mapping in result.mappings.values())
    print(f"{len(result.mappings):,} old IDs: " + ", ".join(f"{count:,} {outcome}" for outcome, count in sorted(outcomes.items())))
    print(f"Report: {result.report}")
    if result.stopped:
        print(f"Stopped: {len(result.stopped)} old ID(s) are {'/'.join(STOPPING_OUTCOMES)}, so nothing was re-keyed.")
        for mapping in result.stopped[:20]:
            print(f"  {mapping.old_id}: {mapping.outcome} - {mapping.detail}")
        if len(result.stopped) > 20:
            print(f"  ... and {len(result.stopped) - 20} more in the report.")
        return 1
    if args.dry_run:
        print("Dry run: every ID maps cleanly; nothing else written.")
        return 0
    for path in result.written:
        print(f"Wrote {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
