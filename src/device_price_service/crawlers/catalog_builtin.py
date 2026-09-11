from device_price_service.crawlers.apple import AppleCatalogConnector
from device_price_service.crawlers.catalog import CatalogConnectorRegistry
from device_price_service.crawlers.huawei import HuaweiCatalogConnector
from device_price_service.crawlers.mofcom_fresh import MofcomFreshWholesaleConnector
from device_price_service.crawlers.oppo import OppoCatalogConnector
from device_price_service.crawlers.shanghai_fresh import ShanghaiFreshRetailConnector
from device_price_service.crawlers.vivo import VivoCatalogConnector
from device_price_service.crawlers.xiaomi import XiaomiCatalogConnector


def build_catalog_registry() -> CatalogConnectorRegistry:
    registry = CatalogConnectorRegistry()
    registry.register(ShanghaiFreshRetailConnector())
    registry.register(MofcomFreshWholesaleConnector())
    registry.register(AppleCatalogConnector())
    registry.register(HuaweiCatalogConnector())
    registry.register(XiaomiCatalogConnector())
    registry.register(OppoCatalogConnector())
    registry.register(VivoCatalogConnector())
    return registry
