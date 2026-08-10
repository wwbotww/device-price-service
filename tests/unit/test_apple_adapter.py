from datetime import datetime
from decimal import Decimal
from pathlib import Path

import pytest

from device_price_service.crawlers.apple import AppleAdapter
from device_price_service.domain.crawl import DiscoveredProduct, FetchResult
from device_price_service.domain.enums import Availability, FetchMethod, OriginalPriceType
from device_price_service.domain.price_policy import ConditionalPriceError

FIXTURES = Path(__file__).parents[1] / "fixtures" / "apple"
PRODUCT_URL = "https://www.apple.com.cn/shop/buy-iphone/iphone-fixture-pro"


def _result(filename: str, url: str = PRODUCT_URL) -> FetchResult:
    return FetchResult(
        request_url=url,
        final_url=url,
        status_code=200,
        headers={"content-type": "text/html; charset=utf-8"},
        body=(FIXTURES / filename).read_bytes(),
        fetched_at=datetime(2026, 8, 8, 8),
        duration_ms=10,
        fetch_method=FetchMethod.REPLAY,
    )


def test_apple_discovers_only_family_pages() -> None:
    items = AppleAdapter.parse_discovery(
        (FIXTURES / "discovery_iphone.html").read_bytes(),
        "https://www.apple.com.cn/shop/buy-iphone",
        "PHONE",
    )

    assert [(item.official_product_id, item.category_code) for item in items] == [
        ("iphone-fixture-pro", "PHONE"),
        ("iphone-fixture-air", "PHONE"),
    ]


def test_apple_classifies_computers_and_excludes_displays() -> None:
    items = AppleAdapter.parse_discovery(
        (FIXTURES / "discovery_mac.html").read_bytes(),
        "https://www.apple.com.cn/shop/buy-mac",
        "COMPUTER",
    )

    assert [(item.official_product_id, item.category_code) for item in items] == [
        ("macbook-air", "LAPTOP"),
        ("imac", "DESKTOP"),
        ("mac-mini", "DESKTOP"),
    ]


def test_apple_parses_and_normalizes_direct_sku_prices() -> None:
    adapter = AppleAdapter()
    item = DiscoveredProduct(
        official_product_id="iphone-fixture-pro",
        url=PRODUCT_URL,
        category_code="PHONE",
    )
    parsed = adapter.parse_product(item, _result("product_iphone.html"))
    product = adapter.normalize(item, parsed)

    assert product.name == "iPhone Fixture Pro"
    assert len(product.skus) == 3
    assert product.skus[0].official_sku_id == "FX256BLUECH/A"
    assert product.skus[0].capacity == "256GB"
    assert product.skus[0].offers[0].current_price == Decimal("8999.00")
    assert product.skus[0].offers[0].original_price is None
    assert product.skus[1].offers[0].original_price == Decimal("10999.00")
    assert (
        product.skus[1].offers[0].original_price_type
        is OriginalPriceType.EXPLICIT_ORIGINAL
    )
    assert product.skus[2].offers[0].availability is Availability.OUT_OF_STOCK
    assert len({sku.spec_fingerprint for sku in product.skus}) == 3


def test_apple_rejects_conditional_selected_price() -> None:
    adapter = AppleAdapter()
    url = "https://www.apple.com.cn/shop/buy-iphone/iphone-fixture-conditional"
    item = DiscoveredProduct(
        official_product_id="iphone-fixture-conditional",
        url=url,
        category_code="PHONE",
    )
    parsed = adapter.parse_product(item, _result("product_conditional_price.html", url))

    with pytest.raises(ConditionalPriceError, match="conditional price"):
        adapter.normalize(item, parsed)


def test_apple_preserves_cross_category_configuration_dimensions() -> None:
    adapter = AppleAdapter()
    url = "https://www.apple.com.cn/shop/buy-mac/macbook-fixture"
    item = DiscoveredProduct(
        official_product_id="macbook-fixture",
        url=url,
        category_code="LAPTOP",
    )
    parsed = adapter.parse_product(item, _result("product_macbook.html", url))
    product = adapter.normalize(item, parsed)
    sku = product.skus[0]

    assert product.category_code == "LAPTOP"
    assert sku.memory == "16GB"
    assert sku.capacity == "512GB"
    assert sku.size == "14 英寸"
    assert sku.attributes["processor"] == "Fixture Pro 芯片"
