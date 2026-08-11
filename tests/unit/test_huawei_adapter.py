import json
from datetime import datetime
from decimal import Decimal
from pathlib import Path

import pytest

from device_price_service.crawlers.huawei import HUAWEI_SNAPSHOT_PLAN, HuaweiAdapter
from device_price_service.domain.crawl import DiscoveredProduct, FetchResult
from device_price_service.domain.enums import Availability, FetchMethod, OriginalPriceType
from device_price_service.domain.price_policy import ConditionalPriceError

FIXTURES = Path(__file__).parents[1] / "fixtures" / "huawei"
PRODUCT_URL = "https://www.vmall.com/product/42002.html"
NEXT_PRODUCT_URL = "https://item.vmall.com/product/comdetail/index.html?prdId=10001&sbomCode=280101"


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


def test_huawei_discovers_only_core_huawei_devices() -> None:
    items = HuaweiAdapter.parse_discovery(
        (FIXTURES / "discovery.html").read_bytes(), "https://www.vmall.com/"
    )

    assert [(item.official_product_id, item.category_code) for item in items] == [
        ("42001", "PHONE"),
        ("42002", "TABLET"),
        ("42003", "LAPTOP"),
        ("42004", "DESKTOP"),
        ("42005", "WATCH"),
    ]


def test_huawei_discovers_current_openapi_products() -> None:
    body = json.dumps(
        {
            "data": [
                {
                    "prdId": 10001,
                    "skuCode": "280101",
                    "prdName": "HUAWEI Mate 80",
                },
                {"prdId": 10002, "skuCode": "280102", "prdName": "HUAWEI WATCH GT 7"},
                {"prdId": 10003, "prdName": "HUAWEI FreeBuds Fixture"},
                {"prdId": 10004, "skuCode": "280104", "prdName": "HUAWEI WATCH 5 表带"},
                {
                    "prdId": 10005,
                    "skuCode": "280105",
                    "prdName": "HUAWEI WATCH 无线超级快充底座(第二代)",
                },
            ]
        }
    ).encode()

    items = HuaweiAdapter.parse_discovery(body, "https://openapi.vmall.com/example")

    assert [(item.official_product_id, item.category_code) for item in items] == [
        ("10001", "PHONE"),
        ("10002", "WATCH"),
    ]
    assert items[0].url == (
        "https://item.vmall.com/product/comdetail/index.html?prdId=10001&sbomCode=280101"
    )


def test_huawei_snapshot_dimensions_are_optional_for_single_spec_devices() -> None:
    assert [dimension.name for dimension in HUAWEI_SNAPSHOT_PLAN.dimensions] == [
        "version",
        "color",
    ]
    assert all(dimension.optional for dimension in HUAWEI_SNAPSHOT_PLAN.dimensions)


def test_huawei_parses_current_next_product_evidence() -> None:
    body = json.dumps(
        {
            "props": {
                "pageProps": {
                    "mainData": {
                        "current": {
                            "name": "HUAWEI MateBook Fold 非凡大师 麒麟X90 Plus",
                            "productOptions": {
                                "sbomList": {
                                    "280101": {
                                        "sbomCode": "280101",
                                        "price": 24999,
                                        "inventory": 0,
                                        "commingSoonFlag": 0,
                                        "subsidyActivity": True,
                                        "estPriceDetail": {"isEstLoad": False},
                                        "gbomAttrList": [
                                            {"attrName": "配置", "attrValue": "24GB/512GB"},
                                            {"attrName": "颜色", "attrValue": "天际白"},
                                        ],
                                    },
                                    "280102": {
                                        "sbomCode": "280102",
                                        "price": 26999,
                                        "inventory": 1,
                                        "commingSoonFlag": 0,
                                        "gbomAttrList": [
                                            {"attrName": "版本", "attrValue": "32GB/1TB"},
                                            {"attrName": "颜色", "attrValue": "天际白"},
                                            {"attrName": "款式", "attrValue": "标准屏"},
                                        ],
                                    },
                                    "280103": {
                                        "sbomCode": "280103",
                                        "price": 27999,
                                        "inventory": 1,
                                        "commingSoonFlag": 0,
                                        "gbomAttrList": [
                                            {"attrName": "版本", "attrValue": "32GB/1TB"},
                                            {"attrName": "颜色", "attrValue": "天际白"},
                                            {"attrName": "款式", "attrValue": "柔光屏"},
                                        ],
                                    },
                                }
                            },
                        }
                    }
                }
            }
        },
        ensure_ascii=False,
    )
    html = f'<html><script id="__NEXT_DATA__" type="application/json">{body}</script></html>'
    result = FetchResult(
        request_url=NEXT_PRODUCT_URL,
        final_url=NEXT_PRODUCT_URL,
        status_code=200,
        headers={"content-type": "text/html"},
        body=html.encode(),
        fetched_at=datetime(2026, 8, 11, 8),
        duration_ms=5,
        fetch_method=FetchMethod.REPLAY,
    )
    adapter = HuaweiAdapter()
    item = DiscoveredProduct(
        official_product_id="10001",
        url=NEXT_PRODUCT_URL,
        category_code="LAPTOP",
    )

    product = adapter.normalize(item, adapter.parse_product(item, result))

    assert len(product.skus) == 3
    assert product.skus[0].official_sku_id == "280101"
    assert product.skus[0].memory == "24GB"
    assert product.skus[0].capacity == "512GB"
    assert product.skus[0].color == "天际白"
    assert product.skus[0].offers[0].current_price == Decimal("24999.00")
    assert product.skus[0].offers[0].original_price is None
    assert product.skus[0].offers[0].availability is Availability.UNKNOWN
    assert product.skus[1].memory == "32GB"
    assert product.skus[1].capacity == "1TB"
    assert product.skus[1].spec_fingerprint != product.skus[0].spec_fingerprint
    assert product.skus[2].spec_fingerprint != product.skus[1].spec_fingerprint
    assert product.skus[2].attributes["official_attributes"]["款式"] == "柔光屏"


def test_huawei_normalizes_direct_prices_and_ignores_subsidy_copy() -> None:
    adapter = HuaweiAdapter()
    item = DiscoveredProduct(official_product_id="42002", url=PRODUCT_URL, category_code="TABLET")
    product = adapter.normalize(
        item, adapter.parse_product(item, _result("product_snapshots.json"))
    )

    assert len(product.skus) == 2
    assert product.skus[0].official_sku_id == "HW-42002-8256-G"
    assert product.skus[0].memory == "8GB"
    assert product.skus[0].capacity == "256GB"
    assert product.skus[0].offers[0].current_price == Decimal("2999.00")
    assert product.skus[0].offers[0].original_price is None
    assert product.skus[1].offers[0].original_price == Decimal("4299.00")
    assert product.skus[1].offers[0].original_price_type is OriginalPriceType.CROSSED_OUT
    assert product.skus[1].offers[0].availability is Availability.OUT_OF_STOCK


def test_huawei_rejects_subsidized_selected_price() -> None:
    adapter = HuaweiAdapter()
    item = DiscoveredProduct(official_product_id="42002", url=PRODUCT_URL, category_code="TABLET")
    parsed = adapter.parse_product(item, _result("product_conditional_price.json"))

    with pytest.raises(ConditionalPriceError, match="conditional price"):
        adapter.normalize(item, parsed)
