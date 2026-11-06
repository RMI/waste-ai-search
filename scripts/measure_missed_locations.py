#!/usr/bin/env python3
"""How often a cited page states an address or coordinates that the agent didn't copy.

Measures whether a second, page-reading pass is worth building (src/waste_ai_search/missed_locations.py
says how it looks). Reads an arbitrated run whose link check kept page text: evidence.csv,
sources.csv, seed.csv, link_check.json and page_text/. Prints a summary, and with --out writes the
missed pages, to spot-check before deciding anything.

Read-only: it writes nothing in the run folder.

Usage:
  uv run python scripts/measure_missed_locations.py --run-id batch2
  uv run python scripts/measure_missed_locations.py --run-dir outputs/runs/batch2 --out missed.csv

A run with no page text needs `uv run waste-ai-search check-links --run-id <run id>` first (network),
and then `arbitrate`.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    where = parser.add_mutually_exclusive_group(required=True)
    where.add_argument("--run-id", help="A run under outputs/runs/.")
    where.add_argument("--run-dir", help="A run folder on disk.")
    parser.add_argument("--out", default="", help="Write the missed pages to this CSV.")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    from waste_ai_search.missed_locations import measure_run, summary, write_missed
    from waste_ai_search.run_context import default_run_dir

    args = parse_args(argv)
    run_dir = Path(args.run_dir).expanduser().resolve() if args.run_dir else default_run_dir(args.run_id)
    measurement = measure_run(run_dir)
    print(summary(measurement))
    if args.out:
        count = write_missed(Path(args.out).expanduser(), measurement)
        print(f"\n{count:,} missed page(s) written to {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
