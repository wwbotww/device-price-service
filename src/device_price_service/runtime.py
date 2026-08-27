from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import Engine
from sqlalchemy.orm import Session, sessionmaker

from device_price_service.config import Settings, get_settings
from device_price_service.crawlers.builtin import build_builtin_registry
from device_price_service.crawlers.catalog import CatalogDatasetConnectorRegistry
from device_price_service.crawlers.catalog_builtin import build_catalog_dataset_registry
from device_price_service.crawlers.registry import AdapterRegistry
from device_price_service.db.session import create_database_engine, create_session_factory
from device_price_service.fetchers.browser import BrowserFetcher
from device_price_service.fetchers.http import HttpFetcher
from device_price_service.normalization.fresh_food import build_fresh_food_rule_registry
from device_price_service.services.artifact_store import RawArtifactStore
from device_price_service.services.catalog_crawl_pipeline import CatalogCrawlPipeline
from device_price_service.services.crawl_pipeline import CrawlPipeline
from device_price_service.validation.rules import QualityValidator


@dataclass(slots=True)
class ApplicationRuntime:
    settings: Settings
    engine: Engine
    session_factory: sessionmaker[Session]
    http_fetcher: HttpFetcher
    browser_fetcher: BrowserFetcher
    registry: AdapterRegistry
    pipeline: CrawlPipeline

    async def aclose(self) -> None:
        await self.http_fetcher.aclose()
        self.engine.dispose()


@dataclass(slots=True)
class CatalogApplicationRuntime:
    settings: Settings
    engine: Engine
    session_factory: sessionmaker[Session]
    http_fetcher: HttpFetcher
    browser_fetcher: BrowserFetcher
    registry: CatalogDatasetConnectorRegistry
    pipeline: CatalogCrawlPipeline

    async def aclose(self) -> None:
        await self.http_fetcher.aclose()
        self.engine.dispose()


def build_runtime(settings: Settings | None = None) -> ApplicationRuntime:
    resolved = settings or get_settings()
    engine = create_database_engine(resolved)
    session_factory = create_session_factory(engine)
    http_fetcher = HttpFetcher(resolved)
    browser_fetcher = BrowserFetcher(resolved)
    registry = build_builtin_registry()
    pipeline = CrawlPipeline(
        engine=engine,
        session_factory=session_factory,
        http_fetcher=http_fetcher,
        browser_fetcher=browser_fetcher,
        artifact_store=RawArtifactStore(resolved.raw_storage_path),
        validator=QualityValidator(price_change_threshold=resolved.price_change_confirm_threshold),
        stale_run_after_minutes=resolved.crawl_stale_after_minutes,
        missing_confirmation_runs=resolved.missing_confirmation_runs,
        discovery_count_floor_ratio=resolved.discovery_count_floor_ratio,
    )
    return ApplicationRuntime(
        settings=resolved,
        engine=engine,
        session_factory=session_factory,
        http_fetcher=http_fetcher,
        browser_fetcher=browser_fetcher,
        registry=registry,
        pipeline=pipeline,
    )


def build_catalog_runtime(settings: Settings | None = None) -> CatalogApplicationRuntime:
    resolved = settings or get_settings()
    engine = create_database_engine(resolved)
    session_factory = create_session_factory(engine)
    http_fetcher = HttpFetcher(resolved)
    browser_fetcher = BrowserFetcher(resolved)
    registry = build_catalog_dataset_registry()
    pipeline = CatalogCrawlPipeline(
        engine=engine,
        session_factory=session_factory,
        http_fetcher=http_fetcher,
        browser_fetcher=browser_fetcher,
        artifact_store=RawArtifactStore(resolved.raw_storage_path),
        category_rules=build_fresh_food_rule_registry(),
    )
    return CatalogApplicationRuntime(
        settings=resolved,
        engine=engine,
        session_factory=session_factory,
        http_fetcher=http_fetcher,
        browser_fetcher=browser_fetcher,
        registry=registry,
        pipeline=pipeline,
    )
