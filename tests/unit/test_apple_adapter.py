import asyncio
import json
from dataclasses import replace
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from device_price_service.crawlers.apple import AppleCatalogConnector, AppleParseError
from device_price_service.crawlers.base import AdapterContext
from device_price_service.domain.catalog_crawl import (
    CatalogCollectionRequest,
    DiscoveredCatalogProduct,
    ParsedCatalogProduct,
)
from device_price_service.domain.catalog_enums import (
    Availability,
    FeeStatus,
    OriginalPriceType,
    PriceNature,
    PriceType,
    PricingBasis,
    QualityStatus,
    RegionScope,
    SellerType,
    VerificationStatus,
)
from device_price_service.domain.crawl import FetchResult
from device_price_service.domain.enums import FetchMethod
from device_price_service.domain.price_policy import ConditionalPriceError, MoneyParseError
from device_price_service.normalization.devices import DeviceCategoryRule, DeviceSpecification
from device_price_service.services.catalog_preparation import prepare_catalog_product

FIXTURES = Path(__file__).parents[1] / "fixtures" / "apple"
PRODUCT_URL = "https://www.apple.com.cn/shop/buy-iphone/iphone-fixture-pro"
DISCOVERY_URL = "https://www.apple.com.cn/shop/buy-iphone"


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


def _item(url: str = PRODUCT_URL, category: str = "PHONE") -> DiscoveredCatalogProduct:
    return DiscoveredCatalogProduct(
        external_product_id=url.rsplit("/", 1)[-1], url=url, category_code=category
    )


def _bootstrap() -> dict:
    return {
        "products": [
            {
                "aosContainerPartNumber": "RO_MBA_FIXTURE",
                "priceKey": "13inch-midnight",
                "isComingSoon": False,
                "dimensions": {
                    "chassis-dimensionScreensize": "13inch",
                    "chassis-dimensionColor": "midnight",
                },
                "productConfiguration": {"memory": "MEM16", "storage": "SSD512"},
            }
        ],
        "mainDisplayValues": {
            "chassis-dimensionScreensize": {"13inch": {"header": "13 英寸"}},
            "chassis-dimensionColor": {"midnight": {"header": "午夜色"}},
            "prices": {"13inch-midnight": {"currentPrice": {"raw_amount": "9999.00"}}},
        },
    }


def _parse_bootstrap(selection: dict) -> ParsedCatalogProduct:
    url = "https://www.apple.com.cn/shop/buy-mac/macbook-air"
    html = (
        "<html><h1>购买 MacBook Air</h1><script>"
        "window.PRODUCT_SELECTION_BOOTSTRAP = {productSelectionData: "
        f"{json.dumps(selection, ensure_ascii=False)}}};</script></html>"
    )
    result = replace(_result("product_macbook.html", url), body=html.encode())
    return AppleCatalogConnector().parse_product(_item(url, "LAPTOP"), result)


def test_apple_discovers_only_family_pages() -> None:
    items = AppleCatalogConnector.parse_discovery(
        (FIXTURES / "discovery_iphone.html").read_bytes(), DISCOVERY_URL, "PHONE"
    )
    assert [(item.external_product_id, item.category_code) for item in items] == [
        ("iphone-fixture-pro", "PHONE"),
        ("iphone-fixture-air", "PHONE"),
    ]


def test_apple_classifies_computers_and_excludes_displays() -> None:
    items = AppleCatalogConnector.parse_discovery(
        (FIXTURES / "discovery_mac.html").read_bytes(),
        "https://www.apple.com.cn/shop/buy-mac",
        "COMPUTER",
    )
    assert [(item.external_product_id, item.category_code) for item in items] == [
        ("macbook-air", "LAPTOP"),
        ("imac", "DESKTOP"),
        ("mac-mini", "DESKTOP"),
    ]


def test_apple_parses_direct_sku_prices_into_catalog_rows() -> None:
    product = AppleCatalogConnector().parse_product(_item(), _result("product_iphone.html"))
    assert product.name == "iPhone Fixture Pro"
    assert product.brand_code == "APPLE"
    assert len(product.rows) == 3
    first, second, third = product.rows
    assert first.item.external_sku_id == "FX256BLUECH/A"
    specification = DeviceSpecification.model_validate(
        first.parsed.source_attributes["device_specification"]
    )
    assert specification.capacity == "256GB"
    assert specification.manufacturer_part_number == "FX256BLUECH/A"
    assert specification.memory is None
    assert first.parsed.price_candidates[0].current_price == Decimal("8999.00")
    assert first.parsed.price_candidates[0].original_price is None
    assert second.parsed.price_candidates[0].original_price == Decimal("10999.00")
    assert (
        second.parsed.price_candidates[0].original_price_type is OriginalPriceType.EXPLICIT_ORIGINAL
    )
    assert third.parsed.price_candidates[0].availability is Availability.OUT_OF_STOCK
    fingerprints = set()
    for row in product.rows:
        identity = DeviceCategoryRule().normalize(row.item, row.parsed)
        assert identity.quality_status is QualityStatus.ACCEPTED
        fingerprints.add(identity.build_fingerprint())
        assert row.item.external_product_id == product.external_product_id
        assert row.item.price_nature is PriceNature.RETAIL_OFFER
        assert row.item.merchant.seller_type is SellerType.BRAND_OFFICIAL
        assert row.item.merchant.verification_status is VerificationStatus.VERIFIED
        candidate = row.parsed.price_candidates[0]
        assert candidate.price_type is PriceType.DIRECT_UNCONDITIONAL
        assert candidate.pricing_basis is PricingBasis.PACKAGE_TOTAL
        assert candidate.fee_status is FeeStatus.ITEM_ONLY
        assert candidate.source_observed_at is None
        assert candidate.region.scope is RegionScope.NATIONAL
        assert candidate.region.code == "CN"
    assert len(fingerprints) == 3
    assert ParsedCatalogProduct.model_validate_json(product.model_dump_json()) == product


def test_apple_rejects_conditional_selected_price() -> None:
    url = "https://www.apple.com.cn/shop/buy-iphone/iphone-fixture-conditional"
    with pytest.raises(ConditionalPriceError, match="conditional price"):
        AppleCatalogConnector().parse_product(
            _item(url), _result("product_conditional_price.html", url)
        )


def test_apple_rows_pass_the_shared_smoke_and_persistence_preparation() -> None:
    connector = AppleCatalogConnector()
    item = _item()
    product = connector.parse_product(item, _result("product_iphone.html"))
    prepared = prepare_catalog_product(
        product,
        discovered=item,
        expected_brand_code=connector.brand_code,
        request=CatalogCollectionRequest(
            region_scope=RegionScope.NATIONAL, region_code="CN", category_codes=("PHONE",)
        ),
        allowed_domains=connector.allowed_domains,
        rule_for_category=lambda _: DeviceCategoryRule(),
    )
    assert prepared.complete
    assert prepared.accepted_count == 3
    assert prepared.rejected_count == prepared.review_count == 0


def test_apple_preserves_cross_category_configuration_dimensions() -> None:
    url = "https://www.apple.com.cn/shop/buy-mac/macbook-fixture"
    product = AppleCatalogConnector().parse_product(
        _item(url, "LAPTOP"), _result("product_macbook.html", url)
    )
    specification = DeviceSpecification.model_validate(
        product.rows[0].parsed.source_attributes["device_specification"]
    )
    assert product.category_code == "LAPTOP"
    assert specification.memory == "16GB"
    assert specification.capacity == "512GB"
    assert specification.size == "14 英寸"
    assert specification.attributes["processor"] == "Fixture Pro 芯片"
    assert specification.attributes["model"] == "MacBook Fixture"


def test_apple_parses_exact_preselected_configuration_from_bootstrap() -> None:
    row = _parse_bootstrap(_bootstrap()).rows[0]
    specification = DeviceSpecification.model_validate(
        row.parsed.source_attributes["device_specification"]
    )
    assert row.item.external_sku_id == "RO_MBA_FIXTURE"
    assert specification.size == "13 英寸"
    assert specification.color == "午夜色"
    assert specification.attributes["configuration_memory"] == "MEM16"
    assert specification.attributes["configuration_storage"] == "SSD512"
    assert specification.attributes["aos_container_part_number"] == "RO_MBA_FIXTURE"
    # A configuration-container ID is not a proven manufacturer part number.
    assert specification.manufacturer_part_number is None
    assert row.parsed.price_candidates[0].current_price == Decimal("9999.00")


def test_apple_bootstrap_preserves_original_price_and_manufacturer_part_number() -> None:
    selection = _bootstrap()
    selection["products"][0]["btrOrFdPartNumber"] = "MBFIXTURECH/A"
    selection["products"][0]["isComingSoon"] = True
    selection["mainDisplayValues"]["prices"]["13inch-midnight"]["previousPrice"] = {
        "raw_amount": "10999"
    }
    row = _parse_bootstrap(selection).rows[0]
    assert row.item.external_sku_id == "MBFIXTURECH/A"
    assert (
        row.parsed.source_attributes["device_specification"]["manufacturer_part_number"]
        == "MBFIXTURECH/A"
    )
    candidate = row.parsed.price_candidates[0]
    assert candidate.availability is Availability.COMING_SOON
    assert candidate.original_price == Decimal("10999.00")
    assert candidate.original_price_type is OriginalPriceType.CROSSED_OUT


@pytest.mark.parametrize("field", ["request_url", "final_url"])
@pytest.mark.parametrize(
    "url",
    [
        "https://www.apple.com.cn/shop/buy-iphone/iphone-other",
        "https://example.com/shop/buy-iphone/iphone-fixture-pro",
        "https://www.apple.com.cn:8443/shop/buy-iphone/iphone-fixture-pro",
        "https://user@www.apple.com.cn/shop/buy-iphone/iphone-fixture-pro",
    ],
)
def test_apple_rejects_foreign_or_wrong_product_response(field: str, url: str) -> None:
    result = replace(_result("product_iphone.html"), **{field: url})
    with pytest.raises(AppleParseError, match="Apple"):
        AppleCatalogConnector().parse_product(_item(), result)


@pytest.mark.parametrize(
    "target",
    [
        "/shop/buy-iphone/iphone-other/FX256BLUECH/A",
        "https://example.com/shop/buy-iphone/iphone-fixture-pro/FX256BLUECH/A",
        "https://www.apple.com.cn:8443/shop/buy-iphone/iphone-fixture-pro/FX256BLUECH/A",
    ],
)
def test_apple_rejects_priced_sku_outside_product(target: str) -> None:
    result = _result("product_iphone.html")
    body = result.body.replace(
        b"/shop/buy-iphone/iphone-fixture-pro/FX256BLUECH/A", target.encode()
    )
    with pytest.raises(AppleParseError, match="priced SKU URL"):
        AppleCatalogConnector().parse_product(_item(), replace(result, body=body))


def test_apple_rejects_duplicate_html_sku_instead_of_silently_dropping_it() -> None:
    result = _result("product_iphone.html")
    body = result.body.replace(b"FX512SILVERCH/A", b"FX256BLUECH/A")
    with pytest.raises(AppleParseError, match="repeats SKU"):
        AppleCatalogConnector().parse_product(_item(), replace(result, body=body))


def test_apple_rejects_duplicate_bootstrap_sku() -> None:
    selection = _bootstrap()
    selection["products"].append(dict(selection["products"][0]))
    with pytest.raises(AppleParseError, match="repeats SKU"):
        _parse_bootstrap(selection)


def test_apple_rejects_incomplete_bootstrap_sku_prices() -> None:
    selection = _bootstrap()
    selection["products"].append({"btrOrFdPartNumber": "MISSING/A", "priceKey": "missing"})
    with pytest.raises(AppleParseError, match="missing a SKU identity or price"):
        _parse_bootstrap(selection)


def test_apple_rejects_missing_html_sku_price_instead_of_partial_product() -> None:
    result = _result("product_iphone.html")
    body = result.body.replace(b'<span class="current_price">RMB 8,999</span>', b"")
    with pytest.raises(AppleParseError, match="missing explicit dimensions or current price"):
        AppleCatalogConnector().parse_product(_item(), replace(result, body=body))


def test_apple_zero_raw_price_cannot_fall_back_to_another_amount() -> None:
    selection = _bootstrap()
    selection["mainDisplayValues"]["prices"]["13inch-midnight"]["currentPrice"] = {
        "raw_amount": 0,
        "amount": "9999",
    }
    with pytest.raises(MoneyParseError, match="greater than zero"):
        _parse_bootstrap(selection)


def test_apple_equal_original_price_remains_null() -> None:
    result = _result("product_iphone.html")
    body = result.body.replace(b"RMB 10,999", b"RMB 9,999")
    candidate = (
        AppleCatalogConnector()
        .parse_product(_item(), replace(result, body=body))
        .rows[1]
        .parsed.price_candidates[0]
    )
    assert candidate.original_price is None
    assert candidate.original_price_type is OriginalPriceType.NONE


def test_apple_title_change_does_not_change_specification_identity() -> None:
    result = _result("product_iphone.html")
    original = AppleCatalogConnector().parse_product(_item(), result)
    changed = AppleCatalogConnector().parse_product(
        _item(), replace(result, body=result.body.replace(b"iPhone Fixture Pro", b"New Title"))
    )
    assert original.name != changed.name
    for before, after in zip(original.rows, changed.rows, strict=True):
        assert before.item.listing_key == after.item.listing_key
        assert before.parsed.source_attributes == after.parsed.source_attributes


@pytest.mark.parametrize("update", [{"external_product_id": "other"}, {"category_code": "TABLET"}])
def test_apple_rejects_discovered_identity_mismatch(update: dict[str, str]) -> None:
    with pytest.raises(AppleParseError, match="does not match"):
        AppleCatalogConnector().parse_product(
            _item().model_copy(update=update), _result("product_iphone.html")
        )


def test_apple_discovery_respects_category_scope_and_uses_one_product_fetch() -> None:
    fetcher = AsyncMock()
    fetcher.fetch.return_value = _result("discovery_iphone.html", DISCOVERY_URL)
    context = AdapterContext(http=fetcher, browser=fetcher, allowed_domains=["www.apple.com.cn"])
    connector = AppleCatalogConnector()
    request = CatalogCollectionRequest(
        region_scope=RegionScope.NATIONAL, region_code="CN", category_codes=("PHONE",)
    )
    items = asyncio.run(connector.discover_products(context, request))
    assert len(items) == 2
    assert fetcher.fetch.await_count == 1
    fetcher.fetch.return_value = _result("product_iphone.html")
    result = asyncio.run(connector.fetch_product(context, items[0]))
    assert len(connector.parse_product(items[0], result).rows) == 3
    assert fetcher.fetch.await_count == 2


def test_apple_discovery_rejects_redirect_to_another_shop_page() -> None:
    fetcher = AsyncMock()
    fetcher.fetch.return_value = replace(
        _result("discovery_iphone.html", DISCOVERY_URL),
        final_url="https://www.apple.com.cn/shop/buy-ipad",
    )
    context = AdapterContext(http=fetcher, browser=fetcher, allowed_domains=["www.apple.com.cn"])
    with pytest.raises(AppleParseError, match="does not match"):
        asyncio.run(
            AppleCatalogConnector().discover_products(
                context,
                CatalogCollectionRequest(
                    region_scope=RegionScope.NATIONAL, region_code="CN", category_codes=("PHONE",)
                ),
            )
        )


@pytest.mark.parametrize(
    "scope",
    [
        CatalogCollectionRequest(region_scope=RegionScope.CITY, region_code="310100"),
        CatalogCollectionRequest(
            region_scope=RegionScope.NATIONAL, region_code="CN", category_codes=("FRESH",)
        ),
        CatalogCollectionRequest(
            region_scope=RegionScope.NATIONAL, region_code="CN", source_item_codes=("PORK",)
        ),
    ],
)
def test_apple_rejects_unsupported_scope_before_network(scope: CatalogCollectionRequest) -> None:
    fetcher = AsyncMock()
    context = AdapterContext(http=fetcher, browser=fetcher, allowed_domains=["www.apple.com.cn"])
    with pytest.raises(AppleParseError):
        asyncio.run(AppleCatalogConnector().discover_products(context, scope))
    fetcher.fetch.assert_not_awaited()
