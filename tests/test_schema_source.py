"""The vendored spec must stay byte-identical to the upstream commit it is pinned to.

These tests are offline. They cannot tell you that upstream has moved on — that is
`scripts/sync_schema.py --check`, which needs network and a token. What they catch is
the spec being edited in place here, which would silently decouple this repository's
column bindings from the table the ETL actually writes.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
SPEC = ROOT / "inputs" / "StandardizedFacilityTableSpecification.md"
PROVENANCE = ROOT / "inputs" / "SCHEMA_SOURCE.json"


@pytest.fixture(scope="module")
def provenance() -> dict:
    assert PROVENANCE.exists(), f"{PROVENANCE.name} is missing; run scripts/sync_schema.py"
    return json.loads(PROVENANCE.read_text())


def test_provenance_records_the_upstream_source(provenance: dict) -> None:
    assert provenance["repo"] == "RMI/waste_data_ingestion_pipeline"
    assert provenance["path"] == "facility_etl/StandardizedFacilityTableSpecification.md"
    # A 40-character SHA, not a branch name: a pin has to name one immutable commit.
    assert len(provenance["ref"]) == 40, "ref must be a full commit SHA, not a branch"


def test_vendored_spec_matches_its_pin(provenance: dict) -> None:
    actual = hashlib.sha256(SPEC.read_text().encode("utf-8")).hexdigest()
    assert actual == provenance["sha256"], (
        "The vendored spec no longer matches the commit it is pinned to. It is owned by "
        "the upstream ETL repo -- change it there and re-run scripts/sync_schema.py "
        "rather than editing inputs/ directly."
    )


def test_spec_columns_match_the_code(provenance: dict) -> None:
    """Guard the binding itself: every spec column must exist in STANDARDIZED_FACILITY_COLUMNS."""
    from waste_ai_search.standardized import STANDARDIZED_FACILITY_COLUMNS

    # Column rows carry at least 4 pipes (name/type/range/description). The enum value
    # tables are single-column and carry 2, so this count alone separates them -- names
    # like `waste_depth` that are both an enum type and a column stay in scope.
    in_spec: list[str] = []
    for line in SPEC.read_text().splitlines():
        if line.startswith("| `") and line.count("|") >= 4:
            name = line.split("`")[1]
            if name.islower() and name not in in_spec and " " not in name:
                in_spec.append(name)

    declared = set(STANDARDIZED_FACILITY_COLUMNS)
    missing = [c for c in in_spec if c not in declared]

    assert not missing, (
        f"Spec defines columns the code does not declare: {missing}. "
        "Update STANDARDIZED_FACILITY_COLUMNS and the code that populates it."
    )
