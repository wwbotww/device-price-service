from __future__ import annotations

import asyncio
import json
from collections.abc import Callable
from dataclasses import asdict
from typing import Annotated

import structlog
import typer
from sqlalchemy import inspect, select, text

from device_price_service.config import Settings, get_settings
from device_price_service.crawlers.base import AdapterContext
from device_price_service.crawlers.catalog import CatalogConnector, CatalogDatasetConnector
from device_price_service.crawlers.catalog_builtin import build_catalog_registry
from device_price_service.crawlers.mofcom_fresh import (
    MOFCOM_FRESH_CATEGORY_CODE,
    MOFCOM_SUPPORTED_COMMODITIES,
)
from device_price_service.crawlers.shanghai_fresh import (
    SHANGHAI_FRESH_CATEGORY_CODE,
    SHANGHAI_FRESH_REGION_CODE,
)
from device_price_service.db.catalog_models import (
    CatalogBrand,
    CatalogCrawlRecord,
    CatalogCrawlRun,
    CatalogItem,
    CatalogPriceCurrent,
    CatalogPriceObservationRecord,
    ItemVariant,
    ListingMatch,
    ListingRevision,
    Merchant,
    SourceChannel,
    SourceListing,
    TaxonomyCategory,
)
from device_price_service.db.catalog_seed import (
    FRESH_CATEGORY_SEEDS,
    MOFCOM_FRESH_CHANNEL_CODE,
    SHANGHAI_FRESH_CHANNEL_CODE,
    seed_fresh_categories,
    seed_mofcom_fresh_source,
    seed_shanghai_fresh_source,
)
from device_price_service.db.device_seed import DEVICE_CATEGORY_SEEDS, seed_device_catalog
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
from device_price_service.db.mysql_compat import compatibility_mode, validate_mysql_version
from device_price_service.db.seed import seed_reference_data
from device_price_service.db.session import create_database_engine, create_session_factory
from device_price_service.domain.catalog_crawl import CatalogCollectionRequest
from device_price_service.domain.catalog_enums import RegionScope
from device_price_service.domain.crawl import FetchResult
from device_price_service.domain.enums import RunStatus
from device_price_service.fetchers.browser import BrowserFetcher
from device_price_service.fetchers.http import HttpFetcher
from device_price_service.fetchers.url_policy import UrlPolicy
from device_price_service.logging import configure_logging
from device_price_service.normalization.catalog_rules import CategoryRule
from device_price_service.runtime import (
    build_catalog_rule_registry,
    build_catalog_runtime,
)
from device_price_service.services.catalog_crawl_pipeline import CatalogConfigurationError
from device_price_service.services.catalog_preparation import (
    PreparedCatalogRows,
    prepare_catalog_product,
    prepare_catalog_rows,
)
from device_price_service.services.database_audit import audit_database

app = typer.Typer(help="Auditable public price collection service")
db_app = typer.Typer(help="Database lifecycle commands")
catalog_app = typer.Typer(help="V2 government and official-device price collection commands")
app.add_typer(db_app, name="db")
app.add_typer(catalog_app, name="catalog")

V1_TABLES = {
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
V2_TABLES = {
    CatalogBrand.__tablename__,
    TaxonomyCategory.__tablename__,
    CatalogItem.__tablename__,
    ItemVariant.__tablename__,
    SourceChannel.__tablename__,
    Merchant.__tablename__,
    SourceListing.__tablename__,
    ListingRevision.__tablename__,
    ListingMatch.__tablename__,
    CatalogPriceObservationRecord.__tablename__,
    CatalogPriceCurrent.__tablename__,
    CatalogCrawlRun.__tablename__,
    CatalogCrawlRecord.__tablename__,
}
EXPECTED_TABLES = V1_TABLES | V2_TABLES


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
        raw_version = str(connection.scalar(text("SELECT VERSION()")))
        version = validate_mysql_version(raw_version)
        session_time_zone = str(connection.scalar(text("SELECT @@session.time_zone")))
    actual_tables = set(inspect(engine).get_table_names())
    missing = EXPECTED_TABLES - actual_tables
    unexpected = actual_tables - EXPECTED_TABLES - {"alembic_version"}
    if missing or unexpected:
        typer.echo(f"missing={sorted(missing)} unexpected={sorted(unexpected)}", err=True)
        raise typer.Exit(code=1)
    typer.echo(
        f"database connection OK; all {len(EXPECTED_TABLES)} application tables are present; "
        f"server={raw_version}; mode={compatibility_mode(version)}; "
        f"session_time_zone={session_time_zone}"
    )


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


@db_app.command("seed-v2-fresh")
def db_seed_v2_fresh() -> None:
    """Seed the implemented V2 fresh-food category tree and rule references."""

    engine = create_database_engine()
    factory = create_session_factory(engine)
    with factory.begin() as session:
        categories = seed_fresh_categories(session)
    typer.echo(f"V2 fresh category seed complete: {len(categories)} categories")


@db_app.command("seed-v2-government")
def db_seed_v2_government(
    enable: Annotated[
        bool,
        typer.Option(
            "--enable",
            help="Explicitly enable both government sources after seeding them",
        ),
    ] = False,
) -> None:
    """Seed fresh rules and government sources, disabled by default."""

    engine = create_database_engine()
    factory = create_session_factory(engine)
    with factory.begin() as session:
        categories = seed_fresh_categories(session)
        channels = (
            seed_shanghai_fresh_source(session, enable=enable),
            seed_mofcom_fresh_source(session, enable=enable),
        )
    typer.echo(
        f"V2 government seed complete: {len(categories)} categories; "
        f"channels={','.join(channel.code for channel in channels)}; "
        f"enabled={all(bool(channel.enabled) for channel in channels)}"
    )


@db_app.command("audit")
def db_audit() -> None:
    """Run read-only consistency and freshness checks for monitoring."""
    settings = get_settings()
    engine = create_database_engine(settings)
    with engine.connect() as connection:
        report = audit_database(
            connection,
            stale_run_minutes=settings.crawl_stale_after_minutes,
            stale_price_hours=settings.full_crawl_interval_hours * 2,
            require_completed_runs=settings.live_crawl_enabled,
        )
    typer.echo(
        json.dumps(
            {
                "healthy": report.healthy,
                "critical": report.critical,
                "warnings": report.warnings,
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    )
    if not report.healthy:
        raise typer.Exit(code=1)


@db_app.command("seed-devices")
def db_seed_devices(
    enable: Annotated[
        bool,
        typer.Option(
            "--enable",
            help="Explicitly enable V2 device sources; does not start collection",
        ),
    ] = False,
) -> None:
    """Seed five V2 device brands, seven categories and disabled official sources."""
    engine = create_database_engine()
    try:
        factory = create_session_factory(engine)
        with factory.begin() as session:
            channels = seed_device_catalog(session, enable=enable)
            enabled_count = sum(bool(channel.enabled) for channel in channels)
        typer.echo(
            f"V2 device seed complete: 5 brands, {len(DEVICE_CATEGORY_SEEDS)} categories, "
            f"{len(channels)} sources; enabled={enabled_count}; no collection started"
        )
    finally:
        engine.dispose()


@app.command("adapters")
def list_adapters() -> None:
    """Retired V1 source listing; use catalog sources."""
    raise typer.BadParameter("V1 adapters are retired; use catalog sources")


@app.command("crawl")
def crawl(
    brand: Annotated[
        str,
        typer.Option("--brand", "-b", help="Retired; use catalog crawl --channel"),
    ],
    mode: Annotated[str, typer.Option(help="Only full is supported in V1")] = "full",
) -> None:
    """Retired V1 collection; use catalog crawl."""
    settings = get_settings()
    _require_live_crawl(settings.live_crawl_enabled)
    _require_legacy_brand(brand)


@app.command("smoke")
def smoke(
    brand: Annotated[
        str,
        typer.Option("--brand", "-b", help="Retired; use catalog smoke --channel"),
    ],
    max_products: Annotated[int, typer.Option(min=1, max=5)] = 1,
) -> None:
    """Retired V1 live validation; use catalog smoke."""
    settings = get_settings()
    _require_live_crawl(settings.live_crawl_enabled)
    _require_legacy_brand(brand)


@app.command("replay")
def replay(
    record_id: Annotated[int, typer.Option("--record-id", min=1)],
) -> None:
    """Retired V1 replay; unified V2 replay is not available yet."""
    raise typer.BadParameter("V1 replay is retired; catalog replay is planned for phase L")


@app.command("scheduler")
def scheduler() -> None:
    """Retired V1 scheduler; optional V2 scheduling is not available yet."""
    raise typer.BadParameter("V1 scheduler is retired; V2 scheduling is planned for phase L")


@catalog_app.command("sources")
def list_catalog_sources() -> None:
    """List V2 connectors without accessing a source or database."""

    for connector in build_catalog_registry():
        typer.echo(f"{connector.channel_code}\t{connector.connector_code}\t{connector.version}")


@catalog_app.command("smoke")
def catalog_smoke(
    channel: Annotated[
        str,
        typer.Option("--channel", "-c", help="Registered government or official-device channel"),
    ] = SHANGHAI_FRESH_CHANNEL_CODE,
    commodity: Annotated[
        str | None,
        typer.Option(
            "--commodity",
            help="MOFCOM commodity code or ALL; omit for other sources",
        ),
    ] = None,
    max_products: Annotated[
        int,
        typer.Option(min=1, max=5, help="Maximum product pages to smoke; datasets are unchanged"),
    ] = 1,
) -> None:
    """Fetch, normalize and validate source evidence without connecting to MySQL."""

    settings = get_settings()
    _require_live_crawl(settings.live_crawl_enabled)
    asyncio.run(_catalog_smoke_once(settings, channel, commodity, max_products))


@catalog_app.command("crawl")
def catalog_crawl(
    channel: Annotated[
        str,
        typer.Option("--channel", "-c", help="Enabled government or official-device channel"),
    ] = SHANGHAI_FRESH_CHANNEL_CODE,
    commodity: Annotated[
        str | None,
        typer.Option(
            "--commodity",
            help="MOFCOM commodity code or ALL; omit for other sources",
        ),
    ] = None,
) -> None:
    """Collect official products or latest government documents into the V2 tables."""

    settings = get_settings()
    _require_live_crawl(settings.live_crawl_enabled)
    asyncio.run(_catalog_crawl_once(settings, channel, commodity))




def _shanghai_catalog_request() -> CatalogCollectionRequest:
    return CatalogCollectionRequest(
        region_scope=RegionScope.CITY,
        region_code=SHANGHAI_FRESH_REGION_CODE,
        category_codes=(SHANGHAI_FRESH_CATEGORY_CODE,),
    )


def _catalog_requests(
    channel: str,
    commodity: str | None,
) -> list[CatalogCollectionRequest]:
    normalized_channel = channel.strip().upper()
    normalized_commodity = commodity.strip().upper() if commodity else None
    if normalized_channel == SHANGHAI_FRESH_CHANNEL_CODE:
        if normalized_commodity is not None:
            raise typer.BadParameter("--commodity is only supported by the MOFCOM source")
        return [_shanghai_catalog_request()]
    if normalized_channel == MOFCOM_FRESH_CHANNEL_CODE:
        supported = [mapping.commodity_code for mapping in MOFCOM_SUPPORTED_COMMODITIES]
        if normalized_commodity in {None, "ALL"}:
            selected = supported
        elif normalized_commodity in supported:
            selected = [normalized_commodity]
        else:
            raise typer.BadParameter(
                f"unsupported MOFCOM commodity {normalized_commodity!r}; "
                f"choose one of {', '.join(supported)} or ALL"
            )
        return [
            CatalogCollectionRequest(
                region_scope=RegionScope.MULTI,
                region_code="*",
                category_codes=(MOFCOM_FRESH_CATEGORY_CODE,),
                source_item_codes=(code,),
            )
            for code in selected
        ]
    connector = _catalog_connector(normalized_channel)
    if isinstance(connector, CatalogConnector):
        if normalized_commodity is not None:
            raise typer.BadParameter("--commodity is only supported by the MOFCOM source")
        return [
            CatalogCollectionRequest(
                region_scope=RegionScope.NATIONAL,
                region_code="CN",
                category_codes=connector.default_category_codes,
            )
        ]
    raise typer.BadParameter(f"catalog source does not define a request scope: {channel}")


def _catalog_connector(channel: str) -> CatalogConnector | CatalogDatasetConnector:
    try:
        return build_catalog_registry().get(channel)
    except ValueError as error:
        raise typer.BadParameter(str(error)) from error


def _catalog_seed_rule_resolver() -> Callable[[str], CategoryRule]:
    registry = build_catalog_rule_registry()
    categories = {seed.code: seed for seed in (*FRESH_CATEGORY_SEEDS, *DEVICE_CATEGORY_SEEDS)}

    def resolve(category_code: str) -> CategoryRule:
        try:
            category = categories[category_code]
        except KeyError as error:
            raise ValueError(f"category has no configured seed: {category_code}") from error
        return registry.get(category.attribute_profile_code, category.attribute_profile_version)

    return resolve


def _catalog_smoke_prices(prepared: PreparedCatalogRows) -> list[dict[str, object]]:
    return [
        {
            "product_id": row.row.item.external_product_id,
            "sku_id": row.row.item.external_sku_id,
            "merchant": row.row.item.merchant.name,
            "region_scope": candidate.region.scope.value,
            "region_code": candidate.region.code,
            "current_price": str(candidate.current_price)
            if candidate.current_price is not None
            else None,
            "original_price": str(candidate.original_price)
            if candidate.original_price is not None
            else None,
            "availability": candidate.availability.value,
            "quality_status": candidate.quality_status.value,
            "rejection_code": candidate.rejection_code,
        }
        for row in prepared.rows
        for candidate in row.evaluated
    ]


def _catalog_smoke_result(
    result: FetchResult,
    prepared: PreparedCatalogRows,
) -> dict[str, object]:
    prices = _catalog_smoke_prices(prepared)
    return {
        "status": prepared.status.value,
        "error_code": prepared.error_code,
        "status_code": result.status_code,
        "content_type": result.content_type,
        "source_hash": result.source_hash,
        "parsed_count": len(prepared.rows),
        "price_count": len(prices),
        "accepted_count": prepared.accepted_count,
        "review_count": prepared.review_count,
        "rejected_count": prepared.rejected_count,
        "prices": prices[:10],
        "price_samples_truncated": len(prices) > 10,
    }


def _validate_catalog_fetch(result: FetchResult, allowed_domains: tuple[str, ...]) -> None:
    urls = UrlPolicy(allowed_domains)
    urls.validate(result.request_url)
    urls.validate(result.final_url)
    if not 200 <= result.status_code < 300:
        raise ValueError(f"source fetch failed with HTTP {result.status_code}")


def _catalog_smoke_error(error: Exception) -> dict[str, object]:
    return {
        "status": RunStatus.FAILED.value,
        "accepted_count": 0,
        "error_code": getattr(error, "error_code", type(error).__name__),
        "error": str(error),
    }


def _catalog_result_status(samples: list[dict[str, object]]) -> RunStatus:
    if samples and all(sample["status"] == RunStatus.SUCCEEDED for sample in samples):
        return RunStatus.SUCCEEDED
    if any(sample.get("accepted_count", 0) != 0 for sample in samples):
        return RunStatus.PARTIAL
    return RunStatus.FAILED


async def _catalog_smoke_once(
    settings: Settings,
    channel: str,
    commodity: str | None,
    max_products: int = 1,
) -> None:
    connector = _catalog_connector(channel)
    requests = _catalog_requests(connector.channel_code, commodity)
    rule_for_category = _catalog_seed_rule_resolver()
    http = HttpFetcher(settings)
    try:
        context = AdapterContext(
            http=http,
            browser=BrowserFetcher(settings),
            allowed_domains=list(connector.allowed_domains),
        )
        payloads: list[dict[str, object]] = []
        discovered_count = 0
        for request in requests:
            if isinstance(connector, CatalogDatasetConnector):
                sample: dict[str, object] = {"source_item_codes": request.source_item_codes}
                try:
                    dataset = await connector.discover_dataset(context, request)
                    UrlPolicy(connector.allowed_domains).validate(dataset.url)
                    result = await connector.fetch_dataset(context, dataset)
                    _validate_catalog_fetch(result, connector.allowed_domains)
                    parsed = connector.parse_dataset(dataset, result)
                    prepared = prepare_catalog_rows(
                        parsed.rows,
                        request=request,
                        allowed_domains=connector.allowed_domains,
                        rule_for_category=rule_for_category,
                    )
                    sample.update(
                        dataset_key=dataset.dataset_key,
                        source_page_url=dataset.source_page_url,
                        source_observed_at=dataset.source_observed_at.isoformat(
                            timespec="milliseconds"
                        ),
                        **_catalog_smoke_result(result, prepared),
                    )
                except Exception as error:
                    sample.update(_catalog_smoke_error(error))
                payloads.append(sample)
                continue
            try:
                discovered = await connector.discover_products(context, request)
                discovered_count += len(discovered)
                if not discovered:
                    raise ValueError("source discovery returned no products")
                product_ids = [item.external_product_id for item in discovered]
                if len(set(product_ids)) != len(product_ids):
                    raise ValueError("source discovery returned duplicate product identities")
            except Exception as error:
                payloads.append(_catalog_smoke_error(error))
                continue
            for item in discovered[:max_products]:
                sample = {"product_id": item.external_product_id}
                try:
                    UrlPolicy(connector.allowed_domains).validate(item.url)
                    result = await connector.fetch_product(context, item)
                    _validate_catalog_fetch(result, connector.allowed_domains)
                    product = connector.parse_product(item, result)
                    prepared = prepare_catalog_product(
                        product,
                        discovered=item,
                        expected_brand_code=connector.brand_code,
                        request=request,
                        allowed_domains=connector.allowed_domains,
                        rule_for_category=rule_for_category,
                    )
                    sample.update(
                        name=product.name,
                        category=product.category_code,
                        **_catalog_smoke_result(result, prepared),
                    )
                except Exception as error:
                    sample.update(_catalog_smoke_error(error))
                payloads.append(sample)
        status = _catalog_result_status(payloads)
        payload: dict[str, object]
        if isinstance(connector, CatalogConnector):
            payload = {
                "discovered_count": discovered_count,
                "tested_count": sum("product_id" in sample for sample in payloads),
                "samples": payloads,
            }
        elif len(payloads) == 1:
            payload = dict(payloads[0])
        else:
            payload = {"dataset_count": len(payloads), "datasets": payloads}
        payload.update(channel=connector.channel_code, status=status.value)
        typer.echo(json.dumps(payload, ensure_ascii=False, sort_keys=True))
        if status is not RunStatus.SUCCEEDED:
            raise typer.Exit(code=1)
    finally:
        await http.aclose()


async def _catalog_crawl_once(
    settings: Settings,
    channel: str,
    commodity: str | None,
) -> None:
    connector = _catalog_connector(channel)
    requests = _catalog_requests(connector.channel_code, commodity)
    runtime = build_catalog_runtime(settings)
    try:
        outcomes = [
            await runtime.pipeline.run_dataset(connector, request)
            if isinstance(connector, CatalogDatasetConnector)
            else await runtime.pipeline.run(connector, request)
            for request in requests
        ]
        payload: object
        if len(outcomes) == 1:
            payload = asdict(outcomes[0])
        else:
            payload = {
                "channel": connector.channel_code,
                "dataset_count": len(outcomes),
                "status": _catalog_result_status([asdict(outcome) for outcome in outcomes]).value,
                "outcomes": [asdict(outcome) for outcome in outcomes],
            }
        typer.echo(json.dumps(payload, ensure_ascii=False, sort_keys=True))
        if any(outcome.status is not RunStatus.SUCCEEDED for outcome in outcomes):
            raise typer.Exit(code=1)
    except CatalogConfigurationError as error:
        typer.echo(f"catalog configuration error: {error}", err=True)
        raise typer.Exit(code=2) from error
    finally:
        await runtime.aclose()


def _require_legacy_brand(brand: str) -> None:
    for connector in build_catalog_registry():
        if (
            isinstance(connector, CatalogConnector)
            and connector.brand_code == brand.strip().upper()
        ):
            raise typer.BadParameter(
                f"{connector.brand_code} now uses V2; use catalog crawl/smoke "
                f"--channel {connector.channel_code}"
            )
    raise typer.BadParameter("V1 collection is retired; use catalog sources to select a channel")


def _require_live_crawl(enabled: bool) -> None:
    if not enabled:
        typer.echo(
            "live crawl is disabled; explicitly set LIVE_CRAWL_ENABLED=true",
            err=True,
        )
        raise typer.Exit(code=2)


if __name__ == "__main__":
    app()
