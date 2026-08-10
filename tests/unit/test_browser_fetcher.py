import asyncio

import pytest

from device_price_service.config import Settings
from device_price_service.domain.crawl import BrowserSnapshotPlan
from device_price_service.fetchers.browser import BrowserFetcher
from device_price_service.fetchers.url_policy import UrlPolicyError


def test_browser_fetcher_rejects_url_before_launching_browser() -> None:
    fetcher = BrowserFetcher(Settings(_env_file=None))

    async def run() -> None:
        with pytest.raises(UrlPolicyError):
            await fetcher.fetch("https://evil.example/product", allowed_domains=["shop.example.cn"])

    asyncio.run(run())


def test_browser_snapshot_fetcher_rejects_url_before_launching_browser() -> None:
    fetcher = BrowserFetcher(Settings(_env_file=None))
    plan = BrowserSnapshotPlan(
        ready_selector=".ready",
        snapshot_selector=".product",
        dimensions=(),
    )

    async def run() -> None:
        with pytest.raises(UrlPolicyError):
            await fetcher.fetch_snapshots(
                "https://evil.example/product",
                allowed_domains=["shop.example.cn"],
                plan=plan,
            )

    asyncio.run(run())
