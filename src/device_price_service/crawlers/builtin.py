from __future__ import annotations

from device_price_service.crawlers.registry import AdapterRegistry


def build_builtin_registry() -> AdapterRegistry:
    """Keep the historical runtime inert until its removal with the V1 tools."""
    return AdapterRegistry()
