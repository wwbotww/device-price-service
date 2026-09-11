from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import cast

import pytest

from device_price_service.crawlers.catalog_builtin import build_catalog_registry
from device_price_service.domain.catalog_enums import CollectionTriggerType, RegionScope, RunStatus
from device_price_service.scheduler import CrawlScheduler
from device_price_service.services.catalog_crawl_pipeline import (
    CatalogConfigurationError,
    CatalogCrawlPipeline,
)


class PipelineStub:
    def __init__(self, *, disabled: str | None = None) -> None:
        self.disabled = disabled
        self.preflight = []
        self.calls = []

    def validate_source(self, connector) -> None:
        self.preflight.append(connector.channel_code)
        if connector.channel_code == self.disabled:
            raise CatalogConfigurationError("fixture source disabled")

    async def run(self, connector, request, *, trigger_type):
        self.calls.append((connector, request, trigger_type))
        self.validate_source(connector)
        return SimpleNamespace(
            status=RunStatus.SUCCEEDED, crawl_run_id=1, accepted_count=3, failed_count=0
        )


def scheduler(pipeline, channels):
    return CrawlScheduler(
        pipeline=cast(CatalogCrawlPipeline, pipeline),
        registry=build_catalog_registry(),
        channel_codes=channels,
        full_crawl_interval_hours=6,
    )


def test_only_explicit_device_sources_are_scheduled_with_non_overlap() -> None:
    pipeline = PipelineStub()
    service = scheduler(pipeline, ("apple_cn_web", "HUAWEI_CN_WEB"))
    service.configure()
    jobs = service.scheduler.get_jobs()
    assert [job.id for job in jobs] == ["full:APPLE_CN_WEB", "full:HUAWEI_CN_WEB"]
    assert all(job.max_instances == 1 and job.coalesce for job in jobs)
    assert pipeline.preflight == ["APPLE_CN_WEB", "HUAWEI_CN_WEB"]


@pytest.mark.parametrize(
    "channels",
    [
        (),
        ("",),
        ("APPLE_CN_WEB", "apple_cn_web"),
        ("SH_FGW_FRESH_RETAIL",),
        ("MOFCOM_FRESH_WHOLESALE",),
        ("UNKNOWN",),
    ],
)
def test_scheduler_rejects_empty_duplicate_unknown_and_government_selections(channels) -> None:
    service = scheduler(PipelineStub(), channels)
    with pytest.raises(ValueError):
        service.configure()
    assert service.scheduler.get_jobs() == []


def test_disabled_source_aborts_whole_configuration_before_any_jobs() -> None:
    service = scheduler(PipelineStub(disabled="HUAWEI_CN_WEB"), ("APPLE_CN_WEB", "HUAWEI_CN_WEB"))
    with pytest.raises(CatalogConfigurationError, match="disabled"):
        service.configure()
    assert service.scheduler.get_jobs() == []


def test_scheduled_job_uses_native_full_scope_and_scheduled_trigger() -> None:
    pipeline = PipelineStub()
    service = scheduler(pipeline, ("APPLE_CN_WEB",))
    asyncio.run(service._run_connector("APPLE_CN_WEB"))
    connector, request, trigger = pipeline.calls[0]
    assert connector.channel_code == "APPLE_CN_WEB"
    assert request.region_scope is RegionScope.NATIONAL and request.region_code == "CN"
    assert set(request.category_codes) == set(connector.default_category_codes)
    assert trigger is CollectionTriggerType.SCHEDULED


def test_job_revalidates_disabled_source_without_crashing_scheduler() -> None:
    pipeline = PipelineStub()
    service = scheduler(pipeline, ("APPLE_CN_WEB",))
    service.configure()
    pipeline.disabled = "APPLE_CN_WEB"
    asyncio.run(service._run_connector("APPLE_CN_WEB"))
    assert len(pipeline.preflight) == 2


def test_cancellation_stops_optional_scheduler(monkeypatch: pytest.MonkeyPatch) -> None:
    service = scheduler(PipelineStub(), ("APPLE_CN_WEB",))
    shutdown = []
    monkeypatch.setattr(service.scheduler, "start", lambda: None)
    monkeypatch.setattr(service.scheduler, "shutdown", lambda **kwargs: shutdown.append(kwargs))

    async def run_and_cancel():
        task = asyncio.create_task(service.serve())
        await asyncio.sleep(0)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(run_and_cancel())
    assert shutdown == [{"wait": False}]
