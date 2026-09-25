from waste_ai_search.unit_converter import convert_attribute_value


def test_converts_kg_per_day_to_metric_tonnes_per_year():
    result = convert_attribute_value("annual_incoming_waste_metric_tonnes", "365000", "kg/day")

    assert result.value == "133225"
    assert result.unit == "metric tonnes/year"
    assert result.original_value == "365000"
    assert result.original_unit == "kg/day"
    assert "Converted from kg" in result.note
    assert "Annualized daily rate" in result.note
    assert result.warning == ""


def test_flags_ambiguous_tons_without_silent_conversion():
    result = convert_attribute_value("waste_in_place_metric_tonnes", "1000", "tons")

    assert result.value == "1000"
    assert result.unit == "tons"
    assert "Ambiguous unit" in result.warning


def test_preserves_metric_tonnes_per_year_range_in_canonical_unit():
    result = convert_attribute_value(
        "annual_incoming_waste_metric_tonnes",
        "3,100,000 to 4,000,000",
        "metric tonnes/year",
    )

    assert result.value == "3100000 to 4000000"
    assert result.unit == "metric tonnes/year"
    assert result.warning == ""


def test_metric_tonnes_stock_unit_is_not_mangled():
    result = convert_attribute_value("waste_in_place_metric_tonnes", "1,235,000", "metric tonnes")

    assert result.value == "1235000"
    assert result.unit == "metric tonnes"
    assert result.warning == ""



def test_converts_distance_miles_to_km():
    result = convert_attribute_value("distance_to_original_coordinates_km", "31.2", "miles")

    assert result.value == "50.211533"
    assert result.unit == "km"
    assert "Converted from miles" in result.note
    assert result.warning == ""
