"""User-facing V2 commands operate with exactly 13 tables and never require V1."""

from __future__ import annotations

import asyncio
import json
from datetime import datetime
from pathlib import Path

import pytest
from sqlalchemy import Engine, event, inspect, select
from sqlalchemy.orm import Session, sessionmaker
from typer.testing import CliRunner

import device_price_service.cli as cli
from device_price_service.config import Settings
from device_price_service.crawlers.apple import AppleCatalogConnector
from device_price_service.crawlers.catalog import CatalogConnectorRegistry
from device_price_service.db.base import Base
from device_price_service.db.catalog_models import (
    CatalogCrawlRecord,
    CatalogCrawlRun,
    SourceChannel,
)
from device_price_service.db.device_seed import seed_device_catalog
from device_price_service.domain.catalog_crawl import DiscoveredCatalogProduct
from device_price_service.domain.crawl import FetchResult
from device_price_service.domain.enums import FetchMethod
from device_price_service.runtime import build_catalog_runtime
from device_price_service.scheduler import CrawlScheduler

pytestmark = pytest.mark.integration
PRODUCT = DiscoveredCatalogProduct(
    external_product_id="iphone-fixture-pro",
    category_code="PHONE",
    url="https://www.apple.com.cn/shop/buy-iphone/iphone-fixture-pro",
)
BODY = (Path(__file__).parents[1] / "fixtures/apple/product_iphone.html").read_bytes()


class FixtureApple(AppleCatalogConnector):
    async def discover_products(self, context, request):
        return [PRODUCT]


class FixtureFetcher:
    def __init__(self):
        self.calls = []
        self.closed = False

    async def fetch(self, url, *, allowed_domains):
        assert url == PRODUCT.url
        self.calls.append(url)
        return FetchResult(
            request_url=url,
            final_url=url,
            status_code=200,
            headers={"content-type": "text/html"},
            body=BODY,
            fetched_at=datetime(2026, 9, 11, 8),
            duration_ms=1,
            fetch_method=FetchMethod.HTTP,
        )

    async def aclose(self):
        self.closed = True


def settings_for(engine: Engine, raw_path: Path) -> Settings:
    assert engine.url.database == "device_price_test"
    return Settings(
        _env_file=None,
        mysql_host=engine.url.host,
        mysql_port=engine.url.port,
        mysql_database=engine.url.database,
        mysql_user=engine.url.username,
        mysql_password=engine.url.password,
        raw_storage_path=raw_path,
        live_crawl_enabled=True,
    )


def snapshot(engine: Engine):
    with engine.connect() as connection:
        return {
            table.name: sorted(
                json.dumps(dict(row), sort_keys=True, default=str)
                for row in connection.execute(select(table)).mappings()
            )
            for table in Base.metadata.sorted_tables
        }


def test_seed_crawl_replay_audit_and_check_need_only_v2_tables(
    mysql_engine: Engine,
    session_factory: sessionmaker[Session],
    tmp_path: Path,
    monkeypatch,
):
    assert set(inspect(mysql_engine).get_table_names()) - {"alembic_version"} == cli.V2_TABLES
    settings = settings_for(mysql_engine, tmp_path / "raw")
    monkeypatch.setattr(cli, "get_settings", lambda: settings)
    monkeypatch.setattr(cli, "create_database_engine", lambda *_: mysql_engine)
    seeded = CliRunner().invoke(cli.app, ["db", "seed-devices", "--enable"])
    assert seeded.exit_code == 0, seeded.output
    connector = FixtureApple()
    runtime = build_catalog_runtime(settings)
    asyncio.run(runtime.http_fetcher.aclose())
    fetcher = FixtureFetcher()
    runtime.http_fetcher = fetcher
    runtime.pipeline.http_fetcher = fetcher
    registry = CatalogConnectorRegistry()
    registry.register(connector)
    runtime.registry = registry
    monkeypatch.setattr(cli, "_catalog_connector", lambda _: connector)
    monkeypatch.setattr(cli, "build_catalog_runtime", lambda _: runtime)
    collected = CliRunner().invoke(cli.app, ["catalog", "crawl", "--channel", "APPLE_CN_WEB"])
    assert collected.exit_code == 0, collected.output
    assert json.loads(collected.stdout)["accepted_count"] == 3
    assert fetcher.calls == [PRODUCT.url] and fetcher.closed
    with session_factory() as session:
        record_id = session.scalar(select(CatalogCrawlRecord.id))
    before = snapshot(mysql_engine)
    statements = []

    def capture(connection, cursor, statement, parameters, context, executemany):
        statements.append(statement.strip().split()[0].upper())

    event.listen(mysql_engine, "before_cursor_execute", capture)
    try:
        # No live permission is needed for read-only operational commands.
        monkeypatch.setattr(
            cli, "get_settings", lambda: settings.model_copy(update={"live_crawl_enabled": False})
        )
        replayed = CliRunner().invoke(cli.app, ["catalog", "replay", "--record-id", str(record_id)])
        assert replayed.exit_code == 0, replayed.output
        assert json.loads(replayed.stdout)["status"] == "SUCCEEDED"
        checked = CliRunner().invoke(cli.app, ["db", "check"])
        assert checked.exit_code == 0, checked.output
        assert "13 V2 tables" in checked.stdout
        audited = CliRunner().invoke(cli.app, ["db", "audit", "--check-artifacts"])
        assert audited.exit_code == 0, audited.output
        assert json.loads(audited.stdout)["healthy"] is True
    finally:
        event.remove(mysql_engine, "before_cursor_execute", capture)
    assert not set(statements) & {
        "INSERT",
        "UPDATE",
        "DELETE",
        "REPLACE",
        "CREATE",
        "DROP",
        "ALTER",
    }
    assert snapshot(mysql_engine) == before
    assert fetcher.calls == [PRODUCT.url]


def test_v2_check_accepts_preserved_legacy_tables(
    mysql_engine: Engine,
    migrated_session_factory: sessionmaker[Session],
    monkeypatch,
):
    monkeypatch.setattr(cli, "create_database_engine", lambda *_: mysql_engine)
    before = set(inspect(mysql_engine).get_table_names())
    result = CliRunner().invoke(cli.app, ["db", "check"])
    assert result.exit_code == 0, result.output
    assert "additional_tables_ignored=10" in result.stdout
    assert set(inspect(mysql_engine).get_table_names()) == before


def test_optional_scheduler_rechecks_enabled_state_at_execution(
    mysql_engine: Engine,
    session_factory: sessionmaker[Session],
    tmp_path: Path,
):
    with session_factory.begin() as session:
        seed_device_catalog(session, enable=True)
    settings = settings_for(mysql_engine, tmp_path / "raw")
    runtime = build_catalog_runtime(settings)
    asyncio.run(runtime.http_fetcher.aclose())
    fetcher = FixtureFetcher()
    runtime.http_fetcher = fetcher
    runtime.pipeline.http_fetcher = fetcher
    registry = CatalogConnectorRegistry()
    registry.register(FixtureApple())
    scheduler = CrawlScheduler(
        pipeline=runtime.pipeline,
        registry=registry,
        channel_codes=("APPLE_CN_WEB",),
        full_crawl_interval_hours=6,
    )
    try:
        scheduler.configure()
        asyncio.run(scheduler._run_connector("APPLE_CN_WEB"))
        with session_factory.begin() as session:
            run = session.scalar(select(CatalogCrawlRun))
            assert run.status == "SUCCEEDED" and run.trigger_type == "SCHEDULED"
            channel = session.scalar(
                select(SourceChannel).where(SourceChannel.code == "APPLE_CN_WEB")
            )
            channel.enabled = False
        before = snapshot(mysql_engine)
        asyncio.run(scheduler._run_connector("APPLE_CN_WEB"))
        assert fetcher.calls == [PRODUCT.url]
        assert snapshot(mysql_engine) == before
    finally:
        asyncio.run(runtime.aclose())
