from __future__ import annotations

import asyncio

import structlog
from apscheduler.schedulers.asyncio import AsyncIOScheduler  # type: ignore[import-untyped]

from device_price_service.crawlers.registry import AdapterRegistry
from device_price_service.services.crawl_pipeline import CrawlPipeline


class CrawlScheduler:
    def __init__(
        self,
        *,
        pipeline: CrawlPipeline,
        registry: AdapterRegistry,
        full_crawl_interval_hours: int,
    ) -> None:
        self.pipeline = pipeline
        self.registry = registry
        self.full_crawl_interval_hours = full_crawl_interval_hours
        self.scheduler = AsyncIOScheduler(timezone="UTC")
        self.logger = structlog.get_logger()

    def configure(self) -> None:
        for adapter in self.registry:
            self.scheduler.add_job(
                self._run_adapter,
                trigger="interval",
                hours=self.full_crawl_interval_hours,
                id=f"full:{adapter.channel_code}",
                args=[adapter.channel_code],
                max_instances=1,
                coalesce=True,
                misfire_grace_time=900,
                replace_existing=True,
            )

    def start(self) -> None:
        asyncio.run(self.serve())

    async def serve(self) -> None:
        if not self.scheduler.get_jobs():
            self.configure()
        if not self.scheduler.get_jobs():
            raise RuntimeError("no brand adapters are registered")
        self.scheduler.start()
        try:
            await asyncio.Event().wait()
        finally:
            self.scheduler.shutdown(wait=False)

    async def _run_adapter(self, channel_code: str) -> None:
        adapter = self.registry.get(channel_code)
        try:
            outcome = await self.pipeline.run(adapter)
            self.logger.info(
                "scheduled_crawl_finished",
                channel_code=channel_code,
                crawl_run_id=outcome.crawl_run_id,
                success_count=outcome.success_count,
                failed_count=outcome.failed_count,
            )
        except Exception:
            self.logger.exception("scheduled_crawl_failed", channel_code=channel_code)
