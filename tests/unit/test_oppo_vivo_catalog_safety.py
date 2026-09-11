import asyncio
import json
from dataclasses import replace
from datetime import datetime, timedelta
from hashlib import sha256
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest

from device_price_service.crawlers.base import AdapterContext
from device_price_service.crawlers.oppo import OppoCatalogConnector, OppoParseError
from device_price_service.crawlers.vivo import VivoCatalogConnector, VivoParseError
from device_price_service.domain.catalog_crawl import (
    CatalogCollectionRequest,
    DiscoveredCatalogProduct,
)
from device_price_service.domain.catalog_enums import (
    Availability,
    PriceType,
    RegionScope,
)
from device_price_service.domain.crawl import FetchResult
from device_price_service.domain.enums import FetchMethod
from device_price_service.normalization.devices import DeviceCategoryRule
from device_price_service.services.catalog_preparation import prepare_catalog_product

FIXTURES = Path(__file__).parents[1] / "fixtures"
NOW = datetime(2026, 9, 11, 8)


def _result(url, payload, *, status=200, offset=0):
    return FetchResult(
        request_url=url,
        final_url=url,
        status_code=status,
        headers={"content-type": "application/json"},
        body=json.dumps(payload, ensure_ascii=False).encode(),
        fetched_at=NOW + timedelta(seconds=offset),
        duration_ms=5,
        fetch_method=FetchMethod.REPLAY,
    )


def _oppo_current(sku="1"):
    return {
        "spuId": "10",
        "skuId": sku,
        "seoTitle": "OPPO Reno Fixture",
        "price": "3999",
        "original_price": "4299",
        "bty_type": "立即购买",
        "color": "黑" if sku == "1" else "白",
        "config": "12GB+256GB",
        "skuAttributes": {"key1": "黑" if sku == "1" else "白", "key2": "12GB+256GB"},
        "attributeList": [
            {
                "value": [
                    {
                        "key": "key1",
                        "_$text1": "颜色",
                        "items": [
                            {"skuId": "1", "attributes": {"key1": "黑", "key2": "12GB+256GB"}},
                            {"skuId": "2", "attributes": {"key1": "白", "key2": "12GB+256GB"}},
                        ],
                    },
                    {"key": "key2", "_$text1": "版本"},
                ]
            }
        ],
    }


def _vivo_spec():
    return {
        "specMainSeq": {"0": "版本", "1": "颜色", "2": "网络"},
        "specItemSeq": {
            "0": [{"name": "12GB+256GB"}],
            "1": [{"name": "黑"}, {"name": "白"}],
            "2": [{"name": "5G"}],
        },
        "skuSpecList": [
            {"skuId": "1", "sequences": {"0": "1", "1": "1", "2": "1"}},
            {"skuId": "2", "sequences": {"0": "1", "1": "2", "2": "1"}},
        ],
    }


def _api_setup(brand):
    if brand == "oppo":
        connector = OppoCatalogConnector()
        item = DiscoveredCatalogProduct(
            external_product_id="10", url=connector._detail_url("1"), category_code="PHONE"
        )
        responses = {
            connector._detail_url(sku): _result(
                connector._detail_url(sku),
                {"code": 200, "data": {"_$data": _oppo_current(sku)}},
                offset=int(sku),
            )
            for sku in ("1", "2")
        }
    else:
        connector = VivoCatalogConnector()
        item = DiscoveredCatalogProduct(
            external_product_id="10", url=connector._info_url("10"), category_code="PHONE"
        )
        responses = {
            item.url: _result(
                item.url,
                {
                    "code": 0,
                    "data": {
                        "commoditySpu": {"id": "10", "spuName": "vivo X Fixture"},
                        "specItem": _vivo_spec(),
                        "downSkuIds": [],
                        "zeroStoreSkuIds": [],
                    },
                },
            )
        }
        for sku in ("1", "2"):
            url = connector._detail_url("10", sku)
            responses[url] = _result(
                url,
                {
                    "code": 0,
                    "data": {
                        sku: {
                            "salePrice": 3999,
                            "marketPrice": 4299,
                            "marketable": 1,
                            "skuStatus": {"hasStore": 1},
                        }
                    },
                },
                offset=int(sku),
            )
    return connector, item, responses


class FixtureFetcher:
    def __init__(self, responses):
        self.responses = responses
        self.calls = []

    async def fetch(self, url, *, allowed_domains, tls_profile="DEFAULT"):
        self.calls.append(url)
        return self.responses[url]


def _context(connector, fetcher):
    return AdapterContext(
        http=fetcher, browser=fetcher, allowed_domains=list(connector.allowed_domains)
    )


@pytest.mark.parametrize("brand", ["oppo", "vivo"])
def test_multirequest_product_has_complete_native_rows_and_original_evidence(brand):
    connector, item, responses = _api_setup(brand)
    fetcher = FixtureFetcher(responses)
    evidence = asyncio.run(connector.fetch_product(_context(connector, fetcher), item))
    bundle = json.loads(evidence.body)
    assert fetcher.calls == list(responses)
    assert evidence.fetched_at == NOW + timedelta(seconds=2)
    assert evidence.duration_ms == 5 * len(responses)
    assert len(bundle["requests"]) == len(responses)
    for request in bundle["requests"]:
        original = responses[request["request_url"]]
        assert request["body"].encode() == original.body
        assert request["source_hash"] == sha256(original.body).hexdigest()
        assert request["fetched_at"] == original.fetched_at.isoformat()
    product = connector.parse_product(item, evidence)
    assert [row.item.external_sku_id for row in product.rows] == ["1", "2"]
    for row in product.rows:
        candidate = row.parsed.price_candidates[0]
        assert candidate.evidence_hash is None and candidate.source_observed_at is None
        spec = row.parsed.source_attributes["device_specification"]
        assert spec["memory"] == "12GB" and spec["capacity"] == "256GB"
        assert "manufacturer_part_number" not in spec
        if brand == "vivo":
            assert spec["connectivity"] == "5G"
    prepared = prepare_catalog_product(
        product,
        discovered=item,
        expected_brand_code=connector.brand_code,
        request=CatalogCollectionRequest(region_scope=RegionScope.NATIONAL, region_code="CN"),
        allowed_domains=connector.allowed_domains,
        rule_for_category=lambda _: DeviceCategoryRule(),
    )
    assert prepared.complete and prepared.accepted_count == 2


@pytest.mark.parametrize("brand", ["oppo", "vivo"])
@pytest.mark.parametrize("status", [404, 410])
def test_primary_not_found_preserves_response_without_followup_requests(brand, status):
    connector, item, responses = _api_setup(brand)
    missing = _result(item.url, {}, status=status)
    fetcher = FixtureFetcher({item.url: missing})
    assert asyncio.run(connector.fetch_product(_context(connector, fetcher), item)) is missing
    assert fetcher.calls == [item.url]


@pytest.mark.parametrize("brand", ["oppo", "vivo"])
def test_secondary_not_found_is_not_returned_as_product_not_found(brand):
    connector, item, responses = _api_setup(brand)
    secondary = list(responses)[-1]
    responses[secondary] = replace(responses[secondary], status_code=404)
    fetcher = FixtureFetcher(responses)
    with pytest.raises(ValueError, match="detail returned HTTP 404"):
        asyncio.run(connector.fetch_product(_context(connector, fetcher), item))


@pytest.mark.parametrize("brand", ["oppo", "vivo"])
@pytest.mark.parametrize("destination", ["https://attacker.invalid/", "other-product"])
def test_secondary_redirect_cannot_be_hidden_by_aggregate_response(brand, destination):
    connector, item, responses = _api_setup(brand)
    secondary = list(responses)[-1]
    if destination == "other-product":
        destination = secondary.replace("skuId=2", "skuId=3")
    responses[secondary] = replace(responses[secondary], final_url=destination)
    fetcher = FixtureFetcher(responses)
    with pytest.raises(ValueError, match="URL|origin"):
        asyncio.run(connector.fetch_product(_context(connector, fetcher), item))


@pytest.mark.parametrize("brand", ["oppo", "vivo"])
def test_missing_advertised_sku_cannot_silently_be_dropped(brand):
    connector, item, responses = _api_setup(brand)
    fetcher = FixtureFetcher(responses)
    evidence = asyncio.run(connector.fetch_product(_context(connector, fetcher), item))
    bundle = json.loads(evidence.body)
    if brand == "oppo":
        bundle["responses"].pop()
    else:
        bundle["details"].pop("2")
    with pytest.raises(ValueError, match="SKU"):
        connector.parse_product(item, replace(evidence, body=json.dumps(bundle).encode()))


@pytest.mark.parametrize("brand", ["oppo", "vivo"])
@pytest.mark.parametrize("known_unavailable", [False, True])
def test_priceless_sku_requires_explicit_unavailable_state(brand, known_unavailable):
    connector, item, responses = _api_setup(brand)
    fetcher = FixtureFetcher(responses)
    evidence = asyncio.run(connector.fetch_product(_context(connector, fetcher), item))
    bundle = json.loads(evidence.body)
    if brand == "oppo":
        bundle["responses"][0]["price"] = None
        bundle["responses"][0]["bty_type"] = "已售罄" if known_unavailable else ""
    else:
        bundle["details"]["1"].update(salePrice=None, marketable=None, skuStatus={})
        bundle["zeroStoreSkuIds"] = ["1"] if known_unavailable else []
    evidence = replace(evidence, body=json.dumps(bundle).encode())
    if not known_unavailable:
        with pytest.raises(ValueError, match="direct price or explicit unavailable"):
            connector.parse_product(item, evidence)
        return
    candidate = connector.parse_product(item, evidence).rows[0].parsed.price_candidates[0]
    assert candidate.price_type is PriceType.AVAILABILITY_ONLY
    assert candidate.availability is Availability.OUT_OF_STOCK
    assert candidate.current_price is None and candidate.original_price is None


@pytest.mark.parametrize("brand", ["oppo", "vivo"])
@pytest.mark.parametrize("mutation", ["duplicate", "missing-price", "foreign-product"])
def test_browser_snapshot_rejects_incomplete_or_conflicting_product(brand, mutation):
    connector = OppoCatalogConnector() if brand == "oppo" else VivoCatalogConnector()
    payload = json.loads((FIXTURES / brand / "product_snapshots.json").read_bytes())
    url = payload["source_url"]
    product_id = "43001" if brand == "oppo" else "44001"
    item = DiscoveredCatalogProduct(external_product_id=product_id, url=url, category_code="PHONE")
    if mutation == "duplicate":
        payload["snapshots"].append(payload["snapshots"][0])
    elif mutation == "foreign-product":
        payload["source_url"] += "-other"
    else:
        payload["snapshots"][0]["html"] = payload["snapshots"][0]["html"].replace(
            "current-price", "not-a-price"
        )
    with pytest.raises(ValueError):
        connector.parse_product(item, _result(url, payload))


@pytest.mark.parametrize("mutation", ["duplicate", "missing-dimension", "invalid-option"])
def test_vivo_rejects_ambiguous_or_incomplete_specification_map(mutation):
    spec = _vivo_spec()
    if mutation == "duplicate":
        spec["skuSpecList"].append(spec["skuSpecList"][0])
    elif mutation == "missing-dimension":
        spec["skuSpecList"][0]["sequences"].pop("2")
    else:
        spec["skuSpecList"][0]["sequences"]["0"] = "0"
    with pytest.raises(VivoParseError):
        VivoCatalogConnector._sku_specs(spec)


def test_vivo_missing_marketable_does_not_mean_off_shelf():
    assert VivoCatalogConnector._api_availability("1", {}, set(), set()) is Availability.UNKNOWN
    assert (
        VivoCatalogConnector._api_availability("1", {"skuStatus": {"hasStore": 1}}, set(), set())
        is Availability.UNKNOWN
    )


@pytest.mark.parametrize("connector", [OppoCatalogConnector(), VivoCatalogConnector()])
def test_discovery_rejects_other_region_before_fetching(connector):
    fetcher = FixtureFetcher({})
    with pytest.raises(ValueError, match="NATIONAL/CN"):
        asyncio.run(
            connector.discover_products(
                _context(connector, fetcher),
                CatalogCollectionRequest(region_scope=RegionScope.CITY, region_code="310100"),
            )
        )
    assert fetcher.calls == []


def test_oppo_fetch_rejects_response_for_wrong_sku():
    connector, item, responses = _api_setup("oppo")
    payload = json.loads(responses[item.url].body)
    payload["data"]["_$data"]["skuId"] = "999"
    responses[item.url] = _result(item.url, payload)
    fetcher = FixtureFetcher(responses)
    with pytest.raises(OppoParseError, match="requested SKU"):
        asyncio.run(connector.fetch_product(_context(connector, fetcher), item))


def test_vivo_fetch_rejects_missing_requested_sku_detail():
    connector, item, responses = _api_setup("vivo")
    url = connector._detail_url("10", "2")
    responses[url] = _result(url, {"code": 0, "data": {}})
    fetcher = FixtureFetcher(responses)
    with pytest.raises(VivoParseError, match="omits requested SKU"):
        asyncio.run(connector.fetch_product(_context(connector, fetcher), item))


def test_oppo_keeps_labelled_style_and_fixed_configuration_dimensions():
    raw = _oppo_current()
    raw["skuAttributes"]["key3"] = "柔光版"
    raw["attributeList"][0]["value"].append({"key": "key3", "_$text1": "款式"})
    assert OppoCatalogConnector._selected_dimensions(raw)["款式"] == "柔光版"


@pytest.mark.parametrize("mutation", ["bad-id", "missing-dimension", "ambiguous-label"])
def test_oppo_rejects_incomplete_advertised_specifications(mutation):
    raw = _oppo_current()
    dimension = raw["attributeList"][0]["value"][0]
    if mutation == "bad-id":
        dimension["items"][0]["skuId"] = ""
    elif mutation == "missing-dimension":
        dimension["items"][0]["attributes"].pop("key2")
    else:
        raw["attributeList"][0]["value"].append({"key": "key1", "_$text1": "款式"})
    with pytest.raises(OppoParseError):
        OppoCatalogConnector._variant_sku_ids(raw)


def test_vivo_separate_version_capacity_and_style_are_all_preserved():
    connector, item, _ = _api_setup("vivo")
    row = connector._catalog_row(
        item,
        "vivo X Fixture",
        {
            "sku_id": "1",
            "version": "标准版",
            "color": "黑",
            "dimensions": {"版本": "标准版", "容量": "512GB", "内存": "12GB", "规格": "柔光版"},
            "current_text": "3999",
            "availability": Availability.ON_SALE.value,
        },
    )
    spec = row.parsed.source_attributes["device_specification"]
    assert spec["capacity"] == "512GB" and spec["memory"] == "12GB"
    assert spec["edition"] == "标准版" and spec["attributes"]["规格"] == "柔光版"


@pytest.mark.parametrize("brand", ["oppo", "vivo"])
def test_discovery_category_filter_is_applied_to_native_products(brand):
    connector = OppoCatalogConnector() if brand == "oppo" else VivoCatalogConnector()
    url = connector.discovery_url
    response = replace(_result(url, {}), body=(FIXTURES / brand / "discovery.html").read_bytes())
    fetcher = FixtureFetcher({url: response})
    items = asyncio.run(
        connector.discover_products(
            _context(connector, fetcher),
            CatalogCollectionRequest(
                region_scope=RegionScope.NATIONAL, region_code="CN", category_codes=("WATCH",)
            ),
        )
    )
    assert len(items) == 1 and items[0].category_code == "WATCH"
    assert not parse_qs(urlsplit(items[0].url).query)
