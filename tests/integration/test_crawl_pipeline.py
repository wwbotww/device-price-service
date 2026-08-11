from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from device_price_service.crawlers.base import AdapterContext, BrandAdapter
from device_price_service.crawlers.registry import AdapterRegistry
from device_price_service.db.models import (
    CrawlRecord,
    CrawlRun,
    OfficialOffer,
    PriceCurrent,
    PriceHistory,
    Product,
    SalesChannel,
    Sku,
)
from device_price_service.db.repositories import CrawlRunRepository
from device_price_service.db.seed import seed_reference_data
from device_price_service.domain.crawl import (
    DiscoveredProduct,
    FetchResult,
    NormalizedOffer,
    NormalizedProduct,
    NormalizedSku,
    ParsedProduct,
)
from device_price_service.domain.enums import (
    Availability,
    FetchMethod,
    OriginalPriceType,
    RunType,
    TriggerType,
)
from device_price_service.services.artifact_store import RawArtifactStore
from device_price_service.services.crawl_pipeline import CrawlPipeline
from device_price_service.services.replay_service import ReplayService
from device_price_service.validation.rules import QualityValidator

pytestmark = pytest.mark.integration

PRODUCT_URL = "https://www.apple.com.cn/shop/product-fixture"


class StaticFetcher:
    async def fetch(self, url: str, *, allowed_domains: list[str]) -> FetchResult:
        assert url == PRODUCT_URL
        assert "www.apple.com.cn" in allowed_domains
        body = json.dumps(
            {"name": "Fixture Phone", "sku": "fixture-256", "price": "8999.00"}
        ).encode()
        return FetchResult(
            request_url=url,
            final_url=url,
            status_code=200,
            headers={"content-type": "application/json"},
            body=body,
            fetched_at=datetime(2026, 8, 8, 8),
            duration_ms=20,
            fetch_method=FetchMethod.HTTP,
        )


class FixtureAdapter(BrandAdapter):
    brand_code = "APPLE"
    channel_code = "APPLE_CN_WEB"
    version = "fixture-v1"

    async def discover(self, context: AdapterContext) -> list[DiscoveredProduct]:
        return [
            DiscoveredProduct(
                official_product_id="fixture-phone",
                url=PRODUCT_URL,
                category_code="PHONE",
            )
        ]

    async def fetch_product(self, context: AdapterContext, item: DiscoveredProduct) -> FetchResult:
        return await context.http.fetch(item.url, allowed_domains=context.allowed_domains)

    def parse_product(self, item: DiscoveredProduct, result: FetchResult) -> ParsedProduct:
        return ParsedProduct(source_url=result.final_url, payload=json.loads(result.body))

    def normalize(self, item: DiscoveredProduct, parsed: ParsedProduct) -> NormalizedProduct:
        return NormalizedProduct(
            brand_code=self.brand_code,
            channel_code=self.channel_code,
            category_code=item.category_code or "PHONE",
            official_product_id=item.official_product_id,
            name=str(parsed.payload["name"]),
            official_url=parsed.source_url,
            skus=[
                NormalizedSku(
                    official_sku_id=str(parsed.payload["sku"]),
                    name="Fixture Phone 256GB",
                    capacity="256GB",
                    attributes={"capacity": "256GB"},
                    spec_fingerprint="a" * 64,
                    offers=[
                        NormalizedOffer(
                            official_offer_id="fixture-offer",
                            source_url=parsed.source_url,
                            original_price=Decimal("9999.00"),
                            original_price_type=OriginalPriceType.MSRP,
                            current_price=Decimal(str(parsed.payload["price"])),
                            availability=Availability.ON_SALE,
                        )
                    ],
                )
            ],
        )


class SequencePriceFetcher:
    def __init__(self, prices: list[str]) -> None:
        self.prices = prices
        self.calls = 0

    async def fetch(self, url: str, *, allowed_domains: list[str]) -> FetchResult:
        assert url == PRODUCT_URL
        assert "www.apple.com.cn" in allowed_domains
        price = self.prices[self.calls]
        fetched_at = datetime(2026, 8, 8, 8) + timedelta(minutes=self.calls)
        self.calls += 1
        body = json.dumps({"name": "Fixture Phone", "sku": "fixture-256", "price": price}).encode()
        return FetchResult(
            request_url=url,
            final_url=url,
            status_code=200,
            headers={"content-type": "application/json"},
            body=body,
            fetched_at=fetched_at,
            duration_ms=20,
            fetch_method=FetchMethod.HTTP,
        )


class MutableDiscoveryAdapter(FixtureAdapter):
    def __init__(self) -> None:
        self.visible = True

    async def discover(self, context: AdapterContext) -> list[DiscoveredProduct]:
        if not self.visible:
            return []
        return await super().discover(context)


class MissingDetailFetcher(StaticFetcher):
    def __init__(self) -> None:
        self.missing = False
        self.calls = 0

    async def fetch(self, url: str, *, allowed_domains: list[str]) -> FetchResult:
        self.calls += 1
        if not self.missing:
            return await super().fetch(url, allowed_domains=allowed_domains)
        return FetchResult(
            request_url=url,
            final_url=url,
            status_code=404,
            headers={"content-type": "text/html"},
            body=b"fixture product not found",
            fetched_at=datetime(2026, 8, 9, 8) + timedelta(minutes=self.calls),
            duration_ms=10,
            fetch_method=FetchMethod.HTTP,
        )


def test_fixture_adapter_runs_full_pipeline_and_replays(
    mysql_engine: object,
    session_factory: sessionmaker[Session],
    tmp_path: Path,
) -> None:
    with session_factory.begin() as session:
        seed_reference_data(session)
        channel = session.scalar(select(SalesChannel).where(SalesChannel.code == "APPLE_CN_WEB"))
        assert channel is not None
        stale_run = CrawlRunRepository(session).start(
            channel_id=channel.id,
            run_type=RunType.FULL,
            trigger_type=TriggerType.MANUAL,
            adapter_version="stale-fixture",
            started_at=datetime(2020, 1, 1),
        )
        stale_run_id = stale_run.id

    fetcher = StaticFetcher()
    store = RawArtifactStore(tmp_path / "raw")
    validator = QualityValidator(price_change_threshold=0.3)
    pipeline = CrawlPipeline(
        engine=mysql_engine,  # type: ignore[arg-type]
        session_factory=session_factory,
        http_fetcher=fetcher,
        browser_fetcher=fetcher,
        artifact_store=store,
        validator=validator,
    )
    adapter = FixtureAdapter()
    outcome = asyncio.run(pipeline.run(adapter))

    assert outcome.discovered_count == 1
    assert outcome.success_count == 1
    assert outcome.failed_count == 0
    with session_factory() as session:
        run = session.get(CrawlRun, outcome.crawl_run_id)
        assert run is not None
        assert run.status == "SUCCEEDED"
        stale_run = session.get(CrawlRun, stale_run_id)
        assert stale_run is not None
        assert stale_run.status == "FAILED"
        assert stale_run.error_summary == {"STALE_RUN": 1}
        assert session.scalar(select(func.count()).select_from(Product)) == 1
        assert session.scalar(select(func.count()).select_from(Sku)) == 1
        assert session.scalar(select(func.count()).select_from(PriceCurrent)) == 1
        assert session.scalar(select(func.count()).select_from(PriceHistory)) == 1
        record = session.scalar(
            select(CrawlRecord).where(CrawlRecord.crawl_run_id == outcome.crawl_run_id)
        )
        assert record is not None
        assert record.raw_path is not None
        record_id = record.id

    registry = AdapterRegistry()
    registry.register(adapter)
    replay = ReplayService(
        session_factory=session_factory,
        artifact_store=store,
        registry=registry,
        validator=validator,
    ).replay(record_id)
    assert replay.validation.is_valid
    assert replay.product.official_product_id == "fixture-phone"


def test_large_price_change_requires_two_matching_observations(
    mysql_engine: object,
    session_factory: sessionmaker[Session],
    tmp_path: Path,
) -> None:
    with session_factory.begin() as session:
        seed_reference_data(session)

    fetcher = SequencePriceFetcher(["8999.00", "3999.00", "4999.00", "3999.00", "3999.00"])
    pipeline = CrawlPipeline(
        engine=mysql_engine,  # type: ignore[arg-type]
        session_factory=session_factory,
        http_fetcher=fetcher,
        browser_fetcher=fetcher,
        artifact_store=RawArtifactStore(tmp_path / "raw"),
        validator=QualityValidator(price_change_threshold=0.3),
    )
    adapter = FixtureAdapter()

    first = asyncio.run(pipeline.run(adapter))
    rejected = asyncio.run(pipeline.run(adapter))
    assert first.success_count == 1
    assert rejected.failed_count == 1
    with session_factory() as session:
        current = session.scalar(select(PriceCurrent))
        assert current is not None
        assert current.current_price == Decimal("8999.00")
        assert session.scalar(select(func.count()).select_from(PriceHistory)) == 1
        rejected_codes = set(
            session.scalars(
                select(CrawlRecord.error_code).where(
                    CrawlRecord.crawl_run_id == rejected.crawl_run_id
                )
            ).all()
        )
        assert rejected_codes == {
            "LARGE_PRICE_CHANGE_RECHECK",
            "LARGE_PRICE_CHANGE_UNCONFIRMED",
        }

    confirmed = asyncio.run(pipeline.run(adapter))
    assert confirmed.success_count == 1
    assert confirmed.failed_count == 0
    with session_factory() as session:
        current = session.scalar(select(PriceCurrent))
        assert current is not None
        assert current.current_price == Decimal("3999.00")
        assert session.scalar(select(func.count()).select_from(PriceHistory)) == 2


def test_missing_product_requires_three_runs_and_detail_404_confirmation(
    mysql_engine: object,
    session_factory: sessionmaker[Session],
    tmp_path: Path,
) -> None:
    with session_factory.begin() as session:
        seed_reference_data(session)

    fetcher = MissingDetailFetcher()
    adapter = MutableDiscoveryAdapter()
    pipeline = CrawlPipeline(
        engine=mysql_engine,  # type: ignore[arg-type]
        session_factory=session_factory,
        http_fetcher=fetcher,
        browser_fetcher=fetcher,
        artifact_store=RawArtifactStore(tmp_path / "raw"),
        validator=QualityValidator(price_change_threshold=0.3),
        missing_confirmation_runs=3,
    )
    assert asyncio.run(pipeline.run(adapter)).success_count == 1

    adapter.visible = False
    fetcher.missing = True
    first_missing = asyncio.run(pipeline.run(adapter))
    second_missing = asyncio.run(pipeline.run(adapter))
    with session_factory() as session:
        offer = session.scalar(select(OfficialOffer))
        assert offer is not None
        assert offer.consecutive_misses == 2
        assert offer.availability == "ON_SALE"
    third_missing = asyncio.run(pipeline.run(adapter))

    assert first_missing.failed_count == 1
    assert second_missing.failed_count == 1
    assert third_missing.failed_count == 1
    with session_factory() as session:
        product = session.scalar(select(Product))
        offer = session.scalar(select(OfficialOffer))
        current = session.scalar(select(PriceCurrent))
        histories = session.scalars(select(PriceHistory).order_by(PriceHistory.valid_from)).all()
        assert product is not None and product.lifecycle_status == "INACTIVE"
        assert offer is not None and offer.availability == "OFF_SHELF"
        assert offer.consecutive_misses == 3
        assert current is not None and current.current_price == Decimal("8999.00")
        assert [history.availability for history in histories] == ["ON_SALE", "OFF_SHELF"]
        confirmation = session.scalar(
            select(CrawlRecord).where(
                CrawlRecord.crawl_run_id == third_missing.crawl_run_id,
                CrawlRecord.error_code == "OFF_SHELF_CONFIRMED",
            )
        )
        assert confirmation is not None
