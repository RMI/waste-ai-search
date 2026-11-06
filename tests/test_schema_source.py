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
SPEC = ROOT / "schema" / "StandardizedFacilityTableSpecification.md"
PROVENANCE = ROOT / "schema" / "SCHEMA_SOURCE.json"


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
        "rather than editing schema/ directly."
    )


def test_spec_columns_match_the_code(provenance: dict) -> None:
    """Guard the binding itself: every spec column must exist in STANDARDIZED_FACILITY_COLUMNS."""
    from waste_ai_search.schema import STANDARDIZED_FACILITY_COLUMNS

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
        "Update STANDARDIZED_FACILITY_COLUMNS and the attribute bindings in schema.py."
    )

    # `point` is a real column on all 11 transformed.* tables that the spec does not document;
    # it is derived from latitude/longitude. It is the ONLY accepted extra, so that
    # a genuinely invented column cannot slip in beside it.
    extra = [c for c in STANDARDIZED_FACILITY_COLUMNS if c not in in_spec]
    assert extra == ["point"], (
        f"Code declares columns the spec does not define: {extra}. Only 'point' is an accepted "
        "deviation; anything else must be added to the spec upstream first."
    )


def test_column_set_matches_the_spec() -> None:
    """43 spec columns plus `point`."""
    from waste_ai_search.schema import STANDARDIZED_FACILITY_COLUMNS

    assert len(STANDARDIZED_FACILITY_COLUMNS) == 44
    assert STANDARDIZED_FACILITY_COLUMNS[0] == "facility_id"
    assert STANDARDIZED_FACILITY_COLUMNS[-3:] == ["data_source", "created_date", "git_sha"]
    # Grouped with the other cover columns, where the spec puts it.
    assert STANDARDIZED_FACILITY_COLUMNS.index("has_biocover") == (
        STANDARDIZED_FACILITY_COLUMNS.index("cover_types") + 1
    )


def test_spec_column_order_is_preserved() -> None:
    """The code lists the spec's columns in the spec's order, so the two read side by side."""
    from waste_ai_search.schema import STANDARDIZED_FACILITY_COLUMNS

    in_spec = []
    for line in SPEC.read_text().splitlines():
        if line.startswith("| `") and line.count("|") == 5:
            name = line.split("`")[1]
            if name not in in_spec:
                in_spec.append(name)

    assert in_spec == [c for c in STANDARDIZED_FACILITY_COLUMNS if c in in_spec]
