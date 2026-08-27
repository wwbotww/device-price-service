from device_price_service.crawlers.catalog import CatalogDatasetConnectorRegistry
from device_price_service.crawlers.mofcom_fresh import MofcomFreshWholesaleConnector
from device_price_service.crawlers.shanghai_fresh import ShanghaiFreshRetailConnector


def build_catalog_dataset_registry() -> CatalogDatasetConnectorRegistry:
    registry = CatalogDatasetConnectorRegistry()
    registry.register(ShanghaiFreshRetailConnector())
    registry.register(MofcomFreshWholesaleConnector())
    return registry
