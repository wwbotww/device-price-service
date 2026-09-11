import pytest

from device_price_service.crawlers.base import AdapterContext, BrandAdapter
from device_price_service.crawlers.builtin import build_builtin_registry
from device_price_service.crawlers.catalog import CatalogConnector
from device_price_service.crawlers.catalog_builtin import build_catalog_registry
from device_price_service.crawlers.registry import AdapterRegistry, AdapterRegistryError
from device_price_service.domain.crawl import (
    DiscoveredProduct,
    FetchResult,
    NormalizedProduct,
    ParsedProduct,
)


class MinimalAdapter(BrandAdapter):
    brand_code = "APPLE"
    channel_code = "APPLE_CN_WEB"
    version = "test"

    async def discover(self, context: AdapterContext) -> list[DiscoveredProduct]:
        return []

    async def fetch_product(self, context: AdapterContext, item: DiscoveredProduct) -> FetchResult:
        raise NotImplementedError

    def parse_product(self, item: DiscoveredProduct, result: FetchResult) -> ParsedProduct:
        raise NotImplementedError

    def normalize(self, item: DiscoveredProduct, parsed: ParsedProduct) -> NormalizedProduct:
        raise NotImplementedError


def test_registry_registers_and_rejects_duplicate_channel() -> None:
    registry = AdapterRegistry()
    adapter = MinimalAdapter()
    registry.register(adapter)

    assert registry.get("APPLE_CN_WEB") is adapter
    assert len(registry) == 1
    with pytest.raises(AdapterRegistryError, match="already registered"):
        registry.register(MinimalAdapter())


def test_registry_reports_missing_adapter() -> None:
    with pytest.raises(AdapterRegistryError, match="not registered"):
        AdapterRegistry().get("MISSING")


def test_legacy_builtin_registry_no_longer_contains_production_brands() -> None:
    registry = build_builtin_registry()

    assert len(registry) == 0
    for brand in ("apple", "huawei", "xiaomi", "oppo", "vivo"):
        with pytest.raises(AdapterRegistryError, match="found 0"):
            registry.get_by_brand(brand)


def test_unified_registry_contains_five_native_device_sources_and_two_government_sources() -> None:
    registry = build_catalog_registry()

    assert len(list(registry)) == 7
    brands = {
        connector.brand_code: connector.channel_code
        for connector in registry
        if isinstance(connector, CatalogConnector)
    }
    assert brands == {
        brand: f"{brand}_CN_WEB" for brand in ("APPLE", "HUAWEI", "XIAOMI", "OPPO", "VIVO")
    }
