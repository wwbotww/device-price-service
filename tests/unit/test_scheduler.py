from typing import cast

import pytest

from device_price_service.crawlers.base import AdapterContext, BrandAdapter
from device_price_service.crawlers.registry import AdapterRegistry
from device_price_service.domain.crawl import (
    DiscoveredProduct,
    FetchResult,
    NormalizedProduct,
    ParsedProduct,
)
from device_price_service.scheduler import CrawlScheduler
from device_price_service.services.crawl_pipeline import CrawlPipeline


class SchedulerAdapter(BrandAdapter):
    brand_code = "APPLE"
    channel_code = "APPLE_CN_WEB"
    version = "test"

    async def discover(self, context: AdapterContext) -> list[DiscoveredProduct]:
        return []

    async def fetch_product(self, context: AdapterContext, item: DiscoveredProduct) -> FetchResult:
        raise NotImplementedError

    def parse_product(self, item: DiscoveredProduct, result: FetchResult) -> ParsedProduct:
        raise NotImplementedError

    def normalize(self, item: DiscoveredProduct, parsed: ParsedProduct) -> NormalizedProduct:
        raise NotImplementedError


def test_scheduler_configures_one_non_overlapping_job_per_adapter() -> None:
    registry = AdapterRegistry()
    registry.register(SchedulerAdapter())
    scheduler = CrawlScheduler(
        pipeline=cast(CrawlPipeline, object()),
        registry=registry,
        full_crawl_interval_hours=6,
    )
    scheduler.configure()

    jobs = scheduler.scheduler.get_jobs()
    assert len(jobs) == 1
    assert jobs[0].id == "full:APPLE_CN_WEB"
    assert jobs[0].max_instances == 1


def test_scheduler_refuses_to_start_without_adapters() -> None:
    scheduler = CrawlScheduler(
        pipeline=cast(CrawlPipeline, object()),
        registry=AdapterRegistry(),
        full_crawl_interval_hours=6,
    )
    with pytest.raises(RuntimeError, match="no brand adapters"):
        scheduler.start()
