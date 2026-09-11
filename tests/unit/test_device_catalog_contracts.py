from decimal import Decimal

import pytest
from pydantic import ValidationError

from device_price_service.domain.catalog_crawl import (
    DiscoveredCatalogListing,
    DiscoveredCatalogProduct,
    ParsedCatalogListing,
    ParsedCatalogProduct,
    ParsedCatalogRow,
    SourceMerchant,
    SourcePriceCandidate,
)
from device_price_service.domain.catalog_enums import (
    Availability,
    FeeStatus,
    PriceNature,
    PriceType,
    PricingBasis,
    QualityStatus,
    SellerType,
    VerificationStatus,
)
from device_price_service.normalization.devices import (
    DeviceCategoryRule,
    DeviceSpecification,
    device_item_key,
    device_listing_key,
)


def _row(*, sku_id: str | None = "sku-1", color: str = "黑色") -> ParsedCatalogRow:
    spec = DeviceSpecification(color=color, capacity="256 GB")
    return ParsedCatalogRow(
        item=DiscoveredCatalogListing(
            listing_key=device_listing_key(product_id="phone-1", sku_id=sku_id, specification=spec),
            external_product_id="phone-1",
            external_sku_id=sku_id,
            url="https://device.example.test/phone-1",
            category_code="PHONE",
            price_nature=PriceNature.RETAIL_OFFER,
            merchant=SourceMerchant(
                merchant_key="official",
                name="官方直营",
                seller_type=SellerType.BRAND_OFFICIAL,
                verification_status=VerificationStatus.VERIFIED,
            ),
        ),
        parsed=ParsedCatalogListing(
            source_title="测试手机 黑色 256GB",
            source_attributes={"device_specification": spec.model_dump()},
            price_candidates=[
                SourcePriceCandidate(
                    current_price=Decimal("5999"),
                    price_type=PriceType.DIRECT_UNCONDITIONAL,
                    pricing_basis=PricingBasis.PACKAGE_TOTAL,
                    availability=Availability.ON_SALE,
                    fee_status=FeeStatus.ITEM_ONLY,
                )
            ],
        ),
    )


def _product(rows: list[ParsedCatalogRow]) -> ParsedCatalogProduct:
    return ParsedCatalogProduct(
        external_product_id="phone-1",
        category_code="PHONE",
        brand_code=" test ",
        name="测试手机",
        rows=rows,
    )


def test_product_contract_preserves_shared_multi_sku_rows_and_discovery() -> None:
    product = _product([_row(), _row(sku_id="sku-2", color="白色")])
    discovered = DiscoveredCatalogProduct(
        external_product_id="phone-1",
        url="https://device.example.test/phone-1",
        category_code="phone",
        metadata={"configuration_family": "standard"},
    )
    product.validate_discovery(discovered)
    assert product.brand_code == "TEST"
    assert len(product.rows) == 2
    assert ParsedCatalogProduct.model_validate_json(product.model_dump_json()) == product
    with pytest.raises(ValueError, match="discovery identity"):
        product.validate_discovery(discovered.model_copy(update={"external_product_id": "other"}))


@pytest.mark.parametrize("change", ["product", "category", "listing", "sku"])
def test_product_contract_rejects_conflicting_sku_membership(change: str) -> None:
    first, second = _row(), _row(sku_id="sku-2")
    updates: dict[str, object] = {
        "product": {"external_product_id": "other"},
        "category": {"category_code": "TABLET"},
        "listing": {"listing_key": first.item.listing_key},
        "sku": {"external_sku_id": first.item.external_sku_id},
    }[change]
    second = second.model_copy(update={"item": second.item.model_copy(update=updates)})
    with pytest.raises(ValidationError):
        _product([first, second])


def test_device_rule_has_stable_identity_without_marketing_or_price_fields() -> None:
    row = _row(sku_id=None)
    rule = DeviceCategoryRule()
    before = rule.normalize(row.item, row.parsed)
    after = rule.normalize(row.item, row.parsed.model_copy(update={"source_title": "全新宣传标题"}))
    assert before.quality_status is QualityStatus.ACCEPTED
    assert before.base_unit == "PIECE"
    assert before.base_quantity_value == Decimal(1)
    assert before.build_fingerprint() == after.build_fingerprint()
    assert before.normalized_attributes["capacity"] == "256GB"
    assert "memory" not in before.normalized_attributes
    changed = _row(sku_id=None, color="白色")
    assert row.item.listing_key != changed.item.listing_key
    assert (
        before.build_fingerprint()
        != rule.normalize(
            changed.item,
            changed.parsed,
        ).build_fingerprint()
    )


def test_external_sku_is_source_identity_but_not_a_configuration_override() -> None:
    first, changed = _row(), _row(color="白色")
    assert first.item.listing_key == changed.item.listing_key
    rule = DeviceCategoryRule()
    assert (
        rule.normalize(first.item, first.parsed).build_fingerprint()
        != rule.normalize(
            changed.item,
            changed.parsed,
        ).build_fingerprint()
    )


def test_device_identity_keeps_part_numbers_and_full_labelled_dimensions() -> None:
    first = DeviceSpecification(
        manufacturer_part_number="MX123CH/A",
        attributes={"版本": "标准版", "款式": "柔光版", "Configuration_Band": "回环式表带"},
    )
    second = DeviceSpecification(
        manufacturer_part_number="MX123CH/A",
        attributes={"configuration_band": "回环式表带", "款式": "柔光版", "版本": "标准版"},
    )
    assert first == second
    assert first.identity_attributes()["manufacturer_part_number"] == "MX123CH/A"
    assert first.identity_attributes()["attributes"]["款式"] == "柔光版"


@pytest.mark.parametrize(
    "values",
    [
        {},
        {"color": " "},
        {"color": "黑色", "price": "5999"},
        {"attributes": {"stock": "1"}},
        {"attributes": {"版本": ""}},
        {"attributes": {"Color": "黑色", " color ": "白色"}},
    ],
)
def test_unproven_or_mutable_specifications_are_rejected(values: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        DeviceSpecification.model_validate(values)


def test_device_rule_keeps_missing_specification_in_review() -> None:
    row = _row()
    identity = DeviceCategoryRule().normalize(
        row.item,
        row.parsed.model_copy(update={"source_attributes": {}}),
    )
    assert identity.quality_status is QualityStatus.REVIEW_REQUIRED
    assert identity.rejection_code == "DEVICE_SPECIFICATION_UNPROVEN"


def test_device_keys_are_namespaced_and_do_not_depend_on_titles() -> None:
    first = device_item_key(brand_code="TEST", channel_code="OFFICIAL", product_id="A:B")
    assert first == device_item_key(brand_code=" test ", channel_code="official", product_id="A:B")
    assert first != device_item_key(brand_code="OTHER", channel_code="OFFICIAL", product_id="A:B")
    assert first != device_item_key(brand_code="TEST", channel_code="OTHER", product_id="A:B")
    with pytest.raises(ValueError, match="official product ID"):
        device_item_key(brand_code="TEST", channel_code="OFFICIAL", product_id=" ")
