from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Iterator

from device_price_service.crawlers.base import AdapterContext
from device_price_service.domain.catalog_crawl import (
    CatalogCollectionRequest,
    DiscoveredCatalogDataset,
    DiscoveredCatalogListing,
    ParsedCatalogDataset,
    ParsedCatalogListing,
)
from device_price_service.domain.catalog_enums import CollectionFetchMethod
from device_price_service.domain.crawl import FetchResult


class CatalogSourceConnector(ABC):
    """Fields shared by listing and public-dataset connectors."""

    channel_code: str
    connector_code: str
    version: str
    fetch_method: CollectionFetchMethod


class CatalogConnector(CatalogSourceConnector):
    """Source-specific discovery, fetching and parsing for individual listings."""

    @abstractmethod
    async def discover(
        self,
        context: AdapterContext,
        request: CatalogCollectionRequest,
    ) -> list[DiscoveredCatalogListing]:
        """Discover stable source listing identities inside the requested scope."""

    @abstractmethod
    async def fetch_listing(
        self,
        context: AdapterContext,
        item: DiscoveredCatalogListing,
    ) -> FetchResult:
        """Fetch one discovered listing through an approved shared fetcher."""

    @abstractmethod
    def parse_listing(
        self,
        item: DiscoveredCatalogListing,
        result: FetchResult,
    ) -> ParsedCatalogListing:
        """Map source-specific response fields into the connector contract."""


class CatalogDatasetConnector(CatalogSourceConnector):
    """Narrow connector contract for one document containing many price rows."""

    allowed_domains: tuple[str, ...]

    @abstractmethod
    async def discover_dataset(
        self,
        context: AdapterContext,
        request: CatalogCollectionRequest,
    ) -> DiscoveredCatalogDataset:
        """Find the latest in-scope public data document."""

    @abstractmethod
    async def fetch_dataset(
        self,
        context: AdapterContext,
        dataset: DiscoveredCatalogDataset,
    ) -> FetchResult:
        """Fetch the shared public data document once."""

    @abstractmethod
    def parse_dataset(
        self,
        dataset: DiscoveredCatalogDataset,
        result: FetchResult,
    ) -> ParsedCatalogDataset:
        """Map the shared document into listing-shaped rows."""


class CatalogConnectorRegistry:
    def __init__(self) -> None:
        self._by_channel: dict[str, CatalogConnector] = {}

    def register(self, connector: CatalogConnector) -> None:
        channel_code = connector.channel_code.strip().upper()
        if channel_code in self._by_channel:
            raise ValueError(f"catalog connector already registered: {channel_code}")
        if not connector.connector_code.strip() or not connector.version.strip():
            raise ValueError("catalog connector code and version cannot be blank")
        self._by_channel[channel_code] = connector

    def get(self, channel_code: str) -> CatalogConnector:
        normalized = channel_code.strip().upper()
        try:
            return self._by_channel[normalized]
        except KeyError as error:
            raise ValueError(f"catalog connector is not registered: {normalized}") from error

    def __iter__(self) -> Iterator[CatalogConnector]:
        return iter(self._by_channel.values())


class CatalogDatasetConnectorRegistry:
    def __init__(self) -> None:
        self._by_channel: dict[str, CatalogDatasetConnector] = {}

    def register(self, connector: CatalogDatasetConnector) -> None:
        channel_code = connector.channel_code.strip().upper()
        if channel_code in self._by_channel:
            raise ValueError(f"catalog dataset connector already registered: {channel_code}")
        if not connector.connector_code.strip() or not connector.version.strip():
            raise ValueError("catalog dataset connector code and version cannot be blank")
        if not connector.allowed_domains:
            raise ValueError("catalog dataset connector allowed domains cannot be empty")
        self._by_channel[channel_code] = connector

    def get(self, channel_code: str) -> CatalogDatasetConnector:
        normalized = channel_code.strip().upper()
        try:
            return self._by_channel[normalized]
        except KeyError as error:
            raise ValueError(
                f"catalog dataset connector is not registered: {normalized}"
            ) from error

    def __iter__(self) -> Iterator[CatalogDatasetConnector]:
        return iter(self._by_channel.values())
