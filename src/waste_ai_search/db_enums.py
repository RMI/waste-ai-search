"""Categorical values generated from the Postgres enum types.

DO NOT EDIT BY HAND. Regenerate with `uv run python scripts/generate_enums.py`.
Source: wastemap-database-dev.postgres.database.azure.com/postgres
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

DB_ENUMS: dict[str, list[str]] = {
    'facility_status': FACILITY_STATUS_VALUES,
    'facility_type': FACILITY_TYPE_VALUES,
    'cover_type': COVER_TYPE_VALUES,
    'gccs_energy_project_type': GCCS_ENERGY_PROJECT_TYPE_VALUES,
    'gccs_current_project_status': GCCS_CURRENT_PROJECT_STATUS_VALUES,
}
