"""Which consolidation run a seed reads.

`consolidation.consolidated_facility` accumulates runs: every consolidation appends its full result
beside the earlier ones, and so do the value resolution ledger and `entity_linkage.crosswalk`. Read
whole, the table is a blend of runs - sites a newer run dropped come back, and one site's values
can come from different runs. So a seed reads exactly one run, picked the way the Climate TRACE
pipeline picks it (`resolve_consolidation_run` in RMI_Climate_TRACE_Waste_Methane), so that both
read the same sites:

- `CONSOLIDATION_RUN_ID`, when set, pins one run exactly;
- otherwise the newest run for `CONSOLIDATION_STRATEGY` and `ENTITY_LINKAGE_STRATEGY`, both
  `legacy_replica_v1` by default. The newest run overall can be a colleague's experiment against
  another crosswalk.

Every failure stops the run rather than falling back: reading the wrong run would silently change
which sites are searched.
"""
from __future__ import annotations

import os
from dataclasses import asdict, dataclass
from typing import Any, Mapping

from .schema import normalize_scalar


TABLE = "consolidation.consolidated_facility"
# The columns consolidation stamps on every row of a run, which RUNS_SQL reads.
RUN_COLUMNS = [
    "consolidation_run_id",
    "consolidation_strategy",
    "entity_linkage_strategy",
    "entity_linkage_run_id",
    "consolidation_created_date",
]
DEFAULT_CONSOLIDATION_STRATEGY = "legacy_replica_v1"
DEFAULT_ENTITY_LINKAGE_STRATEGY = "legacy_replica_v1"

# Newest first, by the date consolidation stamps on every row of a run; the id is a uuid4 and says
# nothing about time, so it only breaks ties. A run with no date sorts last rather than first.
RUNS_SQL = (
    "SELECT consolidation_run_id, consolidation_strategy, entity_linkage_strategy,\n"
    "       MAX(entity_linkage_run_id) AS entity_linkage_run_id,\n"
    "       MAX(consolidation_created_date) AS created,\n"
    "       count(*) AS row_count\n"
    f"FROM {TABLE}\n"
    "GROUP BY consolidation_run_id, consolidation_strategy, entity_linkage_strategy\n"
    "ORDER BY created DESC NULLS LAST, consolidation_run_id DESC"
)
ROW_COUNT_SQL = f"SELECT count(*) AS n FROM {TABLE} WHERE consolidation_run_id = %s"


@dataclass(frozen=True)
class ConsolidationRun:
    run_id: str
    consolidation_strategy: str
    entity_linkage_strategy: str
    # The crosswalk run this consolidation consumed, so the snapshot copies that crosswalk only.
    entity_linkage_run_id: str
    created: str
    rows: int
    chosen_by: str

    def describe(self) -> str:
        return (
            f"Consolidation run {self.run_id} ({self.consolidation_strategy}/"
            f"{self.entity_linkage_strategy}, created {self.created or 'at an unknown time'}, "
            f"{self.rows:,} rows): {self.chosen_by}."
        )

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def _run(row: Mapping[str, Any], chosen_by: str) -> ConsolidationRun:
    created = row.get("created")
    return ConsolidationRun(
        run_id=normalize_scalar(row.get("consolidation_run_id")),
        consolidation_strategy=normalize_scalar(row.get("consolidation_strategy")),
        entity_linkage_strategy=normalize_scalar(row.get("entity_linkage_strategy")),
        entity_linkage_run_id=normalize_scalar(row.get("entity_linkage_run_id")),
        created=created.isoformat() if hasattr(created, "isoformat") else normalize_scalar(created),
        rows=int(row.get("row_count") or 0),
        chosen_by=chosen_by,
    )


def choose_run(runs: list[Mapping[str, Any]], environ: Mapping[str, str] | None = None) -> ConsolidationRun:
    """Pick one run from RUNS_SQL's rows (newest first), honouring the environment's settings."""
    environ = os.environ if environ is None else environ
    if not runs:
        raise RuntimeError(
            f"{TABLE} is empty: nothing has been consolidated into this database yet, or the "
            "connection points at the wrong one."
        )

    pinned = (environ.get("CONSOLIDATION_RUN_ID") or "").strip()
    if pinned:
        match = [row for row in runs if normalize_scalar(row.get("consolidation_run_id")) == pinned]
        if not match:
            newest = ", ".join(normalize_scalar(row.get("consolidation_run_id")) for row in runs[:5])
            raise RuntimeError(
                f"CONSOLIDATION_RUN_ID={pinned!r} is not in {TABLE}. Newest runs there: {newest}."
            )
        return _run(match[0], "pinned by CONSOLIDATION_RUN_ID")

    strategy = (environ.get("CONSOLIDATION_STRATEGY") or DEFAULT_CONSOLIDATION_STRATEGY).strip()
    linkage = (environ.get("ENTITY_LINKAGE_STRATEGY") or DEFAULT_ENTITY_LINKAGE_STRATEGY).strip()
    eligible = [
        row
        for row in runs
        if normalize_scalar(row.get("consolidation_strategy")) == strategy
        and normalize_scalar(row.get("entity_linkage_strategy")) == linkage
    ]
    if not eligible:
        present = sorted(
            {
                f"{normalize_scalar(row.get('consolidation_strategy'))}/"
                f"{normalize_scalar(row.get('entity_linkage_strategy'))}"
                for row in runs
            }
        )
        raise RuntimeError(
            f"No consolidation run for strategy {strategy}/{linkage} in {TABLE}; it holds "
            f"{', '.join(present)}. Set CONSOLIDATION_STRATEGY and ENTITY_LINKAGE_STRATEGY, or pin "
            "CONSOLIDATION_RUN_ID."
        )
    return _run(
        eligible[0],
        f"the newest of {len(eligible)} {strategy}/{linkage} run(s), out of {len(runs)} in the table",
    )


def _load_env() -> None:
    try:
        from dotenv import load_dotenv  # type: ignore

        load_dotenv()
    except ImportError:
        pass


def resolve_run(config: Any = None) -> ConsolidationRun:
    """Choose the run a seed reads, from the database, and say which it is."""
    from .db import fetch_all

    _load_env()  # CONSOLIDATION_RUN_ID and the strategies may be set in .env
    run = choose_run(fetch_all(RUNS_SQL, config=config))
    print(run.describe())
    return run


def row_count(run_id: str, config: Any = None) -> int:
    """How many rows the run has now. Runs are appended whole and never edited, so a change means
    the tables were wiped or rebuilt."""
    from .db import fetch_one

    row = fetch_one(ROW_COUNT_SQL, (run_id,), config=config)
    return int(row["n"]) if row else 0
