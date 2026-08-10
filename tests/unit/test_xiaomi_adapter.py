from datetime import datetime
from decimal import Decimal
from pathlib import Path

import pytest

from device_price_service.crawlers.xiaomi import XIAOMI_SNAPSHOT_PLAN, XiaomiAdapter
from device_price_service.domain.crawl import DiscoveredProduct, FetchResult
from device_price_service.domain.enums import Availability, FetchMethod, OriginalPriceType
from device_price_service.domain.price_policy import ConditionalPriceError

FIXTURES = Path(__file__).parents[1] / "fixtures" / "xiaomi"
PRODUCT_URL = "https://www.mi.com/shop/buy/detail?product_id=91002"


def _result(filename: str) -> FetchResult:
    return FetchResult(
        request_url=PRODUCT_URL,
        final_url=PRODUCT_URL,
        status_code=200,
        headers={
            "content-type": "application/json",
            "x-device-price-artifact": "browser-snapshots-v1",
        },
        body=(FIXTURES / filename).read_bytes(),
        fetched_at=datetime(2026, 8, 8, 8),
        duration_ms=20,
        fetch_method=FetchMethod.REPLAY,
    )


def test_xiaomi_discovers_only_core_xiaomi_devices() -> None:
    items = XiaomiAdapter.parse_discovery(
        (FIXTURES / "discovery.html").read_bytes(),
        "https://www.mi.com/shop/",
    )

    assert [(item.official_product_id, item.category_code) for item in items] == [
        ("91001", "PHONE"),
        ("91002", "TABLET"),
        ("91003", "LAPTOP"),
        ("91004", "WATCH"),
    ]


def test_xiaomi_snapshot_plan_excludes_bundles_and_services() -> None:
    assert [dimension.name for dimension in XIAOMI_SNAPSHOT_PLAN.dimensions] == [
        "version",
        "color",
    ]
    assert [option.value for option in XIAOMI_SNAPSHOT_PLAN.fixed_options] == ["标准版"]


def test_xiaomi_parses_and_normalizes_rendered_variant_snapshots() -> None:
    adapter = XiaomiAdapter()
    item = DiscoveredProduct(
        official_product_id="91002",
        url=PRODUCT_URL,
        category_code="TABLET",
    )
    parsed = adapter.parse_product(item, _result("product_snapshots.json"))
    product = adapter.normalize(item, parsed)

    assert product.name == "Xiaomi Pad Fixture"
    assert len(product.skus) == 3
    assert product.skus[0].memory == "8GB"
    assert product.skus[0].capacity == "128GB"
    assert product.skus[0].offers[0].current_price == Decimal("2999.00")
    assert product.skus[0].offers[0].original_price is None
    assert product.skus[1].offers[0].original_price == Decimal("3499.00")
    assert product.skus[1].offers[0].original_price_type is OriginalPriceType.CROSSED_OUT
    assert product.skus[2].attributes["edition"] == "柔光版"
    assert product.skus[2].offers[0].availability is Availability.OUT_OF_STOCK
    assert all(sku.attributes["bundle"] == "标准版" for sku in product.skus)


def test_xiaomi_ignores_conditional_marketing_copy_but_rejects_conditional_price() -> None:
    adapter = XiaomiAdapter()
    item = DiscoveredProduct(
        official_product_id="91002",
        url=PRODUCT_URL,
        category_code="TABLET",
    )
    normal = adapter.normalize(item, adapter.parse_product(item, _result("product_snapshots.json")))
    assert normal.skus[0].offers[0].current_price == Decimal("2999.00")

    conditional = adapter.parse_product(item, _result("product_conditional_price.json"))
    with pytest.raises(ConditionalPriceError, match="conditional price"):
        adapter.normalize(item, conditional)
