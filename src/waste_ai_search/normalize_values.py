"""One found value as our database holds it: converted to the attribute's unit, mapped to its allowed
values, and checked against its range.

The search's values go through it (arbitrate.extract_evidence), and so does a value an SME types in
Site_Review's corrected_value (accept_reviews.py), so both are held to the same rules.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .schema import (
    ARRAY_TARGET_ATTRIBUTES,
    ATTRIBUTE_RANGES,
    BOOLEAN_TARGET_ATTRIBUTES,
    ENUM_MAPS,
    NUMERIC_TARGET_ATTRIBUTES,
    bucket_waste_depth,
    is_blank,
    map_enum_value,
    normalize_scalar,
    parse_tristate_bool,
)
from .unit_converter import ConversionResult, convert_attribute_value


def parse_float(value: Any) -> float | None:
    text = normalize_scalar(value).replace(",", "")
    if not text:
        return None
    try:
        return float(text)
    except ValueError:
        return None


@dataclass
class Normalized:
    """What normalize_value makes of a value. It can't be used where `value` is blank or
    `numeric_note` is set."""

    conversion: ConversionResult
    value: Any
    mapping_note: str = ""
    numeric_note: str = ""
    # What went wrong, one line each, as Parse_Warnings lists them.
    warnings: list[str] = field(default_factory=list)

    @property
    def usable(self) -> bool:
        return not is_blank(self.value) and not self.numeric_note


def normalize_value(name: str, raw_value: Any, raw_unit: Any) -> Normalized:
    """Convert, map and range-check one value of attribute `name`."""
    warnings = []
    conversion = convert_attribute_value(name, raw_value, raw_unit)
    # A failed conversion returns the value untouched in its ORIGINAL unit. Using that would
    # file an unconverted number in a column whose name asserts the canonical unit - e.g. an
    # ambiguous "30000 tons" landing in waste_in_place_metric_tonnes, a 10% error if the
    # source meant short tons. If we could not normalize it, it cannot be used.
    conversion_failed = bool(conversion.warning)
    if conversion.warning:
        warnings.append(f"{name}: {conversion.warning}")

    value: Any = conversion.value
    mapping_note = ""
    if name in BOOLEAN_TARGET_ATTRIBUTES:
        parsed, note = parse_tristate_bool(value)
        mapping_note = note
        value = "" if parsed is None else ("TRUE" if parsed else "FALSE")
    elif name == "waste_depth":
        # Bin the CONVERTED metres, not the raw value: the source may have reported feet,
        # and the 5m boundary is only meaningful once the unit conversion above has run. A
        # failed conversion leaves conversion.value in its original unit, so skip binning
        # entirely there and let the conversion warning exclude the row.
        if conversion_failed:
            value = ""
        else:
            mapped, mapping_note = bucket_waste_depth(conversion.value)
            value = normalize_scalar(mapped or "")
    elif name in ENUM_MAPS:
        mapped, mapping_note = map_enum_value(name, raw_value)
        value = normalize_scalar(mapped) if name in ARRAY_TARGET_ATTRIBUTES else normalize_scalar(mapped or "")
    if mapping_note:
        warnings.append(f"{name}: {mapping_note}")

    # A numeric attribute whose value is not a number must never be used. The agent can return
    # unit text or prose here, and it would otherwise land in a numeric spec column.
    numeric_note = ""
    if conversion_failed:
        numeric_note = f"{name}: {conversion.warning}"
    elif name in NUMERIC_TARGET_ATTRIBUTES and not is_blank(value):
        parsed = parse_float(value)
        if parsed is None:
            numeric_note = f"{name} value {value!r} is not numeric; not used."
        else:
            low, high = ATTRIBUTE_RANGES.get(name, (None, None))
            if (low is not None and parsed < low) or (high is not None and parsed > high):
                bounds = f"{low if low is not None else '-inf'} to {high if high is not None else 'inf'}"
                numeric_note = (
                    f"{name} value {parsed:g} is outside the spec range {bounds}; not used."
                )
        if numeric_note:
            warnings.append(numeric_note)
    return Normalized(conversion, value, mapping_note, numeric_note, warnings)
