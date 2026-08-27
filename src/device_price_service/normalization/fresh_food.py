from __future__ import annotations

import re
from collections.abc import Callable
from decimal import Decimal, InvalidOperation
from typing import Any

from device_price_service.domain.catalog_crawl import (
    DiscoveredCatalogListing,
    NormalizedListingIdentity,
    ParsedCatalogListing,
)
from device_price_service.domain.catalog_enums import (
    ConditionCode,
    MeasureType,
    QualityStatus,
)
from device_price_service.normalization.catalog_rules import CategoryRule, CategoryRuleRegistry
from device_price_service.normalization.measurements import (
    NormalizedQuantity,
    QuantityParseError,
    parse_quantity,
)


class FreshIdentityValueError(ValueError):
    """Raised when a known identity field contains an unsupported value."""


_GRADE_ALIASES = {
    "特级": "PREMIUM",
    "一级": "GRADE_1",
    "一等": "GRADE_1",
    "1级": "GRADE_1",
    "二级": "GRADE_2",
    "二等": "GRADE_2",
    "2级": "GRADE_2",
    "A级": "GRADE_A",
    "A": "GRADE_A",
    "AA级": "GRADE_AA",
    "AA": "GRADE_AA",
}
_FRESHNESS_ALIASES = {
    "鲜": "FRESH",
    "新鲜": "FRESH",
    "冷鲜": "CHILLED",
    "冷藏": "CHILLED",
    "冰鲜": "CHILLED",
    "冷冻": "FROZEN",
    "冻": "FROZEN",
    "常温": "AMBIENT",
}
_PACKAGING_ALIASES = {
    "箱装": "BOX",
    "盒装": "BOX",
    "袋装": "BAG",
    "板装": "TRAY",
    "托盘装": "TRAY",
    "真空包装": "VACUUM",
    "简装": "SIMPLE",
}
_APPLE_VARIETY_ALIASES = {
    "红富士": "RED_FUJI",
    "红富士苹果": "RED_FUJI",
    "REDFUJI": "RED_FUJI",
}
_EGG_TYPE_ALIASES = {
    "鸡蛋": "CHICKEN_EGG",
    "鲜鸡蛋": "CHICKEN_EGG",
    "普通鸡蛋": "CHICKEN_EGG",
    "土鸡蛋": "FREE_RANGE_CHICKEN_EGG",
}
_PORK_CUT_ALIASES = {
    "五花肉": "PORK_BELLY",
    "猪五花肉": "PORK_BELLY",
    "带皮五花肉": "PORK_BELLY",
    "里脊": "PORK_TENDERLOIN",
    "猪里脊": "PORK_TENDERLOIN",
    "梅花肉": "PORK_COLLAR",
    "猪梅花肉": "PORK_COLLAR",
}
_BONE_STATE_ALIASES = {
    "去骨": "BONELESS",
    "无骨": "BONELESS",
    "带骨": "BONE_IN",
}
_SKIN_STATE_ALIASES = {
    "带皮": "SKIN_ON",
    "去皮": "SKINLESS",
    "无皮": "SKINLESS",
}
_GOVERNMENT_COMMODITY_GROUP_ALIASES = {
    "蔬菜": "VEGETABLE",
    "水果": "FRUIT",
    "肉禽蛋": "MEAT_EGG",
    "肉类": "MEAT_EGG",
    "禽蛋": "MEAT_EGG",
}


class FreshAppleRule(CategoryRule):
    profile_code = "fresh-apple"
    version = "1"

    def normalize(
        self,
        item: DiscoveredCatalogListing,
        parsed: ParsedCatalogListing,
    ) -> NormalizedListingIdentity:
        return _normalize_fresh_identity(
            parsed.source_attributes,
            measure_type=MeasureType.WEIGHT,
            fields={
                "variety": lambda value: _alias(value, _APPLE_VARIETY_ALIASES, "variety"),
                "origin": _origin,
                "grade": lambda value: _alias(value, _GRADE_ALIASES, "grade"),
                "freshness_state": lambda value: _alias(
                    value, _FRESHNESS_ALIASES, "freshness_state"
                ),
                "packaging": lambda value: _alias(value, _PACKAGING_ALIASES, "packaging"),
            },
            optional_fields={"fruit_diameter_mm": _positive_decimal_text},
        )


class PackagedEggRule(CategoryRule):
    profile_code = "packaged-egg"
    version = "1"

    def normalize(
        self,
        item: DiscoveredCatalogListing,
        parsed: ParsedCatalogListing,
    ) -> NormalizedListingIdentity:
        return _normalize_fresh_identity(
            parsed.source_attributes,
            measure_type=MeasureType.COUNT,
            fields={
                "egg_type": lambda value: _alias(value, _EGG_TYPE_ALIASES, "egg_type"),
                "origin": _origin,
                "grade": lambda value: _alias(value, _GRADE_ALIASES, "grade"),
                "freshness_state": lambda value: _alias(
                    value, _FRESHNESS_ALIASES, "freshness_state"
                ),
                "packaging": lambda value: _alias(value, _PACKAGING_ALIASES, "packaging"),
            },
            optional_fields={"shell_color": _upper_token},
        )


class FreshPorkRule(CategoryRule):
    profile_code = "fresh-pork"
    version = "1"

    def normalize(
        self,
        item: DiscoveredCatalogListing,
        parsed: ParsedCatalogListing,
    ) -> NormalizedListingIdentity:
        return _normalize_fresh_identity(
            parsed.source_attributes,
            measure_type=MeasureType.WEIGHT,
            fields={
                "cut": lambda value: _alias(value, _PORK_CUT_ALIASES, "cut"),
                "origin": _origin,
                "grade": lambda value: _alias(value, _GRADE_ALIASES, "grade"),
                "freshness_state": lambda value: _alias(
                    value, _FRESHNESS_ALIASES, "freshness_state"
                ),
                "packaging": lambda value: _alias(value, _PACKAGING_ALIASES, "packaging"),
            },
            optional_fields={
                "bone_state": lambda value: _alias(
                    value, _BONE_STATE_ALIASES, "bone_state"
                ),
                "skin_state": lambda value: _alias(
                    value, _SKIN_STATE_ALIASES, "skin_state"
                ),
            },
            fixed_attributes={"animal_type": "PORK"},
        )


class GovernmentFreshCommodityRule(CategoryRule):
    """Normalize a named commodity quoted by weight in a government price table."""

    profile_code = "government-fresh"
    version = "1"

    def normalize(
        self,
        item: DiscoveredCatalogListing,
        parsed: ParsedCatalogListing,
    ) -> NormalizedListingIdentity:
        identity = _normalize_fresh_identity(
            parsed.source_attributes,
            measure_type=MeasureType.WEIGHT,
            fields={
                "commodity_code": _government_commodity_code,
                "commodity_name": _source_text,
                "commodity_group": lambda value: _alias(
                    value,
                    _GOVERNMENT_COMMODITY_GROUP_ALIASES,
                    "commodity_group",
                ),
                "source_specification": _source_text,
                "quoted_unit": _government_quoted_unit,
            },
        )
        if identity.quality_status is not QualityStatus.ACCEPTED:
            return identity
        quoted_unit = identity.normalized_attributes.get("quoted_unit")
        expected_quantity = {
            "CNY_PER_500G": Decimal("0.500"),
            "CNY_PER_KG": Decimal("1"),
        }.get(quoted_unit) if isinstance(quoted_unit, str) else None
        if (
            expected_quantity is None
            or identity.base_quantity_value != expected_quantity
            or identity.base_unit != "KG"
        ):
            return identity.model_copy(
                update={
                    "quality_status": QualityStatus.REVIEW_REQUIRED,
                    "rejection_code": "QUOTED_UNIT_MISMATCH",
                }
            )
        return identity


def register_fresh_food_rules(registry: CategoryRuleRegistry) -> None:
    registry.register(FreshAppleRule())
    registry.register(PackagedEggRule())
    registry.register(FreshPorkRule())
    registry.register(GovernmentFreshCommodityRule())


def build_fresh_food_rule_registry() -> CategoryRuleRegistry:
    registry = CategoryRuleRegistry()
    register_fresh_food_rules(registry)
    return registry


def _normalize_fresh_identity(
    source: dict[str, Any],
    *,
    measure_type: MeasureType,
    fields: dict[str, Callable[[str], str]],
    optional_fields: dict[str, Callable[[str], str]] | None = None,
    fixed_attributes: dict[str, str] | None = None,
) -> NormalizedListingIdentity:
    normalized_attributes = dict(fixed_attributes or {})
    missing = [name for name in fields if _text(source.get(name)) is None]
    try:
        for name, normalizer in fields.items():
            value = _text(source.get(name))
            if value is not None:
                normalized_attributes[name] = normalizer(value)
        for name, normalizer in (optional_fields or {}).items():
            value = _text(source.get(name))
            if value is not None:
                normalized_attributes[name] = normalizer(value)
    except FreshIdentityValueError:
        return _review_identity(
            measure_type=measure_type,
            normalized_attributes=normalized_attributes,
            source=source,
            rejection_code="IDENTITY_VALUE_UNSUPPORTED",
        )
    if missing:
        return _review_identity(
            measure_type=measure_type,
            normalized_attributes=normalized_attributes,
            source=source,
            rejection_code="IDENTITY_FIELDS_MISSING",
        )

    quantity_text = _text(source.get("net_content"))
    if quantity_text is None:
        return _review_identity(
            measure_type=measure_type,
            normalized_attributes=normalized_attributes,
            source=source,
            rejection_code="QUANTITY_UNRESOLVED",
        )
    try:
        quantity = parse_quantity(quantity_text, expected_measure_type=measure_type)
    except QuantityParseError:
        return _review_identity(
            measure_type=measure_type,
            normalized_attributes=normalized_attributes,
            source=source,
            rejection_code="QUANTITY_UNRESOLVED",
        )
    return _identity_from_quantity(
        quantity,
        normalized_attributes=normalized_attributes,
        quality_status=QualityStatus.ACCEPTED,
        rejection_code=None,
    )


def _review_identity(
    *,
    measure_type: MeasureType,
    normalized_attributes: dict[str, Any],
    source: dict[str, Any],
    rejection_code: str,
) -> NormalizedListingIdentity:
    quantity_text = _text(source.get("net_content"))
    if quantity_text is not None:
        try:
            quantity = parse_quantity(quantity_text, expected_measure_type=measure_type)
        except QuantityParseError:
            quantity = None
        if quantity is not None:
            return _identity_from_quantity(
                quantity,
                normalized_attributes=normalized_attributes,
                quality_status=QualityStatus.REVIEW_REQUIRED,
                rejection_code=rejection_code,
            )
    return NormalizedListingIdentity(
        normalized_attributes=normalized_attributes,
        condition_code=ConditionCode.NEW,
        measure_type=measure_type,
        quality_status=QualityStatus.REVIEW_REQUIRED,
        rejection_code=rejection_code,
    )


def _identity_from_quantity(
    quantity: NormalizedQuantity,
    *,
    normalized_attributes: dict[str, Any],
    quality_status: QualityStatus,
    rejection_code: str | None,
) -> NormalizedListingIdentity:
    return NormalizedListingIdentity(
        normalized_attributes=normalized_attributes,
        condition_code=ConditionCode.NEW,
        measure_type=quantity.measure_type,
        quantity_value=quantity.quantity_value,
        quantity_min=quantity.quantity_min,
        quantity_max=quantity.quantity_max,
        quantity_unit=quantity.quantity_unit,
        base_quantity_value=quantity.base_quantity_value,
        base_quantity_min=quantity.base_quantity_min,
        base_quantity_max=quantity.base_quantity_max,
        base_unit=quantity.base_unit,
        package_count=quantity.package_count,
        quality_status=quality_status,
        rejection_code=rejection_code,
    )


def _alias(value: str, aliases: dict[str, str], field_name: str) -> str:
    token = re.sub(r"\s+", "", value).upper()
    try:
        return aliases[token]
    except KeyError as error:
        raise FreshIdentityValueError(f"unsupported {field_name}: {value!r}") from error


def _origin(value: str) -> str:
    normalized = re.sub(r"\s+", "", value)
    if not normalized:
        raise FreshIdentityValueError("origin cannot be blank")
    return normalized.upper() if normalized.isascii() else normalized


def _positive_decimal_text(value: str) -> str:
    try:
        parsed = Decimal(value)
    except InvalidOperation as error:
        raise FreshIdentityValueError(f"invalid decimal identity value: {value!r}") from error
    if not parsed.is_finite() or parsed <= 0:
        raise FreshIdentityValueError("decimal identity value must be positive")
    normalized = parsed.normalize()
    return format(normalized, "f")


def _upper_token(value: str) -> str:
    normalized = re.sub(r"\s+", "_", value.strip()).upper()
    if not normalized:
        raise FreshIdentityValueError("identity token cannot be blank")
    return normalized


def _government_commodity_code(value: str) -> str:
    normalized = _upper_token(value)
    if re.fullmatch(r"[A-Z][A-Z0-9_]{1,63}", normalized) is None:
        raise FreshIdentityValueError("government commodity code must be a stable ASCII token")
    return normalized


def _government_quoted_unit(value: str) -> str:
    normalized = re.sub(r"\s+", "", value).lower()
    aliases = {
        "元/500克": "CNY_PER_500G",
        "元/500g": "CNY_PER_500G",
        "元/公斤": "CNY_PER_KG",
        "元/千克": "CNY_PER_KG",
        "元/kg": "CNY_PER_KG",
    }
    try:
        return aliases[normalized]
    except KeyError as error:
        raise FreshIdentityValueError(
            f"unsupported government quoted unit: {value!r}"
        ) from error


def _source_text(value: str) -> str:
    normalized = re.sub(r"\s+", " ", value).strip()
    if not normalized:
        raise FreshIdentityValueError("source identity text cannot be blank")
    return normalized


def _text(value: object) -> str | None:
    if value is None:
        return None
    normalized = str(value).strip()
    return normalized or None
