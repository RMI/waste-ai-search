"""`waste_depth` became a category derived from a numeric measurement (upstream 8c0bb3fe).

The spec keeps the numeric conversion standards and adds a binning step on top: convert the
reported depth to metres, then assign '<=5m' or '>5m'. A reported depth of zero or less is not a
measurement, so it is left NULL rather than assigned to a category.

These cover the binning itself and the arbitration path that feeds it, because the two halves
fail differently: binning the raw value instead of the converted one would silently mis-bin every
source that reports feet.
"""
from datetime import date

import pytest

from waste_ai_search.arbitrate import extract_evidence
from waste_ai_search.schema import (
    STANDARDIZED_FACILITY_COLUMNS,
    WASTE_DEPTH_VALUES,
    bucket_waste_depth,
)


# Matches the Tier 1 fixture in test_arbitrate.py: `source_type` must be a member of
# credibility.SOURCE_TYPES, or the source is tiered too low to be promotable and these tests
# would pass or fail for a reason unrelated to depth.
REGULATOR = {
    "source_title": "Annual waste statistics",
    "publisher": "Waste Authority",
    "publication_date": "2022-12-31",
    "url": "https://example.org/waste-statistics",
    "language": "English",
    "source_type": "Regulator",
}


def _extract(value, unit):
    site = {
        "site_id": "1",
        "internal_facility_id": "1",
        "site_name": "Test Landfill",
        "country_iso3": "NGA",
        "reference_year": "2022",
    }
    payload = {
        "site_id": "1",
        "attributes": [
            {
                "attribute_name": "waste_depth",
                "value": value,
                "unit": unit,
                "value_basis": "Direct",
                "confidence_score": "High",
                "value_date": "2022",
                "evidence_summary": "summary",
                "sources": [REGULATOR],
            }
        ],
    }
    return extract_evidence(site, payload, "run", "v", date(2026, 1, 1))


# --- the binning rule ---------------------------------------------------------------------
@pytest.mark.parametrize(
    "meters,expected",
    [
        ("1", "<=5m"),
        ("4.9", "<=5m"),
        ("5", "<=5m"),      # the boundary itself is inclusive, per the '<=5m' label
        ("5.0001", ">5m"),
        ("34.4", ">5m"),    # the spec's own worked example
    ],
)
def test_depth_bins_on_the_five_metre_boundary(meters, expected):
    assert bucket_waste_depth(meters)[0] == expected


@pytest.mark.parametrize("meters", ["0", "0.0", "-3"])
def test_zero_or_negative_depth_is_null_not_a_category(meters):
    """The spec: a reported depth of zero or less is not a measurement."""
    value, note = bucket_waste_depth(meters)
    assert value is None
    assert "not a measurement" in note


def test_blank_depth_produces_no_note():
    assert bucket_waste_depth("") == (None, "")


def test_non_numeric_depth_is_null():
    value, note = bucket_waste_depth("shallow")
    assert value is None
    assert "not numeric" in note


def test_range_straddling_the_boundary_does_not_determine_a_category():
    """The converter emits "3 to 8" for a reported range; that is not one category."""
    value, note = bucket_waste_depth("3 to 8")
    assert value is None
    assert "straddles" in note


def test_range_within_one_band_still_bins():
    assert bucket_waste_depth("12 to 18")[0] == ">5m"


def test_only_spec_values_are_ever_produced():
    produced = {bucket_waste_depth(m)[0] for m in ["1", "5", "6", "100"]}
    assert produced <= set(WASTE_DEPTH_VALUES)


# --- the arbitration path -----------------------------------------------------------------
def test_feet_are_converted_before_binning():
    """20 feet is 6.096 m, so it is '>5m' -- binning the raw 20 would be right by luck.

    10 feet is 3.048 m and bins the other way, which is the case that actually catches a
    pipeline binning the unconverted number.
    """
    _by_attr, rows, _sources, _warn = _extract("10", "feet")
    row = rows[0]

    assert row["claimed_value"] == "10"
    assert row["claimed_unit"] == "feet"
    assert row["normalized_value"] == "3.048"        # metres kept as the evidence
    assert row["normalized_unit"] == "meters"
    assert row["mapped_value"] == "<=5m"             # the category that lands in the column
    assert row["promotion_eligible"] == "TRUE"


def test_metres_bin_without_conversion():
    _by_attr, rows, _sources, _warn = _extract("34.4", "meters")
    assert rows[0]["mapped_value"] == ">5m"
    assert rows[0]["promotion_eligible"] == "TRUE"


def test_zero_depth_is_not_promotable():
    _by_attr, rows, _sources, _warn = _extract("0", "meters")
    assert rows[0]["mapped_value"] == ""
    assert rows[0]["promotion_eligible"] == "FALSE"


def test_unconvertible_unit_is_not_binned_or_promoted():
    """A failed conversion leaves the value in its original unit; binning it would be a guess."""
    _by_attr, rows, _sources, _warn = _extract("40", "fathoms")
    assert rows[0]["mapped_value"] == ""
    assert rows[0]["promotion_eligible"] == "FALSE"


def test_the_column_is_the_renamed_one():
    assert "waste_depth" in STANDARDIZED_FACILITY_COLUMNS
    assert "waste_depth_meters" not in STANDARDIZED_FACILITY_COLUMNS
