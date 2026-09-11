from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, Protocol

from device_price_service.domain.crawl import (
    BrowserSnapshotPlan,
    FetchResult,
)


class Fetcher(Protocol):
    async def fetch(
        self,
        url: str,
        *,
        allowed_domains: list[str],
        tls_profile: Literal["DEFAULT", "TLS12_COMPAT"] = "DEFAULT",
    ) -> FetchResult: ...


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
