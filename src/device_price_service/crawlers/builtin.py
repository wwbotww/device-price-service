from __future__ import annotations

from device_price_service.crawlers.apple import AppleAdapter
from device_price_service.crawlers.registry import AdapterRegistry
from device_price_service.crawlers.xiaomi import XiaomiAdapter


def build_builtin_registry() -> AdapterRegistry:
    registry = AdapterRegistry()
    registry.register(AppleAdapter())
    registry.register(XiaomiAdapter())
    return registry
