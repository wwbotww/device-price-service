import asyncio
import json
import re
from dataclasses import replace
from datetime import datetime
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from device_price_service.crawlers.base import AdapterContext
from device_price_service.crawlers.huawei import HuaweiCatalogConnector, HuaweiParseError
from device_price_service.crawlers.xiaomi import XiaomiCatalogConnector, XiaomiParseError
from device_price_service.domain.catalog_crawl import (
    CatalogCollectionRequest,
    DiscoveredCatalogProduct,
)
from device_price_service.domain.catalog_enums import (
    Availability,
    FeeStatus,
    PriceType,
    RegionScope,
)
from device_price_service.domain.crawl import FetchResult
from device_price_service.domain.enums import FetchMethod
from device_price_service.normalization.devices import DeviceCategoryRule
from device_price_service.services.catalog_preparation import prepare_catalog_product

FIXTURES = Path(__file__).parents[1] / "fixtures"


@pytest.fixture(params=["huawei", "xiaomi"])
def source(request):
    brand = request.param
    connector = HuaweiCatalogConnector() if brand == "huawei" else XiaomiCatalogConnector()
    product_id = "42002" if brand == "huawei" else "91002"
    url = (
        "https://www.vmall.com/product/42002.html"
        if brand == "huawei"
        else "https://www.mi.com/shop/buy/detail?product_id=91002"
    )
    item = DiscoveredCatalogProduct(external_product_id=product_id, url=url, category_code="TABLET")
    result = FetchResult(
        request_url=url,
        final_url=url,
        status_code=200,
        headers={"content-type": "application/json"},
        body=(FIXTURES / brand / "product_snapshots.json").read_bytes(),
        fetched_at=datetime(2026, 9, 10),
        duration_ms=1,
        fetch_method=FetchMethod.REPLAY,
    )
    return connector, item, result


def test_native_rows_pass_common_device_preparation(source) -> None:
    connector, item, result = source
    product = connector.parse_product(item, result)
    prepared = prepare_catalog_product(
        product,
        discovered=item,
        expected_brand_code=connector.brand_code,
        request=CatalogCollectionRequest(
            region_scope=RegionScope.NATIONAL, region_code="CN", category_codes=("TABLET",)
        ),
        allowed_domains=connector.allowed_domains,
        rule_for_category=lambda _: DeviceCategoryRule(),
    )
    assert prepared.complete
    assert prepared.accepted_count == len(product.rows)
    assert not hasattr(connector, "normalize")


@pytest.mark.parametrize("field", ["request_url", "final_url"])
def test_product_redirect_or_identity_change_fails(source, field) -> None:
    connector, item, result = source
    wrong_url = item.url.replace(item.external_product_id, "99999")
    with pytest.raises((HuaweiParseError, XiaomiParseError), match="different product"):
        connector.parse_product(item, replace(result, **{field: wrong_url}))


def test_snapshot_envelope_cannot_point_to_another_product(source) -> None:
    connector, item, result = source
    envelope = json.loads(result.body)
    envelope["source_url"] = item.url.replace(item.external_product_id, "99999")
    with pytest.raises((HuaweiParseError, XiaomiParseError), match="different product"):
        connector.parse_product(item, replace(result, body=json.dumps(envelope).encode()))


def test_duplicate_sku_or_spec_is_not_silently_dropped(source) -> None:
    connector, item, result = source
    envelope = json.loads(result.body)
    envelope["snapshots"].append(envelope["snapshots"][0])
    with pytest.raises((HuaweiParseError, XiaomiParseError), match="repeats"):
        connector.parse_product(item, replace(result, body=json.dumps(envelope).encode()))


def test_missing_current_price_does_not_return_a_priced_subset(source) -> None:
    connector, item, result = source
    envelope = json.loads(result.body)
    snapshot = envelope["snapshots"][0]
    snapshot["html"] = re.sub(
        r"<(div|span) class=['\"]current-price['\"]>.*?</\1>", "", snapshot["html"]
    )
    with pytest.raises((HuaweiParseError, XiaomiParseError), match="no direct price"):
        connector.parse_product(item, replace(result, body=json.dumps(envelope).encode()))


def test_explicit_unavailable_without_amount_is_a_state_only_fact(source) -> None:
    connector, item, result = source
    envelope = json.loads(result.body)
    snapshot = envelope["snapshots"][-1]
    snapshot["html"] = re.sub(
        r"<(div|span) class=['\"]current-price['\"]>.*?</\1>", "", snapshot["html"]
    )
    product = connector.parse_product(item, replace(result, body=json.dumps(envelope).encode()))
    candidate = product.rows[-1].parsed.price_candidates[0]
    assert candidate.price_type is PriceType.AVAILABILITY_ONLY
    assert candidate.availability is Availability.OUT_OF_STOCK
    assert candidate.current_price is None
    assert candidate.original_price is None
    assert candidate.unit_price is None
    assert candidate.fee_status is FeeStatus.NOT_APPLICABLE
    assert candidate.evidence_hash is None and candidate.source_observed_at is None


def test_discovery_filters_requested_category_and_uses_one_product_scope(source) -> None:
    connector, _, result = source
    brand = connector.brand_code.lower()
    fetcher = AsyncMock()
    fetcher.fetch.return_value = replace(
        result,
        request_url=connector.discovery_url,
        final_url=connector.discovery_url,
        body=(FIXTURES / brand / "discovery.html").read_bytes(),
    )
    context = AdapterContext(
        http=fetcher, browser=AsyncMock(), allowed_domains=list(connector.allowed_domains)
    )
    products = asyncio.run(
        connector.discover_products(
            context,
            CatalogCollectionRequest(
                region_scope=RegionScope.NATIONAL, region_code="CN", category_codes=("TABLET",)
            ),
        )
    )
    assert len(products) == 1
    assert products[0].category_code == "TABLET"
    fetcher.fetch.assert_awaited_once()


def test_non_national_scope_is_rejected_before_fetch(source) -> None:
    connector, _, _ = source
    fetcher = AsyncMock()
    context = AdapterContext(
        http=fetcher, browser=AsyncMock(), allowed_domains=list(connector.allowed_domains)
    )
    with pytest.raises((HuaweiParseError, XiaomiParseError), match="NATIONAL/CN"):
        asyncio.run(
            connector.discover_products(
                context,
                CatalogCollectionRequest(region_scope=RegionScope.CITY, region_code="310100"),
            )
        )
    fetcher.fetch.assert_not_called()


@pytest.mark.parametrize("invalid", [None, {}, {"attrName": "颜色", "attrValue": None}])
def test_huawei_next_attributes_reject_missing_configuration(invalid) -> None:
    with pytest.raises(HuaweiParseError):
        HuaweiCatalogConnector._next_attributes(invalid if invalid is None else [invalid])


def test_huawei_next_attributes_reject_duplicate_labels() -> None:
    with pytest.raises(HuaweiParseError, match="duplicate"):
        HuaweiCatalogConnector._next_attributes(
            [
                {"attrName": "款式", "attrValue": "标准屏"},
                {"attrName": "款式", "attrValue": "柔光屏"},
            ]
        )


def _huawei_next_response(second_sku: object):
    url = "https://item.vmall.com/product/comdetail/index.html?prdId=42002&sbomCode=HW1"
    item = DiscoveredCatalogProduct(external_product_id="42002", url=url, category_code="TABLET")
    first_sku = {
        "sbomCode": "HW1",
        "price": 2999,
        "inventory": 0,
        "gbomAttrList": [{"attrName": "版本", "attrValue": "8GB+256GB"}],
    }
    payload = {
        "props": {
            "pageProps": {
                "mainData": {
                    "current": {
                        "name": "HUAWEI MatePad Fixture",
                        "prdId": "42002",
                        "productOptions": {"sbomList": {"first": first_sku, "second": second_sku}},
                    }
                }
            }
        }
    }
    body = f'<script id="__NEXT_DATA__">{json.dumps(payload)}</script>'.encode()
    result = FetchResult(
        request_url=url,
        final_url=url,
        status_code=200,
        headers={"content-type": "text/html"},
        body=body,
        fetched_at=datetime(2026, 9, 10),
        duration_ms=1,
        fetch_method=FetchMethod.REPLAY,
    )
    return item, result


@pytest.mark.parametrize(
    "second_sku",
    [
        None,
        {"sbomCode": None, "price": 2999},
        {"sbomCode": "HW1", "price": 2999},
        {"sbomCode": "HW2", "gbomAttrList": [{"attrName": "颜色", "attrValue": "黑色"}]},
    ],
)
def test_huawei_next_malformed_sku_prevents_returning_a_priced_subset(second_sku) -> None:
    item, result = _huawei_next_response(second_sku)
    with pytest.raises(HuaweiParseError):
        HuaweiCatalogConnector().parse_product(item, result)


def test_huawei_next_coming_soon_without_amount_preserves_only_explicit_state() -> None:
    item, result = _huawei_next_response(
        {
            "sbomCode": "HW2",
            "commingSoonFlag": 1,
            "gbomAttrList": [{"attrName": "款式", "attrValue": "柔光屏"}],
        }
    )
    product = HuaweiCatalogConnector().parse_product(item, result)
    candidate = product.rows[1].parsed.price_candidates[0]
    assert candidate.price_type is PriceType.AVAILABILITY_ONLY
    assert candidate.availability is Availability.COMING_SOON
    assert candidate.current_price is None and candidate.original_price is None
