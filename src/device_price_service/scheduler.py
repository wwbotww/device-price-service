from __future__ import annotations

import asyncio

import structlog
from apscheduler.schedulers.asyncio import AsyncIOScheduler  # type: ignore[import-untyped]

from device_price_service.crawlers.catalog import CatalogConnector, CatalogConnectorRegistry
from device_price_service.domain.catalog_crawl import CatalogCollectionRequest
from device_price_service.domain.catalog_enums import CollectionTriggerType, RegionScope
from device_price_service.services.catalog_crawl_pipeline import CatalogCrawlPipeline


class CrawlScheduler:
    def __init__(
        self,
        *,
        pipeline: CatalogCrawlPipeline,
        registry: CatalogConnectorRegistry,
        channel_codes: tuple[str, ...],
        full_crawl_interval_hours: int,
    ) -> None:
        self.pipeline = pipeline
        self.registry = registry
        self.channel_codes = tuple(code.strip().upper() for code in channel_codes)
        self.full_crawl_interval_hours = full_crawl_interval_hours
        self.scheduler = AsyncIOScheduler(timezone="UTC")
        self.logger = structlog.get_logger()

    def configure(self) -> None:
        if not self.channel_codes or any(not code for code in self.channel_codes):
            raise ValueError("select at least one device source with --channel")
        if len(set(self.channel_codes)) != len(self.channel_codes):
            raise ValueError("scheduler channels must not be duplicated")
        connectors: list[CatalogConnector] = []
        # Validate the entire explicit selection before installing any jobs.
        for channel_code in self.channel_codes:
            connector = self.registry.get(channel_code)
            if not isinstance(connector, CatalogConnector):
                raise ValueError("scheduler only supports explicitly selected device sources")
            self.pipeline.validate_source(connector)
            connectors.append(connector)
        for connector in connectors:
            self.scheduler.add_job(
                self._run_connector,
                trigger="interval",
                hours=self.full_crawl_interval_hours,
                id=f"full:{connector.channel_code}",
                args=[connector.channel_code],
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
            raise ValueError("no device sources were selected")
        self.scheduler.start()
        try:
            await asyncio.Event().wait()
        finally:
            self.scheduler.shutdown(wait=False)

    async def _run_connector(self, channel_code: str) -> None:
        connector = self.registry.get(channel_code)
        if not isinstance(connector, CatalogConnector):
            raise ValueError("only device sources can be scheduled")
        try:
            outcome = await self.pipeline.run(
                connector,
                CatalogCollectionRequest(
                    region_scope=RegionScope.NATIONAL,
                    region_code="CN",
                    category_codes=connector.default_category_codes,
                ),
                trigger_type=CollectionTriggerType.SCHEDULED,
            )
            self.logger.info(
                "scheduled_crawl_finished",
                channel_code=channel_code,
                crawl_run_id=outcome.crawl_run_id,
                status=outcome.status.value,
                accepted_count=outcome.accepted_count,
                failed_count=outcome.failed_count,
            )
        except Exception:
            self.logger.exception("scheduled_crawl_failed", channel_code=channel_code)
