from __future__ import annotations

import asyncio
from datetime import datetime
from pathlib import Path
from urllib.parse import urlsplit

import pytest
from sqlalchemy import Engine, func, select
from sqlalchemy.orm import Session, sessionmaker

from device_price_service.crawlers.huawei import HuaweiAdapter
from device_price_service.crawlers.oppo import OppoAdapter
from device_price_service.crawlers.registry import AdapterRegistry
from device_price_service.crawlers.vivo import VivoAdapter
from device_price_service.db.models import CrawlRecord, PriceCurrent, PriceHistory, Product, Sku
from device_price_service.db.seed import seed_reference_data
from device_price_service.domain.crawl import BrowserSnapshotPlan, FetchResult
from device_price_service.domain.enums import FetchMethod
from device_price_service.services.artifact_store import RawArtifactStore
from device_price_service.services.crawl_pipeline import CrawlPipeline
from device_price_service.services.replay_service import ReplayService
from device_price_service.validation.rules import QualityValidator

pytestmark = pytest.mark.integration

FIXTURES = Path(__file__).parents[1] / "fixtures"
HUAWEI_DISCOVERY_URL = "https://www.vmall.com/"
HUAWEI_PRODUCT_URL = "https://www.vmall.com/product/42002.html"
OPPO_DISCOVERY_URL = "https://www.opposhop.cn/"
OPPO_PRODUCT_URL = "https://www.opposhop.cn/cn/web/products/43001.html"
VIVO_DISCOVERY_URL = "https://shop.vivo.com.cn/product/10001186"
VIVO_PRODUCT_URL = "https://shop.vivo.com.cn/product/44001"


class Phase4FixtureFetcher:
    _DISCOVERY_FIXTURES = {
        HUAWEI_DISCOVERY_URL: FIXTURES / "huawei" / "discovery_one.html",
        OPPO_DISCOVERY_URL: FIXTURES / "oppo" / "discovery_one.html",
        VIVO_DISCOVERY_URL: FIXTURES / "vivo" / "discovery_one.html",
    }
    _PRODUCT_FIXTURES = {
        HUAWEI_PRODUCT_URL: FIXTURES / "huawei" / "product_snapshots.json",
        OPPO_PRODUCT_URL: FIXTURES / "oppo" / "product_snapshots.json",
        VIVO_PRODUCT_URL: FIXTURES / "vivo" / "product_snapshots.json",
    }

    async def fetch(self, url: str, *, allowed_domains: list[str]) -> FetchResult:
        path = self._DISCOVERY_FIXTURES.get(url)
        if path is None:
            raise AssertionError(f"unexpected fixture URL: {url}")
        assert urlsplit(url).hostname in allowed_domains
        return self._result(url, path.read_bytes(), FetchMethod.HTTP, "text/html")

    async def fetch_snapshots(
        self,
        url: str,
        *,
        allowed_domains: list[str],
        plan: BrowserSnapshotPlan,
    ) -> FetchResult:
        path = self._PRODUCT_FIXTURES.get(url)
        if path is None:
            raise AssertionError(f"unexpected fixture URL: {url}")
        assert urlsplit(url).hostname in allowed_domains
        assert [dimension.name for dimension in plan.dimensions] == ["version", "color"]
        assert all(dimension.optional for dimension in plan.dimensions)
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
            fetched_at=datetime(2026, 8, 10, 8),
            duration_ms=5,
            fetch_method=method,
        )


def test_phase4_brand_fixtures_are_idempotent_and_replayable(
    mysql_engine: Engine,
    session_factory: sessionmaker[Session],
    tmp_path: Path,
) -> None:
    with session_factory.begin() as session:
        seed_reference_data(session)

    fetcher = Phase4FixtureFetcher()
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
    adapters = [
        HuaweiAdapter(discovery_url=HUAWEI_DISCOVERY_URL),
        OppoAdapter(discovery_url=OPPO_DISCOVERY_URL),
        VivoAdapter(discovery_url=VIVO_DISCOVERY_URL),
    ]

    async def run_two_cycles() -> None:
        for _ in range(2):
            for adapter in adapters:
                outcome = await pipeline.run(adapter)
                assert outcome.success_count == 1
                assert outcome.failed_count == 0

    asyncio.run(run_two_cycles())

    with session_factory() as session:
        assert session.scalar(select(func.count()).select_from(Product)) == 3
        assert session.scalar(select(func.count()).select_from(Sku)) == 6
        assert session.scalar(select(func.count()).select_from(PriceCurrent)) == 6
        assert session.scalar(select(func.count()).select_from(PriceHistory)) == 6
        records = session.scalars(select(CrawlRecord).order_by(CrawlRecord.id)).all()
        assert len(records) == 6
        record_ids = [record.id for record in records]

    registry = AdapterRegistry()
    for adapter in adapters:
        registry.register(adapter)
    replay = ReplayService(
        session_factory=session_factory,
        artifact_store=store,
        registry=registry,
        validator=validator,
    )
    outcomes = [replay.replay(record_id) for record_id in record_ids]
    assert all(outcome.validation.is_valid for outcome in outcomes)
    assert {outcome.product.brand_code for outcome in outcomes} == {"HUAWEI", "OPPO", "VIVO"}
