from device_price_service.crawlers.catalog import CatalogConnector
from device_price_service.crawlers.catalog_builtin import build_catalog_registry


def test_registry_contains_five_native_device_sources_and_two_government_sources() -> None:
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
