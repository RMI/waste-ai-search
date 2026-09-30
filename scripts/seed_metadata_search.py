#!/usr/bin/env python3
"""Seed the metadata search input CSV from consolidation.consolidated_facility.

One row per physical facility, keyed on internal_facility_id.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output-csv",
        default=str(ROOT / "outputs" / "consolidated_sites.csv"),
        help="Destination seed CSV.",
    )
    parser.add_argument(
        "--iso3",
        nargs="*",
        default=None,
        help="Restrict to these iso3c_plus codes (e.g. --iso3 NGA PHL).",
    )
    parser.add_argument(
        "--require-coordinates",
        action="store_true",
        help="Drop facilities with no latitude/longitude.",
    )
    parser.add_argument(
        "--no-backfill",
        action="store_true",
        help="Keep the latest year verbatim instead of backfilling empty baseline fields.",
    )
    parser.add_argument("--limit", type=int, default=0, help="Write at most N facilities (0 = all).")
    return parser.parse_args()


def main() -> int:
    from waste_ai_search.db import DatabaseConfig
    from waste_ai_search.input_loader import write_csv_records
    from waste_ai_search.seed_source import load_seed_sites, seed_headers

    args = parse_args()
    config = DatabaseConfig.from_env()
    try:
        config.validate()
    except ValueError as exc:
        print(f"Configuration error: {exc}")
        return 1

    print("Source:", config.describe())
    try:
        records = load_seed_sites(
            config=config,
            iso3=args.iso3,
            require_coordinates=args.require_coordinates,
            backfill=not args.no_backfill,
        )
    except Exception as exc:
        print(f"Seed load failed: {type(exc).__name__}: {exc}")
        return 1

    if not records:
        print("No facilities matched the filters; nothing written.")
        return 1

    if args.limit > 0:
        records = records[: args.limit]

    output = Path(args.output_csv).expanduser().resolve()
    write_csv_records(output, records, seed_headers())

    total_rows = sum(int(record["source_row_count"] or 0) for record in records)
    collapsed = sum(1 for record in records if int(record["source_row_count"] or 0) > 1)
    backfilled = sum(1 for record in records if record["backfilled_fields"])
    print(f"\nFacilities written: {len(records)}")
    print(f"Source rows collapsed: {total_rows} ({collapsed} facilities had multiple year rows)")
    print(f"Facilities with backfilled baseline fields: {backfilled}")
    print(f"With coordinates: {sum(1 for r in records if r['latitude'] and r['longitude'])}")
    for field in ("facility_status", "facility_type", "annual_incoming_waste_metric_tonnes", "waste_in_place_metric_tonnes"):
        print(f"  baseline {field}: {sum(1 for r in records if r[field])}")
    print(f"\nWrote {output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
