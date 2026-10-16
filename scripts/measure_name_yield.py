#!/usr/bin/env python3
"""What finished search runs found for sites whose names can't be searched.

Step 1 of docs/plans/2026-10-15-skip-unsearchable-names.md. For each run it compares five classes
of site name (no name, not a name, generic label, mining feature, named) on what searching them found, within
sites that come only from OpenStreetMap or Global Plastic Watch and within the rest, and applies
the plan's bar. It also forecasts what each batch would search, by class, and what that would cost.

Read-only: run files are read from blob into memory (or from a run folder on disk), SME decisions
from the review workbook in the synced SharePoint folder, and the forecast from the database. It
writes only its own CSVs, to outputs/name_yield/.

Usage:
  uv run python scripts/measure_name_yield.py --list-sites         # every site each batch would skip
  uv run python scripts/measure_name_yield.py                      # list the runs in blob
  uv run python scripts/measure_name_yield.py --run-id batch1      # measure runs from blob
  uv run python scripts/measure_name_yield.py --run-dir outputs/runs/batch1
  uv run python scripts/measure_name_yield.py --run-id batch1 --no-forecast

Needs the VPN and `az login` for blob storage and the database, with both set in .env.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--run-id", nargs="*", default=[], help="Runs to measure, read from blob.")
    parser.add_argument("--run-dir", nargs="*", default=[], help="Run folders on disk to measure.")
    parser.add_argument(
        "--review-dir",
        default="",
        help="Folder holding <run_id>/<run_id>_review.xlsx with SME decisions "
        "(default: WASTE_AI_SEARCH_REVIEW_DIR).",
    )
    parser.add_argument(
        "--list-sites",
        action="store_true",
        help="List every site each batch would skip, and why, from today's database (no blob needed).",
    )
    parser.add_argument("--no-forecast", action="store_true", help="Skip the forecast, which reads the database.")
    parser.add_argument("--cost-per-1000-queries", type=float, default=None, help="Bing price used by the forecast.")
    parser.add_argument("--out-dir", default=str(ROOT / "outputs" / "name_yield"))
    return parser.parse_args(argv)


def blob_client():
    from waste_ai_search import storage

    if not storage.configured():
        raise SystemExit(
            "Blob storage isn't configured: set AZURE_STORAGE_ACCOUNT in .env (or pass --run-dir)."
        )
    try:
        return storage.container_client()
    except storage.StorageError as exc:
        raise SystemExit(f"error: {exc}") from exc


def main(argv: list[str] | None = None) -> int:
    from waste_ai_search import name_yield as ny
    from waste_ai_search.publish import review_dir

    args = parse_args(argv)
    out_dir = Path(args.out_dir)

    if args.list_sites:
        list_sites(out_dir)
        if not args.run_id and not args.run_dir:
            return 0

    if not args.run_id and not args.run_dir:
        runs = ny.list_blob_runs(blob_client())
        print("Runs in blob storage:" if runs else "No runs in blob storage.")
        for run_id in runs:
            print(f"  {run_id}")
        print("\nMeasure some with --run-id <id> [<id> ...].")
        return 0

    sources = []
    if args.run_id:
        client = blob_client()
        sources += [(run_id, ny.blob_reader(client, run_id)) for run_id in args.run_id]
    sources += [(Path(path).name, ny.local_reader(Path(path))) for path in args.run_dir]

    reviews = Path(args.review_dir).expanduser() if args.review_dir else review_dir()
    every_site = []
    summaries = []
    for run_id, read in sources:
        try:
            run = ny.load_run(run_id, read)
        except FileNotFoundError as exc:
            print(f"Skipped: {exc}")
            continue
        if reviews is not None:
            run.sme = ny.read_review_workbook(reviews / run_id / f"{run_id}_review.xlsx")
        print(f"\n{ny.describe_run(run)}")
        rows = ny.site_rows(run)
        summary = ny.summarize(rows)
        print(ny.format_summary(summary))
        ny.write_csv(out_dir / f"{run_id}_sites.csv", rows, ny.SITE_HEADERS)
        summaries += [{"run_id": run_id, **row} for row in summary]
        every_site += rows

    if not every_site:
        print("\nNothing to measure.")
        return 1
    if len(sources) > 1:
        pooled = ny.summarize(every_site)
        print("\nAll runs together:" + ny.format_summary(pooled))
        summaries += [{"run_id": "all", **row} for row in pooled]
    ny.write_csv(out_dir / "summary.csv", summaries, ["run_id", *ny.SUMMARY_HEADERS])
    print(
        f"\nBar: a class falls below it when its sites get a new value less than {ny.BAR:.0%} as "
        f"often as named sites from the same kind of source, or SMEs rejected most of its reviewed "
        f"values as the wrong facility. Fewer than {ny.MIN_SITES_TO_JUDGE} answered sites is too few "
        "to judge."
    )

    if not args.no_forecast:
        forecast(every_site, args, out_dir)
    print(f"\nWrote {out_dir}")
    return 0


def list_sites(out_dir: Path) -> None:
    from waste_ai_search import name_yield as ny
    from waste_ai_search.seed_source import load_seed_sites

    sites = load_seed_sites()  # one consolidation run, which it names
    selections = ny.batch_selections(sites)
    rows = ny.skip_candidates(selections)
    print("\nSites each batch would search, and how many have a name that could be skipped:")
    print(ny.format_skip_candidates(selections, rows))
    ny.write_csv(out_dir / "would_skip.csv", rows, ny.SKIP_HEADERS)
    print(f"\nEvery one, with the rule it tripped and a map link: {out_dir / 'would_skip.csv'}")


def forecast(every_site, args, out_dir: Path) -> None:
    from waste_ai_search import name_yield as ny

    print("\nForecast: what each batch would search today, by class of name.")
    try:
        from waste_ai_search.seed_source import load_seed_sites

        sites = load_seed_sites()  # one consolidation run, which it names
    except Exception as exc:  # noqa: BLE001 - the measurement above stands without it
        print(f"Skipped: could not read the database ({type(exc).__name__}: {str(exc).splitlines()[0]}).")
        return
    per_site, fallback = ny.queries_per_site(every_site)
    cost = args.cost_per_1000_queries or ny.DEFAULT_COST_PER_1000_QUERIES
    rows = ny.forecast(sites, per_site, fallback, cost)
    for batch in sorted({row["batch"] for row in rows}):
        parts = [
            f"{row['name_class']} {row['sites']:,} (~${row['est_cost_usd']:,.0f})"
            for row in rows
            if row["batch"] == batch
        ]
        print(f"  batch {batch}: " + ", ".join(parts))
    print(f"  At ${cost:g} per 1,000 Bing queries and the queries per site measured above.")
    ny.write_csv(out_dir / "forecast.csv", rows, ny.FORECAST_HEADERS)


if __name__ == "__main__":
    raise SystemExit(main())
