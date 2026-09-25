from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from .schema import FRACTION_TARGET_ATTRIBUTES, normalize_scalar


MASS_TARGET_ATTRIBUTES = {
    "annual_incoming_waste_metric_tonnes": "rate",
    "waste_in_place_metric_tonnes": "mass",
    # GCCS quantities are annual totals scoped by the record's `year`, so a daily or monthly
    # source rate annualizes the same way an incoming-waste rate does.
    "gccs_ch4_flared_metric_tonnes": "rate",
    "gccs_ch4_generated_metric_tonnes": "rate",
    "gccs_ch4_collected_metric_tonnes": "rate",
    "gccs_ch4_flow_to_project_metric_tonnes": "rate",
}

# Methane quantities may be reported as a gas volume, which only converts because we know the gas.
CH4_MASS_ATTRIBUTES = {
    "gccs_ch4_flared_metric_tonnes",
    "gccs_ch4_generated_metric_tonnes",
    "gccs_ch4_collected_metric_tonnes",
    "gccs_ch4_flow_to_project_metric_tonnes",
}

# waste_depth still converts to metres here. The spec derives its category FROM a numeric
# measurement, so the metre value remains the evidence; schema.bucket_waste_depth bins it.
LENGTH_TARGET_ATTRIBUTES = {"waste_depth"}

AREA_TARGET_ATTRIBUTES = {"area_square_meters"}

# A unit has to carry a letter or a unit symbol; bare digits and punctuation are not a unit.
UNIT_WORD_PATTERN = re.compile(r"[a-z%°]")

# Spec: methane density 0.0192 kg/ft3 (0.679 kg/m3) at roughly 15 C and 1 atm.
CH4_KG_PER_CUBIC_FOOT = 0.0192
CH4_KG_PER_CUBIC_METRE = 0.679

# The spec stores latitude and longitude as separate numeric columns (Q23).
SINGLE_COORDINATE_ATTRIBUTES = {
    "found_latitude": (-90.0, 90.0),
    "found_longitude": (-180.0, 180.0),
}

DISTANCE_TARGET_ATTRIBUTES = {
    "distance_to_original_coordinates_km",
}

CANONICAL_UNITS = {
    "annual_incoming_waste_metric_tonnes": "metric tonnes/year",
    "waste_in_place_metric_tonnes": "metric tonnes",
    "found_latitude": "decimal degrees",
    "found_longitude": "decimal degrees",
    "waste_depth": "meters",
    "area_square_meters": "square meters",
    "gccs_ch4_flared_metric_tonnes": "metric tonnes CH4",
    "gccs_ch4_generated_metric_tonnes": "metric tonnes CH4",
    "gccs_ch4_collected_metric_tonnes": "metric tonnes CH4",
    "gccs_ch4_flow_to_project_metric_tonnes": "metric tonnes CH4",
    "gccs_collection_efficiency": "fraction",
    "distance_to_original_coordinates_km": "km",
}


@dataclass(frozen=True)
class ConversionResult:
    value: str
    unit: str
    original_value: str
    original_unit: str
    note: str = ""
    warning: str = ""


def convert_attribute_value(attribute_name: str, value: Any, unit: Any) -> ConversionResult:
    original_value = normalize_scalar(value)
    original_unit = normalize_scalar(unit)
    if attribute_name in SINGLE_COORDINATE_ATTRIBUTES:
        return convert_single_coordinate_value(attribute_name, original_value, original_unit)

    if attribute_name in DISTANCE_TARGET_ATTRIBUTES:
        return convert_distance_value(attribute_name, original_value, original_unit)

    if attribute_name in LENGTH_TARGET_ATTRIBUTES:
        return convert_length_value(attribute_name, original_value, original_unit)

    if attribute_name in AREA_TARGET_ATTRIBUTES:
        return convert_area_value(attribute_name, original_value, original_unit)

    if attribute_name in FRACTION_TARGET_ATTRIBUTES:
        return convert_fraction_value(attribute_name, original_value, original_unit)

    if attribute_name not in MASS_TARGET_ATTRIBUTES or not original_value:
        return ConversionResult(
            value=original_value,
            unit=original_unit,
            original_value=original_value,
            original_unit=original_unit,
        )

    parsed_numbers, multiplier = parse_numbers(original_value)
    if not parsed_numbers:
        return ConversionResult(
            value=original_value,
            unit=original_unit,
            original_value=original_value,
            original_unit=original_unit,
            warning=f"Could not parse numeric value {original_value!r} for unit conversion.",
        )

    combined_unit = detect_unit_text(original_value, original_unit)
    if attribute_name in CH4_MASS_ATTRIBUTES:
        volume_result = convert_ch4_volume(attribute_name, original_value, original_unit, combined_unit)
        if volume_result is not None:
            return volume_result
    mass_factor, mass_note, mass_warning = mass_to_metric_tonnes_factor(combined_unit)
    if mass_factor is None:
        return ConversionResult(
            value=original_value,
            unit=original_unit,
            original_value=original_value,
            original_unit=original_unit,
            warning=mass_warning or f"Unsupported unit {combined_unit!r} for unit conversion.",
        )

    expected_kind = MASS_TARGET_ATTRIBUTES[attribute_name]
    time_factor, time_note, time_warning = annualization_factor(combined_unit, expected_kind)
    if time_factor is None:
        return ConversionResult(
            value=original_value,
            unit=original_unit,
            original_value=original_value,
            original_unit=original_unit,
            warning=time_warning,
        )

    converted_values = [number * multiplier * mass_factor * time_factor for number in parsed_numbers]
    canonical_unit = CANONICAL_UNITS[attribute_name]
    notes = [note for note in [mass_note, time_note] if note]
    if multiplier != 1:
        notes.insert(0, f"Applied value multiplier {format_number(multiplier)} from reported magnitude wording.")
    return ConversionResult(
        value=format_converted_values(converted_values),
        unit=canonical_unit,
        original_value=original_value,
        original_unit=original_unit,
        note=" ".join(notes),
    )


def convert_single_coordinate_value(attribute_name: str, original_value: str, original_unit: str) -> ConversionResult:
    """Convert one latitude or longitude to decimal degrees, range-checked per the spec."""
    if not original_value:
        return ConversionResult(
            value=original_value,
            unit=original_unit,
            original_value=original_value,
            original_unit=original_unit,
        )

    parsed = parse_coordinate(original_value)
    if parsed is None:
        return ConversionResult(
            value=original_value,
            unit=original_unit,
            original_value=original_value,
            original_unit=original_unit,
            warning=f"Could not parse coordinate {original_value!r} for {attribute_name}.",
        )

    low, high = SINGLE_COORDINATE_ATTRIBUTES[attribute_name]
    if parsed < low or parsed > high:
        return ConversionResult(
            value=original_value,
            unit=original_unit,
            original_value=original_value,
            original_unit=original_unit,
            warning=f"{attribute_name} value {parsed!r} is outside valid range {low} to {high}.",
        )

    return ConversionResult(
        value=format_coordinate(parsed),
        unit=CANONICAL_UNITS[attribute_name],
        original_value=original_value,
        original_unit=original_unit,
    )


def convert_distance_value(attribute_name: str, original_value: str, original_unit: str) -> ConversionResult:
    if not original_value:
        return ConversionResult(
            value=original_value,
            unit=original_unit,
            original_value=original_value,
            original_unit=original_unit,
        )

    parsed_numbers, multiplier = parse_numbers(original_value)
    if not parsed_numbers:
        return ConversionResult(
            value=original_value,
            unit=original_unit,
            original_value=original_value,
            original_unit=original_unit,
            warning=f"Could not parse numeric value {original_value!r} for distance conversion.",
        )

    combined_unit = detect_unit_text(original_value, original_unit)
    distance_factor, distance_note, distance_warning = distance_to_km_factor(combined_unit)
    if distance_factor is None:
        return ConversionResult(
            value=original_value,
            unit=original_unit,
            original_value=original_value,
            original_unit=original_unit,
            warning=distance_warning,
        )

    converted_values = [number * multiplier * distance_factor for number in parsed_numbers]
    notes = [distance_note] if distance_note else []
    if multiplier != 1:
        notes.insert(0, f"Applied value multiplier {format_number(multiplier)} from reported magnitude wording.")
    return ConversionResult(
        value=format_converted_values(converted_values),
        unit=CANONICAL_UNITS[attribute_name],
        original_value=original_value,
        original_unit=original_unit,
        note=" ".join(notes),
    )


def convert_area_value(attribute_name: str, original_value: str, original_unit: str) -> ConversionResult:
    """Footprint area to square metres.

    Spec: m2 = sq ft x 0.092903, m2 = acres x 4046.856. Hectares and km2 are not in the spec's
    table but are how most non-US sources report a landfill footprint, so they are handled too.
    """
    if not original_value:
        return ConversionResult(original_value, original_unit, original_value, original_unit)

    numbers, multiplier = parse_numbers(original_value)
    if not numbers:
        return ConversionResult(
            original_value, original_unit, original_value, original_unit,
            warning=f"Could not parse numeric value {original_value!r} for {attribute_name}.",
        )

    text = detect_unit_text(original_value, original_unit)
    if re.search(r"\b(ha|hectares?)\b", text):
        factor, note = 10_000.0, "Converted from hectares to square meters."
    elif re.search(r"(km2|km\^2|km²|square kilomet(?:er|re)s?|sq\.?\s*km)", text):
        factor, note = 1_000_000.0, "Converted from square kilometers to square meters."
    elif re.search(r"\bacres?\b", text):
        factor, note = 4046.856, "Converted from acres to square meters."
    elif re.search(r"(ft2|ft\^2|ft²|square f(?:ee|oo)t|sq\.?\s*(?:f(?:ee|oo)t|ft)\b)", text):
        factor, note = 0.092903, "Converted from square feet to square meters."
    elif re.search(r"(m2|m\^2|m²|square met(?:er|re)s?|sq\.?\s*m)", text) or not text:
        factor = 1.0
        note = "" if text else "No source unit provided; interpreted as square meters."
    else:
        return ConversionResult(
            original_value, original_unit, original_value, original_unit,
            warning=f"Unsupported area unit {text!r} for {attribute_name}.",
        )

    values = [number * multiplier * factor for number in numbers]
    return ConversionResult(
        value=format_converted_values(values),
        unit=CANONICAL_UNITS[attribute_name],
        original_value=original_value,
        original_unit=original_unit,
        note=note,
    )


def convert_length_value(attribute_name: str, original_value: str, original_unit: str) -> ConversionResult:
    """Waste depth to meters. Spec: meters = feet x 0.3048."""
    if not original_value:
        return ConversionResult(original_value, original_unit, original_value, original_unit)

    numbers, multiplier = parse_numbers(original_value)
    if not numbers:
        return ConversionResult(
            original_value, original_unit, original_value, original_unit,
            warning=f"Could not parse numeric value {original_value!r} for {attribute_name}.",
        )

    text = detect_unit_text(original_value, original_unit)
    if re.search(r"\b(ft|foot|feet)\b", text):
        factor, note = 0.3048, "Converted from feet to meters."
    elif re.search(r"\b(m|meter|meters|metre|metres)\b", text) or not text:
        factor = 1.0
        note = "" if text else "No source unit provided; interpreted as meters."
    elif re.search(r"\b(yd|yard|yards)\b", text):
        factor, note = 0.9144, "Converted from yards to meters."
    else:
        return ConversionResult(
            original_value, original_unit, original_value, original_unit,
            warning=f"Unsupported length unit {text!r} for {attribute_name}.",
        )

    values = [number * multiplier * factor for number in numbers]
    return ConversionResult(
        value=format_converted_values(values),
        unit=CANONICAL_UNITS[attribute_name],
        original_value=original_value,
        original_unit=original_unit,
        note=note,
    )


def convert_fraction_value(attribute_name: str, original_value: str, original_unit: str) -> ConversionResult:
    """Collection efficiency and similar must land in 0-1, never as a percentage."""
    if not original_value:
        return ConversionResult(original_value, original_unit, original_value, original_unit)

    numbers, _multiplier = parse_numbers(original_value)
    if not numbers:
        return ConversionResult(
            original_value, original_unit, original_value, original_unit,
            warning=f"Could not parse numeric value {original_value!r} for {attribute_name}.",
        )

    value = numbers[0]
    text = detect_unit_text(original_value, original_unit)
    note = ""
    if "%" in original_value or "%" in original_unit or re.search(r"\bpercent\b", text):
        value /= 100.0
        note = "Converted from percent to a 0-1 fraction."
    elif value > 1.0:
        value /= 100.0
        note = f"Value {numbers[0]:g} exceeds 1; interpreted as a percentage and divided by 100."

    if value < 0.0 or value > 1.0:
        return ConversionResult(
            original_value, original_unit, original_value, original_unit,
            warning=f"{attribute_name} value {value!r} is outside the required 0-1 range.",
        )

    return ConversionResult(
        value=format_number(value),
        unit=CANONICAL_UNITS[attribute_name],
        original_value=original_value,
        original_unit=original_unit,
        note=note,
    )


def convert_ch4_volume(
    attribute_name: str,
    original_value: str,
    original_unit: str,
    unit_text: str,
) -> ConversionResult | None:
    """Convert a reported methane *volume* to mass, or refuse when the gas is not pure methane.

    Returns None when the unit is not a volume, so the caller falls through to the mass path.

    A landfill-gas volume cannot be converted without the methane fraction, which the spec's
    MMCFD formula takes as an input. Rather than assume a fraction, this refuses and says why.
    """
    is_cubic_feet = bool(re.search(r"(cubic\s*f(?:ee|oo)?t|ft3|ft\^3|ft³|scf|mmcf|mcf)", unit_text))
    is_cubic_metres = bool(re.search(r"(cubic\s*met(?:er|re)s?|m3|m\^3|m³|nm3)", unit_text))
    if not (is_cubic_feet or is_cubic_metres):
        return None

    mentions_methane = bool(re.search(r"(ch4|ch₄|methane)", unit_text)) or bool(
        re.search(r"(ch4|ch₄|methane)", original_value.lower())
    )
    mentions_landfill_gas = bool(re.search(r"(landfill gas|lfg|biogas|raw gas)", unit_text))
    if mentions_landfill_gas and not mentions_methane:
        return ConversionResult(
            original_value, original_unit, original_value, original_unit,
            warning=(
                f"{attribute_name}: {unit_text!r} is a landfill-gas volume, not methane. "
                "Converting needs the methane fraction, which the source did not give."
            ),
        )

    numbers, multiplier = parse_numbers(original_value)
    if not numbers:
        return ConversionResult(
            original_value, original_unit, original_value, original_unit,
            warning=f"Could not parse numeric value {original_value!r} for {attribute_name}.",
        )

    notes: list[str] = []
    scale = 1.0
    if re.search(r"\bmmcf", unit_text):
        scale = 1_000_000.0
        notes.append("Interpreted MMCF as million cubic feet.")
    elif re.search(r"\bmcf\b", unit_text):
        scale = 1_000.0
        notes.append("Interpreted MCF as thousand cubic feet.")

    if is_cubic_feet:
        kg_per_unit = CH4_KG_PER_CUBIC_FOOT
        notes.append(f"Converted cubic feet CH4 to mass at {CH4_KG_PER_CUBIC_FOOT} kg/ft3.")
    else:
        kg_per_unit = CH4_KG_PER_CUBIC_METRE
        notes.append(f"Converted cubic metres CH4 to mass at {CH4_KG_PER_CUBIC_METRE} kg/m3.")

    time_factor, time_note, time_warning = annualization_factor(unit_text, "rate")
    if time_factor is None:
        return ConversionResult(
            original_value, original_unit, original_value, original_unit, warning=time_warning
        )
    if time_note:
        notes.append(time_note)

    values = [number * multiplier * scale * kg_per_unit / 1000.0 * time_factor for number in numbers]
    return ConversionResult(
        value=format_converted_values(values),
        unit=CANONICAL_UNITS[attribute_name],
        original_value=original_value,
        original_unit=original_unit,
        note=" ".join(notes),
    )


def parse_coordinate(value: str) -> float | None:
    text = value.lower().replace(",", "")
    match = re.search(r"[-+]?\d*\.?\d+(?:e[-+]?\d+)?", text)
    if not match:
        return None

    number = float(match.group(0))
    hemisphere = re.search(r"\b(n|north|s|south|e|east|w|west)\b", text)
    if hemisphere and hemisphere.group(1) in {"s", "south", "w", "west"}:
        number = -abs(number)
    return number


def parse_numbers(value: str) -> tuple[list[float], float]:
    text = value.lower().replace(",", "")
    matches = re.findall(r"[-+]?\d*\.?\d+(?:e[-+]?\d+)?", text)
    multiplier = 1.0
    if re.search(r"\b(billion|bn)\b", text):
        multiplier = 1_000_000_000.0
    elif re.search(r"\b(million|mn|mm)\b", text):
        multiplier = 1_000_000.0
    elif re.search(r"\b(thousand|k)\b", text):
        multiplier = 1_000.0
    return [float(match) for match in matches], multiplier


def detect_unit_text(value: str, unit: str) -> str:
    """The unit to convert from: the unit field, else any unit wording inside the value.

    A value with no unit at all must come back empty, not as its own digits. Otherwise "487065"
    is read as the unit name, every converter reports "unsupported unit", and - because a failed
    conversion now blocks promotion - a perfectly good value is dropped to leads.
    """
    text = normalize_unit_text(unit) if unit else normalize_unit_text(value)
    return text if UNIT_WORD_PATTERN.search(text) else ""


def normalize_unit_text(value: str) -> str:
    text = value.lower().strip()
    # Gas-flow abbreviations bundle the volume scale and the time denominator into one token, so
    # split them out or the per-day factor is silently lost. MMCFD is the spec's own example.
    text = re.sub(r"\bmmscfd\b|\bmmcfd\b", "mmcf/day", text)
    text = re.sub(r"\bmscfd\b|\bmcfd\b", "mcf/day", text)
    text = re.sub(r"\bscfd\b|\bcfd\b", "cubic feet/day", text)
    text = re.sub(r"\bscfm\b|\bcfm\b", "cubic feet/minute", text)
    text = re.sub(r"\bmmscf\b", "mmcf", text)
    text = re.sub(r"\bmetric\s+tons?\b", "metric tonnes", text)
    text = re.sub(r"\bkilograms?\b", "kg", text)
    text = re.sub(r"\bpounds?\b", "lb", text)
    text = re.sub(r"\bper\s+annum\b", "per year", text)
    text = re.sub(r"\bper\s+yr\b", "per year", text)
    text = re.sub(r"/yr\b", "/year", text)
    text = re.sub(r"/y\b", "/year", text)
    text = re.sub(r"\s+per\s+", "/", text)
    return re.sub(r"\s+", " ", text)


def mass_to_metric_tonnes_factor(unit_text: str) -> tuple[float | None, str, str]:
    text = unit_text
    if not text:
        return 1.0, "No source unit provided; interpreted as already in metric tonnes.", ""
    if re.search(r"\b(short|us)\s+tons?\b", text):
        return 0.90718474, "Converted from US short tons to metric tonnes.", ""
    if re.search(r"\b(long|imperial)\s+tons?\b", text):
        return 1.0160469088, "Converted from long tons to metric tonnes.", ""
    if re.search(r"\bkg\b", text):
        return 0.001, "Converted from kg to metric tonnes.", ""
    if re.search(r"\blb\b", text):
        return 0.00045359237, "Converted from pounds to metric tonnes.", ""
    if re.search(r"\bmetric tonnes?\b|\btonnes?\b|\bt\b", text):
        return 1.0, "", ""
    if re.search(r"\btons?\b", text):
        return (
            None,
            "",
            "Ambiguous unit 'ton(s)' is not converted; source must distinguish metric tonnes, short tons, or long tons.",
        )
    return None, "", f"Unsupported mass unit {unit_text!r}."


def distance_to_km_factor(unit_text: str) -> tuple[float | None, str, str]:
    text = unit_text
    if not text:
        return 1.0, "No source unit provided; interpreted as kilometers.", ""
    if re.search(r"\b(km|kilomet(?:er|re)s?)\b", text):
        return 1.0, "", ""
    if re.search(r"\b(mi|mile|miles)\b", text):
        return 1.609344, "Converted from miles to kilometers.", ""
    if re.search(r"\b(m|meter|meters|metre|metres)\b", text):
        return 0.001, "Converted from meters to kilometers.", ""
    return None, "", f"Unsupported distance unit {unit_text!r}."


def annualization_factor(unit_text: str, expected_kind: str) -> tuple[float | None, str, str]:
    text = unit_text
    has_time_denominator = bool(
        re.search(r"(/day|/d\b|daily|/week|weekly|/month|monthly|/year|annual|annually|per year)", text)
    )

    if expected_kind == "mass":
        if has_time_denominator:
            return None, "", "Rate unit supplied for stock attribute; cannot convert to waste-in-place mass."
        return 1.0, "", ""

    if re.search(r"(/day|/d\b|daily)", text):
        return 365.0, "Annualized daily rate using 365 days/year.", ""
    if re.search(r"(/week|weekly)", text):
        return 52.0, "Annualized weekly rate using 52 weeks/year.", ""
    if re.search(r"(/month|monthly)", text):
        return 12.0, "Annualized monthly rate using 12 months/year.", ""
    if re.search(r"(/minute|/min\b)", text):
        return 525600.0, "Annualized per-minute rate using 525600 minutes/year.", ""
    if re.search(r"(/hour|/hr\b|hourly)", text):
        return 8760.0, "Annualized hourly rate using 8760 hours/year.", ""
    if re.search(r"(/year|annual|annually|per year)", text):
        return 1.0, "", ""
    return 1.0, "No time denominator provided; interpreted as an annual total.", ""


def format_number(value: float) -> str:
    if abs(value - round(value)) < 1e-9:
        return str(int(round(value)))
    return f"{value:.6f}".rstrip("0").rstrip(".")


def format_coordinate(value: float) -> str:
    return f"{value:.8f}".rstrip("0").rstrip(".")


def format_converted_values(values: list[float]) -> str:
    if len(values) == 1:
        return format_number(values[0])
    return " to ".join(format_number(value) for value in values)
