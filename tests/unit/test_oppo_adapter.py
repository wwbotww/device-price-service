import json
from datetime import datetime
from decimal import Decimal
from pathlib import Path

import pytest

from device_price_service.crawlers.oppo import OPPO_SNAPSHOT_PLAN, OppoAdapter
from device_price_service.domain.crawl import DiscoveredProduct, FetchResult
from device_price_service.domain.enums import FetchMethod, OriginalPriceType
from device_price_service.domain.price_policy import ConditionalPriceError

FIXTURES = Path(__file__).parents[1] / "fixtures" / "oppo"
PRODUCT_URL = "https://www.opposhop.cn/cn/web/products/43001.html"


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


def test_oppo_discovers_core_devices_and_excludes_oneplus_services_and_external_links() -> None:
    items = OppoAdapter.parse_discovery(
        (FIXTURES / "discovery.html").read_bytes(), "https://www.opposhop.cn/"
    )

    assert [(item.official_product_id, item.category_code) for item in items] == [
        ("43001", "PHONE"),
        ("43002", "TABLET"),
        ("43003", "WATCH"),
    ]


def test_oppo_snapshot_dimensions_allow_watch_without_version() -> None:
    assert all(dimension.optional for dimension in OPPO_SNAPSHOT_PLAN.dimensions)


def test_oppo_discovers_current_oapi_products() -> None:
    body = json.dumps(
        {
            "code": 200,
            "data": [
                {
                    "goodsSpuId": 25642,
                    "skuId": 40697,
                    "goodsSpuName": "OPPO Reno16 Pro",
                },
                {"goodsSpuId": 1, "skuId": 2, "goodsSpuName": "一加 Fixture"},
            ],
        }
    ).encode()

    items = OppoAdapter.parse_discovery(body, "https://www.opposhop.cn/")

    assert [(item.official_product_id, item.category_code) for item in items] == [
        ("25642", "PHONE")
    ]
    assert "skuId=40697" in items[0].url


def test_oppo_parses_current_oapi_detail_batch() -> None:
    url = OppoAdapter._detail_url("40697")
    body = json.dumps(
        {
            "schema": "oppo-oapi-detail-batch-v2",
            "responses": [
                {
                    "skuId": 40697,
                    "spuId": 25642,
                    "seoTitle": "OPPO Reno16 Pro",
                    "product_name": "OPPO Reno16 Pro 怦然星动 16GB+512GB",
                    "price": "5299",
                    "original_price": "5599",
                    "product_status": 0,
                    "bty_type": "立即购买",
                    "color": "怦然星动",
                    "config": "16GB+512GB",
                }
            ],
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
        official_product_id="25642",
        url=url,
        category_code="PHONE",
    )
    adapter = OppoAdapter()

    product = adapter.normalize(item, adapter.parse_product(item, result))

    assert product.name == "OPPO Reno16 Pro"
    assert product.skus[0].official_sku_id == "40697"
    assert product.skus[0].memory == "16GB"
    assert product.skus[0].capacity == "512GB"
    assert product.skus[0].offers[0].current_price == Decimal("5299.00")
    assert product.skus[0].offers[0].original_price == Decimal("5599.00")


def test_oppo_uses_discovery_name_when_variant_seo_titles_differ() -> None:
    url = OppoAdapter._detail_url("40697")
    body = json.dumps(
        {
            "schema": "oppo-oapi-detail-batch-v2",
            "responses": [
                {
                    "skuId": 40697,
                    "spuId": 25642,
                    "seoTitle": "OPPO Reno16 Pro 标准版",
                    "price": "5299",
                },
                {
                    "skuId": 40698,
                    "spuId": 25642,
                    "seoTitle": "OPPO Reno16 Pro 柔光版",
                    "price": "5499",
                },
            ],
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
        official_product_id="25642",
        url=url,
        category_code="PHONE",
        metadata={"discovered_name": "OPPO Reno16 Pro"},
    )
    adapter = OppoAdapter()

    product = adapter.normalize(item, adapter.parse_product(item, result))

    assert product.name == "OPPO Reno16 Pro"
    assert [sku.official_sku_id for sku in product.skus] == ["40697", "40698"]


def test_oppo_normalizes_direct_sku_prices() -> None:
    adapter = OppoAdapter()
    item = DiscoveredProduct(official_product_id="43001", url=PRODUCT_URL, category_code="PHONE")
    product = adapter.normalize(
        item, adapter.parse_product(item, _result("product_snapshots.json"))
    )

    assert len(product.skus) == 2
    assert product.skus[0].memory == "12GB"
    assert product.skus[0].capacity == "256GB"
    assert product.skus[0].offers[0].current_price == Decimal("5999.00")
    assert product.skus[1].offers[0].original_price == Decimal("6999.00")
    assert product.skus[1].offers[0].original_price_type is OriginalPriceType.CROSSED_OUT


def test_oppo_rejects_coupon_selected_price() -> None:
    adapter = OppoAdapter()
    item = DiscoveredProduct(official_product_id="43001", url=PRODUCT_URL, category_code="PHONE")
    parsed = adapter.parse_product(item, _result("product_conditional_price.json"))

    with pytest.raises(ConditionalPriceError, match="conditional price"):
        adapter.normalize(item, parsed)
