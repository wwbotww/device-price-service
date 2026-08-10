from decimal import Decimal

from device_price_service.domain.crawl import (
    NormalizedOffer,
    NormalizedProduct,
    NormalizedSku,
)
from device_price_service.domain.enums import Availability, OriginalPriceType
from device_price_service.validation.rules import QualityValidator, Severity


def _product(*, url: str = "https://shop.example.cn/p/1") -> NormalizedProduct:
    return NormalizedProduct(
        brand_code="APPLE",
        channel_code="APPLE_CN_WEB",
        category_code="PHONE",
        official_product_id="p1",
        name="Device",
        official_url=url,
        skus=[
            NormalizedSku(
                official_sku_id="sku1",
                name="Device 256GB",
                spec_fingerprint="a" * 64,
                offers=[
                    NormalizedOffer(
                        official_offer_id="offer1",
                        source_url=url,
                        original_price=Decimal("9999"),
                        original_price_type=OriginalPriceType.MSRP,
                        current_price=Decimal("8999"),
                        availability=Availability.ON_SALE,
                    )
                ],
            )
        ],
    )


def test_quality_validator_accepts_valid_product() -> None:
    report = QualityValidator(price_change_threshold=0.3).validate(
        _product(),
        expected_brand_code="APPLE",
        expected_channel_code="APPLE_CN_WEB",
        allowed_domains=["shop.example.cn"],
    )
    assert report.is_valid
    assert report.issues == ()


def test_quality_validator_rejects_external_url() -> None:
    report = QualityValidator(price_change_threshold=0.3).validate(
        _product(url="https://evil.example/p/1"),
        expected_brand_code="APPLE",
        expected_channel_code="APPLE_CN_WEB",
        allowed_domains=["shop.example.cn"],
    )
    assert not report.is_valid
    assert {issue.code for issue in report.issues if issue.severity is Severity.ERROR} == {
        "URL_NOT_ALLOWED"
    }


def test_quality_validator_warns_on_large_price_change() -> None:
    report = QualityValidator(price_change_threshold=0.3).validate(
        _product(),
        expected_brand_code="APPLE",
        expected_channel_code="APPLE_CN_WEB",
        allowed_domains=["shop.example.cn"],
        previous_prices={"a" * 64: Decimal("5000")},
    )
    assert report.is_valid
    assert "LARGE_PRICE_CHANGE" in {issue.code for issue in report.issues}
