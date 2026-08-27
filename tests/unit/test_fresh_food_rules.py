from decimal import Decimal

import pytest

from device_price_service.domain.catalog_crawl import (
    DiscoveredCatalogListing,
    ParsedCatalogListing,
    SourceMerchant,
    SourcePriceCandidate,
)
from device_price_service.domain.catalog_enums import (
    Availability,
    FeeStatus,
    MeasureType,
    PriceNature,
    PriceType,
    PricingBasis,
    QualityStatus,
    SellerType,
    VerificationStatus,
)
from device_price_service.normalization.fresh_food import (
    FreshAppleRule,
    FreshPorkRule,
    GovernmentFreshCommodityRule,
    PackagedEggRule,
    build_fresh_food_rule_registry,
)
from device_price_service.normalization.measurements import QuantityParseError, parse_quantity


def _item(category_code: str) -> DiscoveredCatalogListing:
    return DiscoveredCatalogListing(
        listing_key=f"fixture:{category_code}",
        url="https://fresh.example.test/item",
        category_code=category_code,
        merchant=SourceMerchant(
            merchant_key="fixture-self",
            name="示例自营",
            seller_type=SellerType.PLATFORM_SELF,
            verification_status=VerificationStatus.VERIFIED,
        ),
        price_nature=PriceNature.RETAIL_OFFER,
    )


def _parsed(attributes: dict[str, object]) -> ParsedCatalogListing:
    return ParsedCatalogListing(
        source_title="脱敏生鲜样本",
        source_attributes=attributes,
        price_candidates=[
            SourcePriceCandidate(
                current_price=Decimal("10"),
                price_type=PriceType.DIRECT_UNCONDITIONAL,
                pricing_basis=PricingBasis.PACKAGE_TOTAL,
                availability=Availability.ON_SALE,
                fee_status=FeeStatus.ITEM_ONLY,
            )
        ],
    )


@pytest.mark.parametrize(
    ("text", "measure_type", "base_value", "base_unit", "package_count"),
    [
        ("5kg", MeasureType.WEIGHT, Decimal("5"), "KG", None),
        ("5000 克", MeasureType.WEIGHT, Decimal("5.000"), "KG", None),
        ("1斤", MeasureType.WEIGHT, Decimal("0.5"), "KG", None),
        ("950ml", MeasureType.VOLUME, Decimal("0.950"), "L", None),
        ("30枚", MeasureType.COUNT, Decimal("30"), "PIECE", 30),
        ("2盒×15枚", MeasureType.COUNT, Decimal("30"), "PIECE", 30),
    ],
)
def test_quantity_parser_converts_supported_units_exactly(
    text: str,
    measure_type: MeasureType,
    base_value: Decimal,
    base_unit: str,
    package_count: int | None,
) -> None:
    quantity = parse_quantity(text, expected_measure_type=measure_type)

    assert quantity.base_quantity_value == base_value
    assert quantity.base_unit == base_unit
    assert quantity.package_count == package_count


def test_quantity_parser_preserves_auditable_range() -> None:
    quantity = parse_quantity("400-500g", expected_measure_type=MeasureType.WEIGHT)

    assert quantity.quantity_value is None
    assert quantity.quantity_min == Decimal("400")
    assert quantity.quantity_max == Decimal("500")
    assert quantity.base_quantity_min == Decimal("0.400")
    assert quantity.base_quantity_max == Decimal("0.500")


@pytest.mark.parametrize(
    ("text", "measure_type"),
    [
        ("约5kg", MeasureType.WEIGHT),
        ("5磅", MeasureType.WEIGHT),
        ("500g", MeasureType.VOLUME),
        ("1.5枚", MeasureType.COUNT),
        ("500-400g", MeasureType.WEIGHT),
    ],
)
def test_quantity_parser_rejects_ambiguous_or_incompatible_values(
    text: str,
    measure_type: MeasureType,
) -> None:
    with pytest.raises(QuantityParseError):
        parse_quantity(text, expected_measure_type=measure_type)


def _apple_attributes(**overrides: object) -> dict[str, object]:
    values: dict[str, object] = {
        "variety": "红富士苹果",
        "origin": "陕西洛川",
        "grade": "一级",
        "freshness_state": "新鲜",
        "packaging": "箱装",
        "fruit_diameter_mm": "80",
        "net_content": "5kg",
    }
    values.update(overrides)
    return values


def test_fresh_apple_rule_normalizes_comparable_identity() -> None:
    identity = FreshAppleRule().normalize(
        _item("FRESH_APPLE"),
        _parsed(_apple_attributes()),
    )

    assert identity.quality_status is QualityStatus.ACCEPTED
    assert identity.normalized_attributes == {
        "variety": "RED_FUJI",
        "origin": "陕西洛川",
        "grade": "GRADE_1",
        "freshness_state": "FRESH",
        "packaging": "BOX",
        "fruit_diameter_mm": "80",
    }
    assert identity.base_quantity_value == Decimal("5")
    assert identity.base_unit == "KG"


def test_equivalent_weight_expressions_share_identity_fingerprint() -> None:
    rule = FreshAppleRule()
    kilograms = rule.normalize(_item("FRESH_APPLE"), _parsed(_apple_attributes()))
    grams = rule.normalize(
        _item("FRESH_APPLE"),
        _parsed(_apple_attributes(net_content="5000g")),
    )

    assert kilograms.quantity_unit == "KG"
    assert grams.quantity_unit == "G"
    assert kilograms.build_fingerprint() == grams.build_fingerprint()


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("origin", "山东烟台"),
        ("grade", "二级"),
        ("freshness_state", "冷藏"),
        ("packaging", "袋装"),
        ("fruit_diameter_mm", "75"),
    ],
)
def test_comparable_apple_attributes_change_identity_fingerprint(
    field: str,
    value: str,
) -> None:
    rule = FreshAppleRule()
    baseline = rule.normalize(_item("FRESH_APPLE"), _parsed(_apple_attributes()))
    changed = rule.normalize(
        _item("FRESH_APPLE"),
        _parsed(_apple_attributes(**{field: value})),
    )

    assert baseline.build_fingerprint() != changed.build_fingerprint()


@pytest.mark.parametrize(
    ("overrides", "code"),
    [
        ({"grade": None}, "IDENTITY_FIELDS_MISSING"),
        ({"variety": "未知苹果"}, "IDENTITY_VALUE_UNSUPPORTED"),
        ({"net_content": "约5kg"}, "QUANTITY_UNRESOLVED"),
    ],
)
def test_unresolved_fresh_identity_requires_review(
    overrides: dict[str, object],
    code: str,
) -> None:
    identity = FreshAppleRule().normalize(
        _item("FRESH_APPLE"),
        _parsed(_apple_attributes(**overrides)),
    )

    assert identity.quality_status is QualityStatus.REVIEW_REQUIRED
    assert identity.rejection_code == code


def test_packaged_egg_rule_preserves_total_count() -> None:
    identity = PackagedEggRule().normalize(
        _item("FRESH_EGG"),
        _parsed(
            {
                "egg_type": "鲜鸡蛋",
                "origin": "河北示例产区",
                "grade": "A级",
                "freshness_state": "新鲜",
                "packaging": "盒装",
                "shell_color": "brown",
                "net_content": "2盒×15枚",
            }
        ),
    )

    assert identity.quality_status is QualityStatus.ACCEPTED
    assert identity.base_quantity_value == Decimal("30")
    assert identity.base_unit == "PIECE"
    assert identity.package_count == 30
    assert identity.normalized_attributes["egg_type"] == "CHICKEN_EGG"


def test_fresh_pork_rule_requires_explicit_cut_and_freshness() -> None:
    identity = FreshPorkRule().normalize(
        _item("FRESH_PORK"),
        _parsed(
            {
                "cut": "猪五花肉",
                "origin": "山东示例产区",
                "grade": "一级",
                "freshness_state": "冷鲜",
                "packaging": "真空包装",
                "bone_state": "去骨",
                "skin_state": "带皮",
                "net_content": "500g",
            }
        ),
    )

    assert identity.quality_status is QualityStatus.ACCEPTED
    assert identity.base_quantity_value == Decimal("0.500")
    assert identity.normalized_attributes["cut"] == "PORK_BELLY"
    assert identity.normalized_attributes["freshness_state"] == "CHILLED"
    assert identity.normalized_attributes["bone_state"] == "BONELESS"
    assert identity.normalized_attributes["skin_state"] == "SKIN_ON"


def test_government_fresh_rule_preserves_commodity_and_quoted_weight() -> None:
    identity = GovernmentFreshCommodityRule().normalize(
        _item("FRESH_MONITORED_COMMODITY"),
        _parsed(
            {
                "commodity_code": "QINGCAI",
                "commodity_name": "青菜",
                "commodity_group": "蔬菜",
                "source_specification": "新鲜一级",
                "quoted_unit": "元/500克",
                "net_content": "500克",
                "source_date": "2026-08-19",
                "time_precision": "DAY",
            }
        ),
    )

    assert identity.quality_status is QualityStatus.ACCEPTED
    assert identity.base_quantity_value == Decimal("0.500")
    assert identity.base_unit == "KG"
    assert identity.normalized_attributes == {
        "commodity_code": "QINGCAI",
        "commodity_name": "青菜",
        "commodity_group": "VEGETABLE",
        "source_specification": "新鲜一级",
        "quoted_unit": "CNY_PER_500G",
    }


def test_government_fresh_rule_accepts_exact_kilogram_wholesale_quote() -> None:
    identity = GovernmentFreshCommodityRule().normalize(
        _item("FRESH_MONITORED_COMMODITY"),
        _parsed(
            {
                "commodity_code": "CUCUMBER",
                "commodity_name": "黄瓜",
                "commodity_group": "蔬菜",
                "source_specification": "批发市场当日报价",
                "quoted_unit": "元/公斤",
                "net_content": "1公斤",
            }
        ),
    )

    assert identity.quality_status is QualityStatus.ACCEPTED
    assert identity.base_quantity_value == Decimal("1")
    assert identity.base_unit == "KG"
    assert identity.normalized_attributes["quoted_unit"] == "CNY_PER_KG"


@pytest.mark.parametrize(
    ("source_group", "normalized_group"),
    [
        ("蔬菜", "VEGETABLE"),
        ("禽蛋", "MEAT_EGG"),
        ("肉类", "MEAT_EGG"),
        ("肉禽蛋", "MEAT_EGG"),
    ],
)
def test_government_fresh_rule_accepts_supported_mofcom_groups(
    source_group: str,
    normalized_group: str,
) -> None:
    identity = GovernmentFreshCommodityRule().normalize(
        _item("FRESH_MONITORED_COMMODITY"),
        _parsed(
            {
                "commodity_code": "FIXTURE",
                "commodity_name": "脱敏品种",
                "commodity_group": source_group,
                "source_specification": "批发市场当日报价",
                "quoted_unit": "元/公斤",
                "net_content": "1公斤",
            }
        ),
    )

    assert identity.quality_status is QualityStatus.ACCEPTED
    assert identity.normalized_attributes["commodity_group"] == normalized_group


def test_government_fresh_rule_rejects_unsupported_quoted_unit() -> None:
    identity = GovernmentFreshCommodityRule().normalize(
        _item("FRESH_MONITORED_COMMODITY"),
        _parsed(
            {
                "commodity_code": "QINGCAI",
                "commodity_name": "青菜",
                "commodity_group": "蔬菜",
                "source_specification": "新鲜一级",
                "quoted_unit": "元/斤",
                "net_content": "500克",
            }
        ),
    )

    assert identity.quality_status is QualityStatus.REVIEW_REQUIRED
    assert identity.rejection_code == "IDENTITY_VALUE_UNSUPPORTED"


def test_fresh_rule_registry_contains_only_implemented_profiles() -> None:
    registry = build_fresh_food_rule_registry()

    assert isinstance(registry.get("fresh-apple", "1"), FreshAppleRule)
    assert isinstance(registry.get("packaged-egg", "1"), PackagedEggRule)
    assert isinstance(registry.get("fresh-pork", "1"), FreshPorkRule)
    assert isinstance(
        registry.get("government-fresh", "1"), GovernmentFreshCommodityRule
    )
    with pytest.raises(ValueError, match="not registered"):
        registry.get("fresh-milk", "1")
