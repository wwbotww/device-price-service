from __future__ import annotations

from device_price_service.crawlers.apple import AppleAdapter
from device_price_service.crawlers.huawei import HuaweiAdapter
from device_price_service.crawlers.oppo import OppoAdapter
from device_price_service.crawlers.registry import AdapterRegistry
from device_price_service.crawlers.vivo import VivoAdapter
from device_price_service.crawlers.xiaomi import XiaomiAdapter


def build_builtin_registry() -> AdapterRegistry:
    registry = AdapterRegistry()
    registry.register(AppleAdapter())
    registry.register(HuaweiAdapter())
    registry.register(XiaomiAdapter())
    registry.register(OppoAdapter())
    registry.register(VivoAdapter())
    return registry
