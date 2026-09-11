from __future__ import annotations

import asyncio
import base64
from dataclasses import replace
from datetime import datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import Engine, event, inspect, select
from sqlalchemy.orm import Session, sessionmaker

from device_price_service.crawlers.catalog import CatalogConnector, CatalogDatasetConnector
from device_price_service.crawlers.catalog_builtin import build_catalog_registry
from device_price_service.db.base import Base
from device_price_service.db.catalog_models import CatalogCrawlRecord, SourceChannel, SourceListing
from device_price_service.db.catalog_seed import (
    seed_fresh_categories,
    seed_mofcom_fresh_source,
    seed_shanghai_fresh_source,
)
from device_price_service.db.device_seed import seed_device_catalog
from device_price_service.domain.catalog_crawl import (
    CatalogCollectionRequest,
    DiscoveredCatalogDataset,
    DiscoveredCatalogProduct,
)
from device_price_service.domain.catalog_enums import RegionScope
from device_price_service.domain.crawl import FetchResult
from device_price_service.domain.enums import FetchMethod
from device_price_service.runtime import build_catalog_rule_registry
from device_price_service.services.artifact_store import RawArtifactStore
from device_price_service.services.catalog_crawl_pipeline import CatalogCrawlPipeline
from device_price_service.services.replay_service import ReplayError, ReplayService

pytestmark = pytest.mark.integration
FIXTURES = Path(__file__).parents[1] / "fixtures"
WHEN = datetime(2026, 9, 11, 8)
DEVICE_CASES = [
    (
        "APPLE_CN_WEB",
        "iphone-fixture-pro",
        "PHONE",
        "https://www.apple.com.cn/shop/buy-iphone/iphone-fixture-pro",
        "apple/product_iphone.html",
        3,
    ),
    (
        "HUAWEI_CN_WEB",
        "42002",
        "TABLET",
        "https://www.vmall.com/product/42002.html",
        "huawei/product_snapshots.json",
        2,
    ),
    (
        "XIAOMI_CN_WEB",
        "91002",
        "TABLET",
        "https://www.mi.com/shop/buy/detail?product_id=91002",
        "xiaomi/product_snapshots.json",
        3,
    ),
    (
        "OPPO_CN_WEB",
        "43001",
        "PHONE",
        "https://www.opposhop.cn/cn/web/products/43001.html",
        "oppo/product_snapshots.json",
        2,
    ),
    (
        "VIVO_CN_WEB",
        "44001",
        "PHONE",
        "https://shop.vivo.com.cn/product/44001",
        "vivo/product_snapshots.json",
        2,
    ),
]


class EvidenceFetcher:
    def __init__(self, result: FetchResult):
        self.result = result
        self.calls = 0

    async def fetch(self, url, **kwargs):
        self.calls += 1
        assert url == self.result.request_url
        return self.result

    async def fetch_snapshots(self, url, **kwargs):
        return await self.fetch(url, **kwargs)


def _snapshot(factory: sessionmaker[Session]):
    with factory() as session:
        return {
            name: list(session.execute(select(table).order_by(*table.primary_key.columns)))
            for name, table in Base.metadata.tables.items()
        }


def _setup(engine: Engine, factory: sessionmaker[Session], tmp_path: Path, result: FetchResult):
    with factory.begin() as session:
        seed_device_catalog(session, enable=True)
        seed_fresh_categories(session)
        seed_mofcom_fresh_source(session, enable=True)
        seed_shanghai_fresh_source(session, enable=True)
    fetcher = EvidenceFetcher(result)
    store = RawArtifactStore(tmp_path / "raw")
    registry = build_catalog_registry()
    rules = build_catalog_rule_registry()
    pipeline = CatalogCrawlPipeline(
        engine=engine,
        session_factory=factory,
        http_fetcher=fetcher,
        browser_fetcher=fetcher,
        artifact_store=store,
        category_rules=rules,
    )
    service = ReplayService(
        session_factory=factory,
        artifact_store=store,
        registry=registry,
        rule_registry=rules,
    )
    return registry, fetcher, pipeline, service


def _readonly_replay(engine, factory, service, record_id):
    before = _snapshot(factory)
    statements = []

    def check_statement(connection, cursor, statement, parameters, context, executemany):
        statements.append(statement)
        assert statement.lstrip().upper().startswith("SELECT"), statement

    event.listen(engine, "before_cursor_execute", check_statement)
    try:
        outcome = service.replay(record_id).to_dict()
    finally:
        event.remove(engine, "before_cursor_execute", check_statement)
    assert statements and before == _snapshot(factory)
    return outcome


@pytest.mark.parametrize("channel,product_id,category,url,filename,rows", DEVICE_CASES)
def test_v2_only_all_brand_replay_preserves_complete_skus_and_original_time(
    mysql_engine,
    session_factory,
    tmp_path,
    monkeypatch,
    channel,
    product_id,
    category,
    url,
    filename,
    rows,
):
    assert len(inspect(mysql_engine).get_table_names()) in {13, 14}  # optional Alembic marker
    result = FetchResult(
        url,
        url,
        200,
        {"content-type": "text/html" if filename.endswith("html") else "application/json"},
        (FIXTURES / filename).read_bytes(),
        WHEN,
        1,
        FetchMethod.REPLAY,
    )
    registry, fetcher, pipeline, service = _setup(mysql_engine, session_factory, tmp_path, result)
    connector = registry.get(channel)
    assert isinstance(connector, CatalogConnector)
    product = DiscoveredCatalogProduct(
        external_product_id=product_id,
        category_code=category,
        url=url,
    )

    async def discover(context, request):
        return [product]

    monkeypatch.setattr(connector, "discover_products", discover)
    request = CatalogCollectionRequest(
        region_scope=RegionScope.NATIONAL,
        region_code="CN",
        category_codes=(category,),
    )
    run = asyncio.run(pipeline.run(connector, request))
    assert run.status.value == "SUCCEEDED"
    with session_factory.begin() as session:
        record = session.scalar(select(CatalogCrawlRecord))
        record_id = record.id
        # A later URL/category toggle must not corrupt the immutable original context.
        for listing in session.scalars(select(SourceListing)):
            listing.canonical_url = "https://later.example/no-longer-the-evidence"
        session.scalar(select(SourceChannel).where(SourceChannel.code == channel)).enabled = False
    count = fetcher.calls
    outcome = _readonly_replay(mysql_engine, session_factory, service, record_id)
    assert outcome["status"] == "SUCCEEDED" and fetcher.calls == count
    assert len(outcome["static_validation"]["rows"]) == rows
    assert len(outcome["historical_validation"]["facts"]) == rows
    assert outcome["fetched_at"] == WHEN.isoformat(timespec="milliseconds")
    assert all(
        price["observed_at"] == outcome["fetched_at"]
        for row in outcome["static_validation"]["rows"]
        for price in row["prices"]
    )


@pytest.mark.parametrize("source", ["mofcom", "shanghai"])
@pytest.mark.parametrize("historical_manifest", [False, True])
def test_government_replay_new_and_original_deployed_evidence(
    mysql_engine,
    session_factory,
    tmp_path,
    monkeypatch,
    source,
    historical_manifest,
):
    mofcom = source == "mofcom"
    channel = "MOFCOM_FRESH_WHOLESALE" if mofcom else "SH_FGW_FRESH_RETAIL"
    source_date = "2026-08-23" if mofcom else "2026-08-19"
    key = f"mofcom-bj:170130:{source_date}" if mofcom else f"shanghai-fresh-retail:{source_date}"
    url = (
        "https://cif.mofcom.gov.cn/cif/seach.fhtml?commdityid=170130"
        if mofcom
        else "https://fgw.sh.gov.cn/cmsres/sanitized/shanghai-fresh-20260819.xls"
    )
    body = (
        (FIXTURES / "catalog_fresh/mofcom_cucumber_20260823.html").read_bytes()
        if mofcom
        else (
            base64.b64decode(
                (FIXTURES / "catalog_fresh/shanghai_daily_20260819.xls.b64").read_bytes()
            )
        )
    )
    result = FetchResult(
        url,
        url,
        200,
        {"content-type": "text/html" if mofcom else "application/vnd.ms-excel"},
        body,
        WHEN,
        1,
        FetchMethod.REPLAY,
    )
    registry, fetcher, pipeline, service = _setup(mysql_engine, session_factory, tmp_path, result)
    connector = registry.get(channel)
    assert isinstance(connector, CatalogDatasetConnector)
    dataset = DiscoveredCatalogDataset(
        dataset_key=key,
        url=url,
        source_page_url=url if mofcom else "https://fgw.sh.gov.cn/fgw_jgjgdt/20260819/fixture.html",
        source_observed_at=datetime.fromisoformat(source_date) - timedelta(hours=8),
        metadata={
            "source_date": source_date,
            "time_precision": "DAY",
            **({"commodity_code": "CUCUMBER", "commodity_id": "170130"} if mofcom else {}),
        },
    )

    async def discover(context, request):
        return dataset

    monkeypatch.setattr(connector, "discover_dataset", discover)
    request = CatalogCollectionRequest(
        region_scope=RegionScope.MULTI if mofcom else RegionScope.CITY,
        region_code="*" if mofcom else "310100",
        category_codes=("FRESH_MONITORED_COMMODITY",),
        source_item_codes=("CUCUMBER",) if mofcom else (),
    )
    run = asyncio.run(pipeline.run_dataset(connector, request))
    assert run.status.value == "SUCCEEDED"
    with session_factory.begin() as session:
        record = session.scalar(select(CatalogCrawlRecord))
        record_id = record.id
        if historical_manifest:
            record.artifact_manifest = [
                part
                for part in record.artifact_manifest
                if part["role"] in {"source_page", "index"}
            ]
    outcome = _readonly_replay(mysql_engine, session_factory, service, record_id)
    assert outcome["status"] == "SUCCEEDED" and fetcher.calls == 1
    assert outcome["context_recovered"] == historical_manifest
    assert outcome["static_validation"]["accepted_count"] == (5 if mofcom else 8)
    assert all(
        price["observed_at"] == dataset.source_observed_at.isoformat(timespec="milliseconds")
        for row in outcome["static_validation"]["rows"]
        for price in row["prices"]
    )


def test_replay_missing_record_has_no_write(mysql_engine, session_factory, tmp_path):
    service = ReplayService(
        session_factory=session_factory,
        artifact_store=RawArtifactStore(tmp_path),
        registry=build_catalog_registry(),
        rule_registry=build_catalog_rule_registry(),
    )
    before = _snapshot(session_factory)
    with pytest.raises(ReplayError, match="does not exist"):
        service.replay(9999)
    assert before == _snapshot(session_factory)


def test_confirmed_absence_replays_original_status_without_parsing_or_copying_prices(
    mysql_engine,
    migrated_session_factory,
    tmp_path,
    monkeypatch,
):
    channel, product_id, category, url, filename, _ = DEVICE_CASES[0]
    result = FetchResult(
        url,
        url,
        200,
        {"content-type": "text/html"},
        (FIXTURES / filename).read_bytes(),
        WHEN,
        1,
        FetchMethod.HTTP,
    )
    registry, fetcher, pipeline, service = _setup(
        mysql_engine,
        migrated_session_factory,
        tmp_path,
        result,
    )
    connector = registry.get(channel)
    assert isinstance(connector, CatalogConnector)
    product = DiscoveredCatalogProduct(
        external_product_id=product_id, category_code=category, url=url
    )
    second = product.model_copy(
        update={
            "external_product_id": "iphone-fixture-second",
            "url": url.replace(product_id, "iphone-fixture-second"),
        }
    )
    products = [product, second]

    async def discover(context, request):
        return products

    async def fetch_product(context, item):
        if len(products) == 1 and item == product:
            return replace(
                result, status_code=404, body=b"not found", fetched_at=WHEN + timedelta(hours=2)
            )
        return replace(
            result,
            request_url=item.url,
            final_url=item.url,
            body=result.body.replace(product_id.encode(), item.external_product_id.encode()),
            fetched_at=WHEN + timedelta(hours=int(len(products) == 1)),
        )

    monkeypatch.setattr(connector, "discover_products", discover)
    monkeypatch.setattr(connector, "fetch_product", fetch_product)
    pipeline.missing_confirmation_runs = 1
    request = CatalogCollectionRequest(
        region_scope=RegionScope.NATIONAL,
        region_code="CN",
        category_codes=connector.default_category_codes,
    )
    assert asyncio.run(pipeline.run(connector, request)).status.value == "SUCCEEDED"
    products[:] = [second]
    assert asyncio.run(pipeline.run(connector, request)).status.value == "SUCCEEDED"
    with migrated_session_factory() as session:
        record_id = session.scalar(
            select(CatalogCrawlRecord.id).where(
                CatalogCrawlRecord.error_code == "OFF_SHELF_CONFIRMED"
            )
        )
    assert record_id is not None

    def must_not_parse(*args, **kwargs):
        raise AssertionError("a confirmed 404 has no product page to parse")

    monkeypatch.setattr(connector, "parse_product", must_not_parse)
    outcome = _readonly_replay(mysql_engine, migrated_session_factory, service, record_id)
    assert outcome["status"] == "SUCCEEDED" and outcome["static_validation"] is None
    assert len(outcome["historical_validation"]["facts"]) == 3
    assert all(
        fact["current_price"] is None
        and fact["original_price"] is None
        and fact["availability"] == "OFF_SHELF"
        for fact in outcome["historical_validation"]["facts"]
    )
