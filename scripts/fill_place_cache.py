#!/usr/bin/env python3
"""Look up, ahead of a batch, the towns its prompts will name, slowly enough that Nominatim answers.

Each prompt names our site's municipality and state, and our other sites within 60 km of it, all
from data/geocode_cache.jsonl, and a search looks up whatever the cache lacks first, at Nominatim's
one request a second. In Germany 47 sites have 578 such neighbours, and two batch 4 practice runs
looking them up at once had Nominatim refuse every request for over an hour (16 November 2026), so
the run stopped before it searched anything. This fills the cache beforehand, one request every
2 seconds by default, pausing up to 15 minutes when refused. It searches nothing and costs nothing,
and stopped, it resumes where it left off.

Usage:
  uv run python scripts/fill_place_cache.py --batch 4 --input-csv outputs/runs/batch4-seed/seed.csv
  uv run python scripts/fill_place_cache.py --site-ids 7923 12313 --input-csv <seed.csv>
"""
from __future__ import annotations

import argparse
import contextlib
import io
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))


def main(argv: list[str] | None = None) -> int:
    from waste_ai_search.geocoder import look_up_places, with_other_sites
    from waste_ai_search.input_loader import load_sites
    from waste_ai_search.locate import NominatimGeocoder
    from waste_ai_search.run_context import PipelineConfig
    from waste_ai_search.search import choose_sites

    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--input-csv", required=True, type=Path, help="The seed the batch will search.")
    parser.add_argument("--batch", type=int, default=0, help="The country batch, selected as `search --batch` would.")
    parser.add_argument("--site-ids", nargs="*", default=[], help="Or these sites.")
    parser.add_argument("--cache", type=Path, default=ROOT / "data" / "geocode_cache.jsonl")
    parser.add_argument("--interval", type=float, default=2.0, help="Seconds between requests (Nominatim allows 1).")
    args = parser.parse_args(argv)
    if not args.batch and not args.site_ids:
        parser.error("give --batch or --site-ids")

    sites, _headers = load_sites(args.input_csv)
    config = PipelineConfig(
        input_csv=args.input_csv, run_dir=ROOT / "outputs" / "runs" / "_fill_place_cache", run_id="fill",
        batch=args.batch, site_ids=list(args.site_ids),
    )
    with contextlib.redirect_stdout(io.StringIO()):
        selected, _skipped = choose_sites(sites, config)
    print(f"{len(selected):,} site(s) to place.")

    geocoder = NominatimGeocoder.from_env()
    geocoder.min_interval = args.interval
    geocoder.RETRY_PAUSES = (60.0, 300.0, 900.0)
    selected = look_up_places(selected, args.cache, geocoder)
    with_other_sites(selected, sites, args.cache, geocoder)
    print(f"Done: {args.cache}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
