from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Iterator

from device_price_service.crawlers.base import AdapterContext
from device_price_service.domain.catalog_crawl import (
    CatalogCollectionRequest,
    DiscoveredCatalogDataset,
    DiscoveredCatalogProduct,
    ParsedCatalogDataset,
    ParsedCatalogProduct,
)
from device_price_service.domain.catalog_enums import CollectionFetchMethod
from device_price_service.domain.crawl import FetchResult


class CatalogSourceConnector(ABC):
    """Fields shared by product and public-dataset connectors."""

    channel_code: str
    connector_code: str
    version: str
    fetch_method: CollectionFetchMethod
    allowed_domains: tuple[str, ...]


class CatalogConnector(CatalogSourceConnector):
    """Discover products, then parse their complete set of source SKU rows."""

    brand_code: str
    # A SKU endpoint disappearing must never retire all SKUs of its product.
    product_not_found_is_definitive: bool = False
    default_category_codes: tuple[str, ...]

    @abstractmethod
    async def discover_products(
        self,
        context: AdapterContext,
        request: CatalogCollectionRequest,
    ) -> list[DiscoveredCatalogProduct]:
        """Discover logical product fetch units inside the requested scope."""

    @abstractmethod
    async def fetch_product(
        self,
        context: AdapterContext,
        item: DiscoveredCatalogProduct,
    ) -> FetchResult:
        """Fetch one product's evidence, shared by all of its SKU rows."""

    @abstractmethod
    def parse_product(
        self,
        item: DiscoveredCatalogProduct,
        result: FetchResult,
    ) -> ParsedCatalogProduct:
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
        self._by_channel: dict[str, CatalogConnector | CatalogDatasetConnector] = {}

    def register(self, connector: CatalogConnector | CatalogDatasetConnector) -> None:
        channel_code = connector.channel_code.strip().upper()
        if channel_code in self._by_channel:
            raise ValueError(f"catalog connector already registered: {channel_code}")
        if not connector.connector_code.strip() or not connector.version.strip():
            raise ValueError("catalog connector code and version cannot be blank")
        if not connector.allowed_domains:
            raise ValueError("catalog connector allowed domains cannot be empty")
        self._by_channel[channel_code] = connector

    def get(self, channel_code: str) -> CatalogConnector | CatalogDatasetConnector:
        normalized = channel_code.strip().upper()
        try:
            return self._by_channel[normalized]
        except KeyError as error:
            raise ValueError(f"catalog connector is not registered: {normalized}") from error

    def __iter__(self) -> Iterator[CatalogConnector | CatalogDatasetConnector]:
        return iter(self._by_channel.values())
