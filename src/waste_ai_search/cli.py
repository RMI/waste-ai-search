from __future__ import annotations

import argparse
import sys
from dataclasses import replace
from pathlib import Path

from .arbitrate import run_arbitration
from .run_context import PipelineConfig, default_run_dir, default_run_id
from .input_loader import refreshed_seed_path
from .search import run_search


def add_common(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--input-csv",
        default="",
        help="Seed CSV. Omit to seed straight from consolidation.consolidated_facility.",
    )
    parser.add_argument("--run-id", default="", help="Run identifier; also the output directory name.")
    parser.add_argument("--run-dir", default="", help="Override the output directory.")
    parser.add_argument("--dataset-version", default="waste_ai_search_v0.2")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="waste-ai-search",
        description="Waste site AI metadata search. Search caches responses; arbitrate is offline.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    search = subparsers.add_parser("search", help="Query Foundry and cache one raw response per site.")
    add_common(search)
    search.add_argument(
        "--limit", type=int, default=0, help="Search the first N sites in seed order (0 = all)."
    )
    search.add_argument(
        "--pilot-size",
        type=int,
        default=0,
        help="Sample N sites spread across countries and hard cases, instead of the first N.",
    )
    search.add_argument("--iso3", nargs="*", default=[], help="Restrict to these country codes.")
    search.add_argument(
        "--include-excluded-countries",
        action="store_true",
        help="Also search countries excluded by default (currently USA).",
    )
    search.add_argument("--site-ids", nargs="*", default=[], help="Search only these site_ids.")
    search.add_argument("--force", action="store_true", help="Re-query sites that already have a cached response.")
    search.add_argument("--site-delay-seconds", type=float, default=3.0)
    search.add_argument("--hard-site-timeout-seconds", type=int, default=240)
    search.add_argument(
        "--use-geocode-cache",
        action="store_true",
        help="Fill municipality/admin context from the geocode cache so the prompt can use it.",
    )
    search.add_argument(
        "--geocode-cache",
        default=str(Path.cwd() / "data" / "geocode_cache.jsonl"),
        help="Path to the geocode cache JSONL.",
    )
    search.add_argument(
        "--search-tool-retries",
        type=int,
        default=3,
        help="Attempts per site when the agent reports its own web-search tool failed.",
    )

    search.add_argument(
        "--no-link-check",
        action="store_true",
        help="Skip fetching cited URLs after the search (no link_check.json is written).",
    )

    arbitrate = subparsers.add_parser(
        "arbitrate",
        help="Rebuild all outputs from cached responses. Offline, free, and repeatable.",
    )
    add_common(arbitrate)
    arbitrate.add_argument(
        "--keep-broken-link-evidence",
        action="store_true",
        help="Let a source whose link is dead (404/410/redirect loop) stay promotable instead of "
        "routing it to leads.",
    )

    links = subparsers.add_parser(
        "check-links",
        help="Fetch a run's cited URLs and write link_check.json (network; no agent calls).",
    )
    add_common(links)

    full = subparsers.add_parser(
        "run",
        help="search -> arbitrate -> follow up on newly found gas collection -> arbitrate. One command.",
    )
    add_common(full)
    full.add_argument("--limit", type=int, default=0, help="Search the first N sites in seed order (0 = all).")
    full.add_argument("--pilot-size", type=int, default=0, help="Sample N sites spread across countries.")
    full.add_argument("--iso3", nargs="*", default=[], help="Restrict to these country codes.")
    full.add_argument("--site-ids", nargs="*", default=[], help="Search only these site_ids.")
    full.add_argument("--include-excluded-countries", action="store_true")
    full.add_argument("--force", action="store_true")
    full.add_argument("--site-delay-seconds", type=float, default=3.0)
    full.add_argument("--hard-site-timeout-seconds", type=int, default=240)
    full.add_argument("--search-tool-retries", type=int, default=3)
    full.add_argument("--use-geocode-cache", action="store_true")
    full.add_argument("--geocode-cache", default=str(Path.cwd() / "data" / "geocode_cache.jsonl"))
    full.add_argument(
        "--no-followup",
        action="store_true",
        help="Skip the gas-capture follow-up pass.",
    )
    full.add_argument("--no-link-check", action="store_true", help="Skip fetching cited URLs.")
    full.add_argument(
        "--keep-broken-link-evidence",
        action="store_true",
        help="Let a source with a dead link stay promotable instead of routing it to leads.",
    )

    refresh = subparsers.add_parser(
        "refresh-seed",
        help="Merge a run's promoted values into the seed so a second pass sees them.",
    )
    add_common(refresh)
    refresh.add_argument("--output-csv", default="", help="Refreshed seed path.")
    refresh.add_argument(
        "--merge-identity",
        action="store_true",
        help="Also overwrite seed site_name/latitude/longitude with promoted identity findings.",
    )

    return parser


def run_refresh_seed(config: PipelineConfig, output_csv: Path, merge_identity: bool) -> int:
    import csv

    from .input_loader import resolve_sites
    from .prompt_builder import requested_attributes
    from .seed_refresh import refresh_sites, write_refreshed_seed

    resolved_path = config.run_dir / "resolved.csv"
    if not resolved_path.exists():
        print(f"No resolved.csv in {config.run_dir}. Run arbitrate first.")
        return 1

    sites, headers = resolve_sites(config)
    with resolved_path.open(newline="", encoding="utf-8-sig") as handle:
        resolved_rows = [dict(row) for row in csv.DictReader(handle)]

    before = sum(len(requested_attributes(site)) for site in sites)
    refreshed, stats = refresh_sites(sites, resolved_rows, config.run_id, merge_identity=merge_identity)
    after = sum(len(requested_attributes(site)) for site in refreshed)

    write_refreshed_seed(output_csv, refreshed, headers)

    print(f"Facilities updated:         {stats['facilities_updated']}")
    print(f"Values merged:              {stats['values_merged']}")
    print(f"Gas collection discovered:  {stats['gas_collection_discovered']}")
    print(f"\nAttribute-requests for the next pass: {before:,} -> {after:,}")
    print(f"Wrote {output_csv}")
    return 0


def make_config(args: argparse.Namespace) -> PipelineConfig:
    run_id = args.run_id or default_run_id()
    run_dir = Path(args.run_dir).expanduser().resolve() if args.run_dir else default_run_dir(run_id)
    return PipelineConfig(
        input_csv=Path(args.input_csv).expanduser().resolve() if args.input_csv else None,
        run_dir=run_dir,
        run_id=run_id,
        dataset_version=args.dataset_version,
        limit=getattr(args, "limit", 0),
        pilot_size=getattr(args, "pilot_size", 0),
        include_excluded_countries=getattr(args, "include_excluded_countries", False),
        iso3=list(getattr(args, "iso3", []) or []),
        site_ids=list(getattr(args, "site_ids", []) or []),
        force=getattr(args, "force", False),
        site_delay_seconds=getattr(args, "site_delay_seconds", 3.0),
        hard_site_timeout_seconds=getattr(args, "hard_site_timeout_seconds", 240),
        search_tool_retries=getattr(args, "search_tool_retries", 3),
        geocode_cache=(
            Path(args.geocode_cache).expanduser().resolve() if getattr(args, "geocode_cache", "") else None
        ),
        use_geocode_cache=getattr(args, "use_geocode_cache", False),
        check_links=not getattr(args, "no_link_check", False),
        keep_broken_links=getattr(args, "keep_broken_link_evidence", False),
    )


def run_everything(config: PipelineConfig, followup: bool = True) -> int:
    """The whole loop in one command, in one run directory.

    Gas-capture attributes are only asked of facilities known to have a collection system, and
    `has_landfill_gas_collection` is itself searched. So a facility whose collection system is
    only discovered during pass 1 needs a second, narrow pass to pick up its GCCS attributes.
    Doing that by hand took three commands, a second run id and a second input file.
    """
    import csv

    from .arbitrate import run_arbitration
    from .input_loader import resolve_sites
    from .prompt_builder import has_gas_collection
    from .schema import GCCS_ATTRIBUTES, normalize_scalar
    from .search import run_search
    from .seed_refresh import refresh_sites, write_refreshed_seed

    print("== pass 1: search ==")
    run_search(config)
    print("\n== pass 1: arbitrate ==")
    run_arbitration(config)

    if not followup:
        return 0

    resolved_path = config.run_dir / "resolved.csv"
    with resolved_path.open(newline="", encoding="utf-8-sig") as handle:
        resolved_rows = [dict(row) for row in csv.DictReader(handle)]

    sites, headers = resolve_sites(config)
    before = {
        normalize_scalar(site.get("site_id")): has_gas_collection(site) for site in sites
    }
    refreshed, _stats = refresh_sites(sites, resolved_rows, config.run_id)

    unlocked = [
        normalize_scalar(site.get("site_id"))
        for site in refreshed
        if has_gas_collection(site) and not before.get(normalize_scalar(site.get("site_id")))
    ]
    if not unlocked:
        print("\n== no newly discovered gas collection; no follow-up needed ==")
        return 0

    print(f"\n== pass 2: {len(unlocked)} site(s) revealed a gas collection system ==")
    seed_path = write_refreshed_seed(refreshed_seed_path(config), refreshed, headers)
    followup_config = replace(
        config,
        input_csv=seed_path,
        site_ids=unlocked,
        iso3=[],
        limit=0,
        pilot_size=0,
    )
    run_search(followup_config, pass_label="gccs", only_attributes=sorted(GCCS_ATTRIBUTES))

    print("\n== pass 2: arbitrate ==")
    run_arbitration(config)
    return 0


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    config = make_config(args)

    if args.command == "search":
        run_search(config)
        print(f"\nNow arbitrate:\n  uv run waste-ai-search arbitrate --run-id {config.run_id}")
        return 0

    if args.command == "check-links":
        from .search import check_run_links

        check_run_links(config.run_dir)
        print(f"\nNow arbitrate to apply the verdicts:\n  uv run waste-ai-search arbitrate --run-id {config.run_id}")
        return 0

    if args.command == "run":
        return run_everything(config, followup=not args.no_followup)

    if args.command == "refresh-seed":
        output = (
            Path(args.output_csv).expanduser().resolve()
            if args.output_csv
            else refreshed_seed_path(config)
        )
        return run_refresh_seed(config, output, args.merge_identity)

    if args.command == "arbitrate":
        paths = run_arbitration(config)
        print("\nOutputs:")
        for name, path in paths.items():
            print(f"  {name:<14} {path}")
        return 0

    return 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
