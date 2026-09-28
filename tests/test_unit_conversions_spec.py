"""Conversions the standardized facility spec mandates for the widened attribute set."""
import pytest

from waste_ai_search.unit_converter import convert_attribute_value as convert


def test_feet_to_meters():
    result = convert("waste_depth", "100", "feet")
    assert result.value == "30.48"
    assert result.unit == "meters"
    assert "feet to meters" in result.note


def test_meters_pass_through():
    assert convert("waste_depth", "30", "meters").value == "30"


def test_yards_to_meters():
    assert convert("waste_depth", "10", "yards").value == "9.144"


def test_unsupported_depth_unit_warns_rather_than_guessing():
    result = convert("waste_depth", "40", "fathoms")
    assert result.warning
    assert "fathoms" in result.warning


@pytest.mark.parametrize(
    "value,unit,expected",
    [("75", "%", "0.75"), ("85", "percent", "0.85"), ("0.75", "", "0.75"), ("1", "", "1")],
)
def test_efficiency_lands_in_zero_to_one(value, unit, expected):
    result = convert("gccs_collection_efficiency", value, unit)
    assert result.value == expected
    assert result.unit == "fraction"


def test_bare_number_above_one_is_read_as_a_percentage():
    result = convert("gccs_collection_efficiency", "68", "")
    assert result.value == "0.68"
    assert "interpreted as a percentage" in result.note


def test_efficiency_outside_range_is_rejected():
    assert convert("gccs_collection_efficiency", "-5", "").warning


def test_cubic_feet_ch4_to_metric_tonnes():
    """Spec: kg CH4 = ft3 CH4 x 0.0192."""
    result = convert("gccs_ch4_generated_metric_tonnes", "1000000", "cubic feet CH4")
    assert result.value == "19.2"
    assert result.unit == "metric tonnes CH4"


def test_cubic_metres_ch4_to_metric_tonnes():
    result = convert("gccs_ch4_collected_metric_tonnes", "1000", "m3 CH4")
    assert result.value == "0.679"


def test_mmcfd_matches_the_spec_formula():
    """metric tonnes CH4/year = MMCFD x 1e6 x 365 x fraction x 0.0192 / 1000."""
    result = convert("gccs_ch4_collected_metric_tonnes", "2.5", "MMCFD methane")
    assert float(result.value) == pytest.approx(2.5 * 1e6 * 365 * 1.0 * 0.0192 / 1000)
    assert "Annualized daily rate" in result.note


def test_scfm_is_annualized():
    result = convert("gccs_ch4_generated_metric_tonnes", "300", "scfm CH4")
    assert "per-minute" in result.note
    assert float(result.value) == pytest.approx(300 * 525600 * 0.0192 / 1000)


def test_landfill_gas_volume_is_refused_without_a_methane_fraction():
    """The spec formula needs a methane fraction; assuming one would fabricate data."""
    result = convert("gccs_ch4_flared_metric_tonnes", "500", "landfill gas m3/day")
    assert result.warning
    assert "methane fraction" in result.warning
    assert result.value == "500"


def test_methane_mass_units_still_work():
    assert convert("gccs_ch4_flared_metric_tonnes", "1200", "metric tonnes").value == "1200"
    assert float(convert("gccs_ch4_flared_metric_tonnes", "1200", "short tons").value) == pytest.approx(
        1200 * 0.90718474
    )


def test_daily_methane_mass_is_annualized():
    result = convert("gccs_ch4_flared_metric_tonnes", "10", "metric tonnes/day")
    assert result.value == "3650"


def test_square_feet_to_square_metres():
    """Spec: square meters = square feet x 0.092903."""
    result = convert("area_square_meters", "100000", "square feet")
    assert float(result.value) == pytest.approx(100000 * 0.092903)
    assert result.unit == "square meters"


def test_acres_to_square_metres():
    """Spec: square meters = acres x 4,046.856."""
    assert float(convert("area_square_meters", "25", "acres").value) == pytest.approx(25 * 4046.856)


@pytest.mark.parametrize("unit", ["sq ft", "sq. ft", "ft2", "ft²", "square feet"])
def test_square_foot_spellings(unit):
    assert float(convert("area_square_meters", "100000", unit).value) == pytest.approx(9290.3)


def test_hectares_and_square_kilometres():
    """Not in the spec's table, but how most non-US sources report a footprint."""
    assert float(convert("area_square_meters", "12.5", "hectares").value) == pytest.approx(125000)
    assert float(convert("area_square_meters", "0.5", "km2").value) == pytest.approx(500000)


def test_unsupported_area_unit_warns():
    assert convert("area_square_meters", "40", "fathoms squared").warning


@pytest.mark.parametrize(
    "attribute,value",
    [
        ("area_square_meters", "487065"),
        ("waste_depth", "30"),
        ("waste_in_place_metric_tonnes", "1235000"),
        ("annual_incoming_waste_metric_tonnes", "120000"),
        ("gccs_ch4_flared_metric_tonnes", "1200"),
    ],
)
def test_a_value_with_no_unit_is_not_read_as_its_own_unit(attribute, value):
    """Bare digits were being treated as the unit name, which blocked promotion entirely."""
    result = convert(attribute, value, "")
    assert result.warning == ""
    assert result.value == value
    assert "No source unit provided" in result.note
