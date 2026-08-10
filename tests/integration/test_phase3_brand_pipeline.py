from __future__ import annotations

import asyncio
from datetime import datetime
from pathlib import Path

import pytest
from sqlalchemy import Engine, func, select
from sqlalchemy.orm import Session, sessionmaker

from device_price_service.crawlers.apple import AppleAdapter
from device_price_service.crawlers.registry import AdapterRegistry
from device_price_service.crawlers.xiaomi import XiaomiAdapter
from device_price_service.db.models import CrawlRecord, PriceCurrent, PriceHistory, Product, Sku
from device_price_service.db.seed import seed_reference_data
from device_price_service.domain.crawl import BrowserSnapshotPlan, CrawlOutcome, FetchResult
from device_price_service.domain.enums import FetchMethod
from device_price_service.services.artifact_store import RawArtifactStore
from device_price_service.services.crawl_pipeline import CrawlPipeline
from device_price_service.services.replay_service import ReplayService
from device_price_service.validation.rules import QualityValidator

pytestmark = pytest.mark.integration

FIXTURES = Path(__file__).parents[1] / "fixtures"
APPLE_DISCOVERY_URL = "https://www.apple.com.cn/shop/buy-iphone"
APPLE_PRODUCT_URL = "https://www.apple.com.cn/shop/buy-iphone/iphone-fixture-pro"
XIAOMI_DISCOVERY_URL = "https://www.mi.com/shop/"
XIAOMI_PRODUCT_URL = "https://www.mi.com/shop/buy/detail?product_id=91002"


class Phase3FixtureFetcher:
    async def fetch(self, url: str, *, allowed_domains: list[str]) -> FetchResult:
        if url == APPLE_DISCOVERY_URL:
            path = FIXTURES / "apple" / "discovery_iphone_one.html"
        elif url == APPLE_PRODUCT_URL:
            path = FIXTURES / "apple" / "product_iphone.html"
        elif url == XIAOMI_DISCOVERY_URL:
            path = FIXTURES / "xiaomi" / "discovery_one.html"
        else:
            raise AssertionError(f"unexpected fixture URL: {url}")
        assert urlsplit_host(url) in allowed_domains
        return self._result(url, path.read_bytes(), FetchMethod.HTTP, "text/html")

    async def fetch_snapshots(
        self,
        url: str,
        *,
        allowed_domains: list[str],
        plan: BrowserSnapshotPlan,
    ) -> FetchResult:
        assert url == XIAOMI_PRODUCT_URL
        assert "www.mi.com" in allowed_domains
        assert [dimension.name for dimension in plan.dimensions] == ["version", "color"]
        path = FIXTURES / "xiaomi" / "product_snapshots.json"
        return self._result(url, path.read_bytes(), FetchMethod.BROWSER, "application/json")

    @staticmethod
    def _result(
        url: str,
        body: bytes,
        method: FetchMethod,
        content_type: str,
    ) -> FetchResult:
        return FetchResult(
            request_url=url,
            final_url=url,
            status_code=200,
            headers={"content-type": content_type},
            body=body,
            fetched_at=datetime(2026, 8, 8, 8),
            duration_ms=5,
            fetch_method=method,
        )


def urlsplit_host(url: str) -> str:
    return "www.apple.com.cn" if "apple.com.cn" in url else "www.mi.com"


def test_apple_and_xiaomi_fixtures_run_pipeline_and_replay(
    mysql_engine: Engine,
    session_factory: sessionmaker[Session],
    tmp_path: Path,
) -> None:
    with session_factory.begin() as session:
        seed_reference_data(session)

    fetcher = Phase3FixtureFetcher()
    store = RawArtifactStore(tmp_path / "raw")
    validator = QualityValidator(price_change_threshold=0.3)
    pipeline = CrawlPipeline(
        engine=mysql_engine,
        session_factory=session_factory,
        http_fetcher=fetcher,
        browser_fetcher=fetcher,
        artifact_store=store,
        validator=validator,
    )
    apple = AppleAdapter(discovery_pages=(("PHONE", APPLE_DISCOVERY_URL),))
    xiaomi = XiaomiAdapter(discovery_url=XIAOMI_DISCOVERY_URL)

    async def run_both() -> tuple[CrawlOutcome, CrawlOutcome]:
        return await pipeline.run(apple), await pipeline.run(xiaomi)

    apple_outcome, xiaomi_outcome = asyncio.run(run_both())
    assert apple_outcome.success_count == 1
    assert xiaomi_outcome.success_count == 1

    with session_factory() as session:
        assert session.scalar(select(func.count()).select_from(Product)) == 2
        assert session.scalar(select(func.count()).select_from(Sku)) == 6
        assert session.scalar(select(func.count()).select_from(PriceCurrent)) == 6
        assert session.scalar(select(func.count()).select_from(PriceHistory)) == 6
        records = session.scalars(select(CrawlRecord).order_by(CrawlRecord.id)).all()
        assert len(records) == 2
        record_ids = [record.id for record in records]

    registry = AdapterRegistry()
    registry.register(apple)
    registry.register(xiaomi)
    replay = ReplayService(
        session_factory=session_factory,
        artifact_store=store,
        registry=registry,
        validator=validator,
    )
    outcomes = [replay.replay(record_id) for record_id in record_ids]
    assert all(outcome.validation.is_valid for outcome in outcomes)
    assert {outcome.product.brand_code for outcome in outcomes} == {"APPLE", "XIAOMI"}
