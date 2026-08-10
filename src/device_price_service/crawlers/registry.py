from __future__ import annotations

from collections.abc import Iterator

from device_price_service.crawlers.base import BrandAdapter


class AdapterRegistryError(ValueError):
    """Raised for duplicate or missing adapter registrations."""


class AdapterRegistry:
    def __init__(self) -> None:
        self._by_channel: dict[str, BrandAdapter] = {}

    def register(self, adapter: BrandAdapter) -> None:
        if adapter.channel_code in self._by_channel:
            raise AdapterRegistryError(f"adapter already registered: {adapter.channel_code}")
        self._by_channel[adapter.channel_code] = adapter

    def get(self, channel_code: str) -> BrandAdapter:
        try:
            return self._by_channel[channel_code]
        except KeyError as error:
            raise AdapterRegistryError(f"adapter is not registered: {channel_code}") from error

    def get_by_brand(self, brand_code: str) -> BrandAdapter:
        normalized = brand_code.upper()
        matches = [
            adapter for adapter in self._by_channel.values() if adapter.brand_code == normalized
        ]
        if len(matches) != 1:
            raise AdapterRegistryError(
                f"expected exactly one adapter for brand {normalized}, found {len(matches)}"
            )
        return matches[0]

    def __iter__(self) -> Iterator[BrandAdapter]:
        return iter(self._by_channel.values())

    def __len__(self) -> int:
        return len(self._by_channel)
