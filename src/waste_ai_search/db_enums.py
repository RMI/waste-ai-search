"""Categorical values generated from the live database.

DO NOT EDIT BY HAND. Regenerate with `uv run python scripts/generate_enums.py`.
Source: wastemap-database-dev.postgres.database.azure.com/postgres

Values come from Postgres enum types where one exists, and otherwise from the CHECK
constraint on consolidation.consolidated_facility. `waste_depth` is the second kind: it
is a text column with a constraint and no enum type.
"""
from __future__ import annotations


FACILITY_STATUS_VALUES: list[str] = [
    'Active',
    'Inactive',
]

FACILITY_TYPE_VALUES: list[str] = [
    'Sanitary Landfill',
    'Controlled Dumpsite',
    'Dumpsite',
    'Incineration Facility',
]

COVER_TYPE_VALUES: list[str] = [
    'clay cover',
    'organic cover',
    'sand cover',
    'other soil mixture',
]

GCCS_ENERGY_PROJECT_TYPE_VALUES: list[str] = [
    'Electricity Generation',
    'Direct Use',
    'Renewable Natural Gas',
    'Other',
]

GCCS_CURRENT_PROJECT_STATUS_VALUES: list[str] = [
    'Operational',
    'Under Construction',
    'Planned',
    'Closed',
]

# From the CHECK constraint on consolidated_facility.waste_depth.
WASTE_DEPTH_VALUES: list[str] = [
    '<=5m',
    '>5m',
]

DB_ENUMS: dict[str, list[str]] = {
    'facility_status': FACILITY_STATUS_VALUES,
    'facility_type': FACILITY_TYPE_VALUES,
    'cover_type': COVER_TYPE_VALUES,
    'gccs_energy_project_type': GCCS_ENERGY_PROJECT_TYPE_VALUES,
    'gccs_current_project_status': GCCS_CURRENT_PROJECT_STATUS_VALUES,
    'waste_depth': WASTE_DEPTH_VALUES,
}
