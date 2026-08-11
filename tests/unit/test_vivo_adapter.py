import json
from datetime import datetime
from decimal import Decimal
from pathlib import Path

import pytest

from device_price_service.crawlers.vivo import VIVO_SNAPSHOT_PLAN, VivoAdapter
from device_price_service.domain.crawl import DiscoveredProduct, FetchResult
from device_price_service.domain.enums import Availability, FetchMethod, OriginalPriceType
from device_price_service.domain.price_policy import ConditionalPriceError

FIXTURES = Path(__file__).parents[1] / "fixtures" / "vivo"
PRODUCT_URL = "https://shop.vivo.com.cn/product/44001"


def _result(filename: str) -> FetchResult:
    return FetchResult(
        request_url=PRODUCT_URL,
        final_url=PRODUCT_URL,
        status_code=200,
        headers={"content-type": "application/json"},
        body=(FIXTURES / filename).read_bytes(),
        fetched_at=datetime(2026, 8, 10, 8),
        duration_ms=5,
        fetch_method=FetchMethod.REPLAY,
    )


def test_vivo_discovers_main_brand_and_excludes_iqoo_third_party_and_external_links() -> None:
    items = VivoAdapter.parse_discovery(
        (FIXTURES / "discovery.html").read_bytes(), "https://shop.vivo.com.cn/"
    )

    assert [(item.official_product_id, item.category_code) for item in items] == [
        ("44001", "PHONE"),
        ("44002", "TABLET"),
        ("44003", "WATCH"),
    ]


def test_vivo_snapshot_dimensions_are_optional_for_single_spec_devices() -> None:
    assert all(dimension.optional for dimension in VIVO_SNAPSHOT_PLAN.dimensions)


def test_vivo_discovers_current_home_api_products() -> None:
    body = json.dumps(
        {
            "code": 0,
            "data": [
                {"spuId": 10010927, "skuId": 138552, "name": "vivo S30 Pro mini"},
                {"spuId": 10011739, "skuId": 143018, "name": "vivo Pad6 Pro 智能触控键盘"},
                {"spuId": 10037836, "skuId": 195100, "name": "vivo Y60 原装保护膜"},
                {"spuId": 2, "skuId": 3, "name": "iQOO Fixture"},
            ],
        }
    ).encode()

    items = VivoAdapter.parse_discovery(body, "https://shop.vivo.com.cn/")

    assert [(item.official_product_id, item.category_code) for item in items] == [
        ("10010927", "PHONE")
    ]
    assert items[0].url.endswith("getInfo?spuId=10010927")


def test_vivo_parses_current_api_detail_batch() -> None:
    url = VivoAdapter._info_url("10010927")
    body = json.dumps(
        {
            "schema": "vivo-api-detail-batch-v2",
            "commoditySpu": {"id": 10010927, "spuName": "vivo S30 Pro mini"},
            "specItem": {
                "specMainSeq": {"0": "版本", "1": "颜色"},
                "specItemSeq": {
                    "0": [{"name": "16GB+512GB"}],
                    "1": [{"name": "薄荷青"}],
                },
                "skuSpecList": [{"skuId": 138552, "sequences": {"0": "1", "1": "1"}}],
            },
            "downSkuIds": [],
            "zeroStoreSkuIds": [],
            "details": {
                "138552": {
                    "skuName": "vivo S30 Pro mini 16GB+512GB 薄荷青",
                    "colorName": "薄荷青",
                    "salePrice": 3799,
                    "marketPrice": 3999,
                    "marketable": 1,
                    "skuStatus": {"hasStore": 1},
                }
            },
        },
        ensure_ascii=False,
    ).encode()
    result = FetchResult(
        request_url=url,
        final_url=url,
        status_code=200,
        headers={"content-type": "application/json"},
        body=body,
        fetched_at=datetime(2026, 8, 11, 8),
        duration_ms=5,
        fetch_method=FetchMethod.REPLAY,
    )
    item = DiscoveredProduct(
        official_product_id="10010927",
        url=url,
        category_code="PHONE",
    )
    adapter = VivoAdapter()

    product = adapter.normalize(item, adapter.parse_product(item, result))

    assert product.skus[0].official_sku_id == "138552"
    assert product.skus[0].memory == "16GB"
    assert product.skus[0].capacity == "512GB"
    assert product.skus[0].offers[0].current_price == Decimal("3799.00")
    assert product.skus[0].offers[0].original_price == Decimal("3999.00")
    assert product.skus[0].offers[0].availability is Availability.ON_SALE


def test_vivo_normalizes_direct_prices_without_coupon_or_installment_copy() -> None:
    adapter = VivoAdapter()
    item = DiscoveredProduct(official_product_id="44001", url=PRODUCT_URL, category_code="PHONE")
    product = adapter.normalize(
        item, adapter.parse_product(item, _result("product_snapshots.json"))
    )

    assert len(product.skus) == 2
    assert product.skus[0].official_sku_id == "140001"
    assert product.skus[0].offers[0].current_price == Decimal("4799.00")
    assert product.skus[1].offers[0].original_price == Decimal("5799.00")
    assert product.skus[1].offers[0].original_price_type is OriginalPriceType.CROSSED_OUT
    assert product.skus[1].offers[0].availability is Availability.OFF_SHELF


def test_vivo_rejects_trade_in_selected_price() -> None:
    adapter = VivoAdapter()
    item = DiscoveredProduct(official_product_id="44001", url=PRODUCT_URL, category_code="PHONE")
    parsed = adapter.parse_product(item, _result("product_conditional_price.json"))

    with pytest.raises(ConditionalPriceError, match="conditional price"):
        adapter.normalize(item, parsed)
