import asyncio
import json
from dataclasses import replace
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from device_price_service.crawlers.base import AdapterContext
from device_price_service.crawlers.xiaomi import (
    XIAOMI_SHOP_URL,
    XIAOMI_SNAPSHOT_PLAN,
    XiaomiCatalogConnector,
    XiaomiParseError,
)
from device_price_service.domain.catalog_crawl import (
    CatalogCollectionRequest,
    DiscoveredCatalogProduct,
)
from device_price_service.domain.catalog_enums import RegionScope
from device_price_service.domain.crawl import FetchResult
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
    items = XiaomiCatalogConnector.parse_discovery(
        (FIXTURES / "discovery.html").read_bytes(),
        XIAOMI_SHOP_URL,
    )

    assert [(item.external_product_id, item.category_code) for item in items] == [
        ("91001", "PHONE"),
        ("91002", "TABLET"),
        ("91003", "LAPTOP"),
        ("91004", "WATCH"),
    ]


def _discovery_result() -> FetchResult:
    return FetchResult(
        request_url="https://www.mi.com/shop",
        final_url="https://www.mi.com/shop",
        status_code=200,
        headers={"content-type": "text/html"},
        body=(FIXTURES / "discovery.html").read_bytes(),
        fetched_at=datetime(2026, 9, 11, 5),
        duration_ms=20,
        fetch_method=FetchMethod.HTTP,
    )


def test_xiaomi_discovery_requests_canonical_shop_without_redirect() -> None:
    connector = XiaomiCatalogConnector()
    http = AsyncMock()
    http.fetch.return_value = _discovery_result()
    context = AdapterContext(http=http, browser=AsyncMock(), allowed_domains=["www.mi.com"])

    products = asyncio.run(
        connector.discover_products(
            context,
            CatalogCollectionRequest(region_scope=RegionScope.NATIONAL, region_code="CN"),
        )
    )

    assert connector.discovery_url == "https://www.mi.com/shop"
    http.fetch.assert_awaited_once_with("https://www.mi.com/shop", allowed_domains=["www.mi.com"])
    assert len(products) == 4


@pytest.mark.parametrize("field", ["request_url", "final_url"])
@pytest.mark.parametrize("changed_url", ["https://www.mi.com/shop/", "https://example.com/shop"])
def test_xiaomi_discovery_still_rejects_changed_response_url(field: str, changed_url: str) -> None:
    connector = XiaomiCatalogConnector()
    http = AsyncMock()
    http.fetch.return_value = replace(_discovery_result(), **{field: changed_url})
    context = AdapterContext(http=http, browser=AsyncMock(), allowed_domains=["www.mi.com"])

    with pytest.raises(XiaomiParseError, match="changed its requested URL"):
        asyncio.run(
            connector.discover_products(
                context,
                CatalogCollectionRequest(region_scope=RegionScope.NATIONAL, region_code="CN"),
            )
        )


def test_xiaomi_snapshot_plan_enumerates_only_device_specs_and_colors() -> None:
    assert [dimension.name for dimension in XIAOMI_SNAPSHOT_PLAN.dimensions] == [
        "color",
        "version",
    ]
    assert XIAOMI_SNAPSHOT_PLAN.fixed_options == ()
    assert XIAOMI_SNAPSHOT_PLAN.dimensions[1].heading_text == ("选择规格", "选择版本")
    assert XIAOMI_SNAPSHOT_PLAN.dimensions[1].optional is True


def test_xiaomi_parses_current_slash_separated_laptop_spec() -> None:
    assert XiaomiCatalogConnector._version_attributes("Ultra5-325/24GB/1TB") == (
        "24GB",
        "1TB",
        "Ultra5-325",
    )


def test_xiaomi_accepts_single_spec_device_without_version_dimension() -> None:
    html = (
        "<div class='product-con'><h2>Xiaomi Watch S5</h2>"
        "<div class='price-info'><span class='current-price'>1299</span></div></div>"
    )
    body = json.dumps(
        {
            "schema": "device-price-browser-snapshots-v1",
            "source_url": PRODUCT_URL,
            "snapshots": [{"selections": {"color": "黑色"}, "html": html}],
        },
        ensure_ascii=False,
    ).encode()
    result = FetchResult(
        request_url=PRODUCT_URL,
        final_url=PRODUCT_URL,
        status_code=200,
        headers={"content-type": "application/json"},
        body=body,
        fetched_at=datetime(2026, 8, 11, 8),
        duration_ms=5,
        fetch_method=FetchMethod.REPLAY,
    )
    item = DiscoveredCatalogProduct(
        external_product_id="91002",
        url=PRODUCT_URL,
        category_code="WATCH",
    )
    adapter = XiaomiCatalogConnector()

    product = adapter.parse_product(item, result)

    assert product.rows[0].parsed.source_attributes["device_specification"] == {"color": "黑色"}
    assert product.rows[0].parsed.price_candidates[0].current_price == Decimal("1299.00")


def test_xiaomi_parses_and_normalizes_rendered_variant_snapshots() -> None:
    adapter = XiaomiCatalogConnector()
    item = DiscoveredCatalogProduct(
        external_product_id="91002",
        url=PRODUCT_URL,
        category_code="TABLET",
    )
    product = adapter.parse_product(item, _result("product_snapshots.json"))

    assert product.name == "Xiaomi Pad Fixture"
    assert len(product.rows) == 3
    specs = [row.parsed.source_attributes["device_specification"] for row in product.rows]
    prices = [row.parsed.price_candidates[0] for row in product.rows]
    assert specs[0]["memory"] == "8GB"
    assert specs[0]["capacity"] == "128GB"
    assert prices[0].current_price == Decimal("2999.00")
    assert prices[0].original_price is None
    assert prices[1].original_price == Decimal("3499.00")
    assert prices[1].original_price_type is OriginalPriceType.CROSSED_OUT
    assert specs[2]["edition"] == "柔光版"
    assert prices[2].availability is Availability.OUT_OF_STOCK
    assert all(row.item.external_sku_id is None for row in product.rows)
    assert all('"spec"' in row.item.listing_key for row in product.rows)
    assert all(price.source_observed_at is None and price.evidence_hash is None for price in prices)
    assert all("bundle" not in spec.get("attributes", {}) for spec in specs)


def test_xiaomi_ignores_conditional_marketing_copy_but_rejects_conditional_price() -> None:
    adapter = XiaomiCatalogConnector()
    item = DiscoveredCatalogProduct(
        external_product_id="91002",
        url=PRODUCT_URL,
        category_code="TABLET",
    )
    normal = adapter.parse_product(item, _result("product_snapshots.json"))
    assert normal.rows[0].parsed.price_candidates[0].current_price == Decimal("2999.00")

    with pytest.raises(ConditionalPriceError, match="conditional price"):
        adapter.parse_product(item, _result("product_conditional_price.json"))
