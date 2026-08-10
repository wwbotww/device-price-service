from __future__ import annotations

import asyncio
import json
from dataclasses import asdict
from typing import Annotated

import structlog
import typer
from sqlalchemy import inspect, select, text

from device_price_service.config import Settings, get_settings
from device_price_service.crawlers.builtin import build_builtin_registry
from device_price_service.db.models import (
    Brand,
    Category,
    CrawlRecord,
    CrawlRun,
    OfficialOffer,
    PriceCurrent,
    PriceHistory,
    Product,
    SalesChannel,
    Sku,
)
from device_price_service.db.seed import seed_reference_data
from device_price_service.db.session import create_database_engine, create_session_factory
from device_price_service.logging import configure_logging
from device_price_service.runtime import ApplicationRuntime, build_runtime
from device_price_service.scheduler import CrawlScheduler
from device_price_service.services.replay_service import ReplayService
from device_price_service.validation.rules import QualityValidator

app = typer.Typer(help="Official device price collection service")
db_app = typer.Typer(help="Database lifecycle commands")
app.add_typer(db_app, name="db")

EXPECTED_TABLES = {
    Brand.__tablename__,
    Category.__tablename__,
    Product.__tablename__,
    Sku.__tablename__,
    SalesChannel.__tablename__,
    OfficialOffer.__tablename__,
    PriceCurrent.__tablename__,
    PriceHistory.__tablename__,
    CrawlRun.__tablename__,
    CrawlRecord.__tablename__,
}


@app.callback()
def main(
    log_level: Annotated[str | None, typer.Option(help="Override LOG_LEVEL")] = None,
) -> None:
    settings = get_settings()
    configure_logging(log_level or settings.log_level)


@db_app.command("check")
def db_check() -> None:
    engine = create_database_engine()
    with engine.connect() as connection:
        connection.execute(text("SELECT 1"))
    actual_tables = set(inspect(engine).get_table_names())
    missing = EXPECTED_TABLES - actual_tables
    unexpected = actual_tables - EXPECTED_TABLES - {"alembic_version"}
    if missing or unexpected:
        typer.echo(f"missing={sorted(missing)} unexpected={sorted(unexpected)}", err=True)
        raise typer.Exit(code=1)
    typer.echo("database connection OK; all 10 application tables are present")


@db_app.command("seed")
def db_seed() -> None:
    engine = create_database_engine()
    factory = create_session_factory(engine)
    with factory.begin() as session:
        seed_reference_data(session)
    with factory() as session:
        counts = {
            "brands": len(session.scalars(select(Brand)).all()),
            "categories": len(session.scalars(select(Category)).all()),
            "channels": len(session.scalars(select(SalesChannel)).all()),
        }
    structlog.get_logger().info("reference_data_seeded", **counts)
    typer.echo(
        f"seed complete: {counts['brands']} brands, {counts['categories']} categories, "
        f"{counts['channels']} channels"
    )


@app.command("adapters")
def list_adapters() -> None:
    """List brand adapters compiled into this image without accessing a mall."""
    registry = build_builtin_registry()
    for adapter in registry:
        typer.echo(
            f"{adapter.brand_code}\t{adapter.channel_code}\t{adapter.version}"
        )


@app.command("crawl")
def crawl(
    brand: Annotated[str, typer.Option("--brand", "-b", help="APPLE or XIAOMI")],
    mode: Annotated[str, typer.Option(help="Only full is supported in V1")] = "full",
) -> None:
    """Run one manually triggered brand crawl after the production gate is enabled."""
    settings = get_settings()
    _require_live_crawl(settings.live_crawl_enabled)
    if mode.lower() != "full":
        raise typer.BadParameter("phase 3 only supports --mode full")
    asyncio.run(_crawl_once(settings, brand))


@app.command("replay")
def replay(
    record_id: Annotated[int, typer.Option("--record-id", min=1)],
) -> None:
    """Replay one stored raw artifact locally without contacting a mall."""
    asyncio.run(_replay_record(record_id))


@app.command("scheduler")
def scheduler() -> None:
    """Start the single-process scheduler after the production gate is enabled."""
    settings = get_settings()
    _require_live_crawl(settings.live_crawl_enabled)
    asyncio.run(_serve_scheduler(settings))


async def _crawl_once(settings: Settings, brand: str) -> None:
    runtime = build_runtime(settings)
    try:
        adapter = runtime.registry.get_by_brand(brand)
        outcome = await runtime.pipeline.run(adapter)
        typer.echo(json.dumps(asdict(outcome), ensure_ascii=False, sort_keys=True))
    finally:
        await runtime.aclose()


async def _replay_record(record_id: int) -> None:
    runtime = build_runtime()
    try:
        outcome = ReplayService(
            session_factory=runtime.session_factory,
            artifact_store=runtime.pipeline.artifact_store,
            registry=runtime.registry,
            validator=QualityValidator(
                price_change_threshold=runtime.settings.price_change_confirm_threshold
            ),
        ).replay(record_id)
        typer.echo(
            json.dumps(
                {
                    "record_id": outcome.record_id,
                    "brand_code": outcome.adapter.brand_code,
                    "product_id": outcome.product.official_product_id,
                    "sku_count": len(outcome.product.skus),
                    "valid": outcome.validation.is_valid,
                    "issues": [issue.code for issue in outcome.validation.issues],
                },
                ensure_ascii=False,
                sort_keys=True,
            )
        )
    finally:
        await runtime.aclose()


async def _serve_scheduler(settings: Settings) -> None:
    runtime: ApplicationRuntime = build_runtime(settings)
    crawl_scheduler = CrawlScheduler(
        pipeline=runtime.pipeline,
        registry=runtime.registry,
        full_crawl_interval_hours=runtime.settings.full_crawl_interval_hours,
    )
    try:
        await crawl_scheduler.serve()
    finally:
        await runtime.aclose()


def _require_live_crawl(enabled: bool) -> None:
    if not enabled:
        typer.echo(
            "live crawl is disabled; complete the phase 0 approvals and set "
            "LIVE_CRAWL_ENABLED=true",
            err=True,
        )
        raise typer.Exit(code=2)


if __name__ == "__main__":
    app()
