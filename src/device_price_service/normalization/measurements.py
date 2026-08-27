from __future__ import annotations

import re
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation

from device_price_service.domain.catalog_enums import MeasureType


class QuantityParseError(ValueError):
    """Raised when a source quantity cannot be converted without guessing."""


@dataclass(frozen=True, slots=True)
class NormalizedQuantity:
    measure_type: MeasureType
    quantity_value: Decimal | None
    quantity_min: Decimal | None
    quantity_max: Decimal | None
    quantity_unit: str
    base_quantity_value: Decimal | None
    base_quantity_min: Decimal | None
    base_quantity_max: Decimal | None
    base_unit: str
    package_count: int | None = None


@dataclass(frozen=True, slots=True)
class _UnitDefinition:
    measure_type: MeasureType
    canonical_unit: str
    base_unit: str
    factor: Decimal


_UNIT_DEFINITIONS = {
    "G": _UnitDefinition(MeasureType.WEIGHT, "G", "KG", Decimal("0.001")),
    "克": _UnitDefinition(MeasureType.WEIGHT, "G", "KG", Decimal("0.001")),
    "KG": _UnitDefinition(MeasureType.WEIGHT, "KG", "KG", Decimal("1")),
    "千克": _UnitDefinition(MeasureType.WEIGHT, "KG", "KG", Decimal("1")),
    "公斤": _UnitDefinition(MeasureType.WEIGHT, "KG", "KG", Decimal("1")),
    "JIN": _UnitDefinition(MeasureType.WEIGHT, "JIN", "KG", Decimal("0.5")),
    "斤": _UnitDefinition(MeasureType.WEIGHT, "JIN", "KG", Decimal("0.5")),
    "ML": _UnitDefinition(MeasureType.VOLUME, "ML", "L", Decimal("0.001")),
    "毫升": _UnitDefinition(MeasureType.VOLUME, "ML", "L", Decimal("0.001")),
    "L": _UnitDefinition(MeasureType.VOLUME, "L", "L", Decimal("1")),
    "升": _UnitDefinition(MeasureType.VOLUME, "L", "L", Decimal("1")),
    "PIECE": _UnitDefinition(MeasureType.COUNT, "PIECE", "PIECE", Decimal("1")),
    "PCS": _UnitDefinition(MeasureType.COUNT, "PIECE", "PIECE", Decimal("1")),
    "PC": _UnitDefinition(MeasureType.COUNT, "PIECE", "PIECE", Decimal("1")),
    "个": _UnitDefinition(MeasureType.COUNT, "PIECE", "PIECE", Decimal("1")),
    "枚": _UnitDefinition(MeasureType.COUNT, "PIECE", "PIECE", Decimal("1")),
    "只": _UnitDefinition(MeasureType.COUNT, "PIECE", "PIECE", Decimal("1")),
}

_NUMBER = r"(?:\d+(?:\.\d+)?)"
_EXACT_PATTERN = re.compile(rf"^(?P<value>{_NUMBER})(?P<unit>[A-Z]+|[\u4e00-\u9fff]+)$")
_RANGE_PATTERN = re.compile(
    rf"^(?P<minimum>{_NUMBER})(?:-|~|～|至)(?P<maximum>{_NUMBER})"
    r"(?P<unit>[A-Z]+|[\u4e00-\u9fff]+)$"
)
_COUNT_MULTIPLIER_PATTERN = re.compile(
    r"^(?P<outer>\d+)(?:盒|包|袋|板)(?:\*|X|×)(?P<inner>\d+)"
    r"(?P<unit>枚|个|只|PIECE|PCS|PC)$"
)


def parse_quantity(text: str, *, expected_measure_type: MeasureType) -> NormalizedQuantity:
    """Parse a strict, auditable quantity expression used by fresh-food rules."""

    normalized = re.sub(r"\s+", "", text).upper()
    if not normalized:
        raise QuantityParseError("quantity text cannot be blank")

    multiplier_match = _COUNT_MULTIPLIER_PATTERN.fullmatch(normalized)
    if multiplier_match is not None:
        if expected_measure_type is not MeasureType.COUNT:
            raise QuantityParseError("count package expression has the wrong measure type")
        total = Decimal(multiplier_match.group("outer")) * Decimal(
            multiplier_match.group("inner")
        )
        return _build_exact(total, "PIECE", expected_measure_type, package_count=int(total))

    range_match = _RANGE_PATTERN.fullmatch(normalized)
    if range_match is not None:
        minimum = _decimal(range_match.group("minimum"))
        maximum = _decimal(range_match.group("maximum"))
        if maximum < minimum:
            raise QuantityParseError("quantity range maximum cannot be below minimum")
        definition = _unit(range_match.group("unit"), expected_measure_type)
        return NormalizedQuantity(
            measure_type=definition.measure_type,
            quantity_value=None,
            quantity_min=minimum,
            quantity_max=maximum,
            quantity_unit=definition.canonical_unit,
            base_quantity_value=None,
            base_quantity_min=minimum * definition.factor,
            base_quantity_max=maximum * definition.factor,
            base_unit=definition.base_unit,
        )

    exact_match = _EXACT_PATTERN.fullmatch(normalized)
    if exact_match is None:
        raise QuantityParseError(f"unsupported quantity expression: {text!r}")
    value = _decimal(exact_match.group("value"))
    unit = exact_match.group("unit")
    package_count = None
    if expected_measure_type is MeasureType.COUNT:
        if value != value.to_integral_value():
            raise QuantityParseError("count quantity must be a whole number")
        package_count = int(value)
    return _build_exact(value, unit, expected_measure_type, package_count=package_count)


def _build_exact(
    value: Decimal,
    unit: str,
    expected_measure_type: MeasureType,
    *,
    package_count: int | None,
) -> NormalizedQuantity:
    definition = _unit(unit, expected_measure_type)
    return NormalizedQuantity(
        measure_type=definition.measure_type,
        quantity_value=value,
        quantity_min=None,
        quantity_max=None,
        quantity_unit=definition.canonical_unit,
        base_quantity_value=value * definition.factor,
        base_quantity_min=None,
        base_quantity_max=None,
        base_unit=definition.base_unit,
        package_count=package_count,
    )


def _unit(value: str, expected_measure_type: MeasureType) -> _UnitDefinition:
    try:
        definition = _UNIT_DEFINITIONS[value]
    except KeyError as error:
        raise QuantityParseError(f"unsupported quantity unit: {value!r}") from error
    if definition.measure_type is not expected_measure_type:
        raise QuantityParseError(
            f"quantity unit {value!r} does not match {expected_measure_type.value}"
        )
    return definition


def _decimal(value: str) -> Decimal:
    try:
        parsed = Decimal(value)
    except InvalidOperation as error:
        raise QuantityParseError(f"invalid quantity number: {value!r}") from error
    if parsed <= 0:
        raise QuantityParseError("quantity must be greater than zero")
    return parsed
