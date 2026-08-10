from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Protocol

from device_price_service.domain.crawl import (
    BrowserSnapshotPlan,
    DiscoveredProduct,
    FetchResult,
    NormalizedProduct,
    ParsedProduct,
)


class Fetcher(Protocol):
    async def fetch(self, url: str, *, allowed_domains: list[str]) -> FetchResult: ...


class BrowserSnapshotFetcher(Fetcher, Protocol):
    async def fetch_snapshots(
        self,
        url: str,
        *,
        allowed_domains: list[str],
        plan: BrowserSnapshotPlan,
    ) -> FetchResult: ...


@dataclass(frozen=True, slots=True)
class AdapterContext:
    http: Fetcher
    browser: BrowserSnapshotFetcher
    allowed_domains: list[str]


class BrandAdapter(ABC):
    brand_code: str
    channel_code: str
    version: str

    @abstractmethod
    async def discover(self, context: AdapterContext) -> list[DiscoveredProduct]:
        """Return all in-scope product detail pages discovered for this run."""

    @abstractmethod
    async def fetch_product(
        self,
        context: AdapterContext,
        item: DiscoveredProduct,
    ) -> FetchResult:
        """Fetch a product page through the approved context fetchers."""

    @abstractmethod
    def parse_product(self, item: DiscoveredProduct, result: FetchResult) -> ParsedProduct:
        """Extract site-specific fields without writing to the database."""

    @abstractmethod
    def normalize(self, item: DiscoveredProduct, parsed: ParsedProduct) -> NormalizedProduct:
        """Convert site-specific fields into the cross-brand domain model."""
