from waste_ai_search.unit_converter import convert_attribute_value, parse_numbers


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


import pytest  # noqa: E402


@pytest.mark.parametrize(
    ("attribute", "value", "unit", "expected", "canonical"),
    [
        # As the agent wrote them in the Mexico tests (30 October 2026), each dropped as unsupported.
        ("waste_in_place_metric_tonnes", "2500000", "toneladas", "2500000", "metric tonnes"),
        ("area_square_meters", "280000", "metros cuadrados", "280000", "square meters"),
        ("area_square_meters", "28", "Hectáreas", "280000", "square meters"),
        # Other ways Spanish sources write them.
        ("waste_in_place_metric_tonnes", "2500000", "toneladas métricas", "2500000", "metric tonnes"),
        ("area_square_meters", "1.5", "kilómetros cuadrados", "1500000", "square meters"),
        ("annual_incoming_waste_metric_tonnes", "1300", "toneladas por día", "474500", "metric tonnes/year"),
        ("annual_incoming_waste_metric_tonnes", "1300", "toneladas diarias", "474500", "metric tonnes/year"),
        ("annual_incoming_waste_metric_tonnes", "1300", "ton/día", "474500", "metric tonnes/year"),
        ("annual_incoming_waste_metric_tonnes", "474500", "toneladas al año", "474500", "metric tonnes/year"),
        ("annual_incoming_waste_metric_tonnes", "40000", "toneladas mensuales", "480000", "metric tonnes/year"),
        ("waste_depth", "15", "metros", "15", "meters"),
        # Batches 4 and 5: German, Italian and French.
        ("annual_incoming_waste_metric_tonnes", "120000", "Tonnen pro Jahr", "120000", "metric tonnes/year"),
        ("annual_incoming_waste_metric_tonnes", "120000", "t/a", "120000", "metric tonnes/year"),
        ("annual_incoming_waste_metric_tonnes", "120000", "tonnellate all'anno", "120000", "metric tonnes/year"),
        ("annual_incoming_waste_metric_tonnes", "300", "tonnes par jour", "109500", "metric tonnes/year"),
        ("area_square_meters", "12", "ettari", "120000", "square meters"),
        ("area_square_meters", "12", "Hektar", "120000", "square meters"),
        ("area_square_meters", "5000", "mètres carrés", "5000", "square meters"),
        ("area_square_meters", "5000", "Quadratmeter", "5000", "square meters"),
    ],
)
def test_units_written_in_other_languages_are_read(attribute, value, unit, expected, canonical):
    result = convert_attribute_value(attribute, value, unit)
    assert (result.value, result.unit, result.warning) == (expected, canonical, "")


def test_an_english_ton_stays_ambiguous_but_a_spanish_one_is_the_metric_tonne():
    """Mexican sources write "ton/día" for toneladas; an English "tons" may be short tons."""
    assert "Ambiguous unit" in convert_attribute_value("annual_incoming_waste_metric_tonnes", "1300", "tons/day").warning
    assert convert_attribute_value("annual_incoming_waste_metric_tonnes", "1300", "ton/día").value == "474500"


@pytest.mark.parametrize(
    ("attribute", "value", "unit", "expected"),
    [
        # 7482 in the 1 November retest: "1.4 millones de toneladas" loaded as 1.4 t.
        ("waste_in_place_metric_tonnes", "1.4", "millones de toneladas", "1400000"),
        ("waste_in_place_metric_tonnes", "1.4 millones", "toneladas", "1400000"),
        ("waste_in_place_metric_tonnes", "1", "millón de toneladas", "1000000"),
        ("waste_in_place_metric_tonnes", "800", "mil toneladas", "800000"),
        ("waste_in_place_metric_tonnes", "2", "mil millones de toneladas", "2000000000"),
        ("waste_in_place_metric_tonnes", "1.2", "milhões de toneladas", "1200000"),
        ("waste_in_place_metric_tonnes", "3", "Millionen Tonnen", "3000000"),
        ("area_square_meters", "1.5", "millones de metros cuadrados", "1500000"),
        # Not a magnitude: millimetres and miles only start with "mil".
        ("waste_in_place_metric_tonnes", "5000", "toneladas", "5000"),
    ],
)
def test_a_magnitude_written_in_another_language_is_applied(attribute, value, unit, expected):
    assert convert_attribute_value(attribute, value, unit).value == expected


def test_millimetres_and_miles_are_not_read_as_thousands():
    assert parse_numbers("12", "milímetros") == ([12.0], 1.0)
    assert parse_numbers("3", "millas") == ([3.0], 1.0)
    assert parse_numbers("31.2", "miles") == ([31.2], 1.0)
