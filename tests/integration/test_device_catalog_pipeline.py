"""The former four-brand V1 fixtures now exercise only native catalog collection."""

from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path
from threading import Barrier, Lock, local
from typing import Any
from urllib.parse import urlsplit

import pytest
from schema_support import reflect_legacy_tables
from sqlalchemy import Engine, event, func, select
from sqlalchemy.orm import Session, sessionmaker

from device_price_service.crawlers.catalog import CatalogConnector
from device_price_service.crawlers.huawei import HuaweiCatalogConnector
from device_price_service.crawlers.oppo import OppoCatalogConnector
from device_price_service.crawlers.vivo import VivoCatalogConnector
from device_price_service.crawlers.xiaomi import XiaomiCatalogConnector
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
    SourceListing,
)
from device_price_service.db.device_seed import seed_device_catalog
from device_price_service.domain.catalog_crawl import (
    CatalogCollectionRequest,
    DiscoveredCatalogProduct,
)
from device_price_service.domain.catalog_enums import RegionScope, RunStatus
from device_price_service.domain.crawl import BrowserSnapshotPlan, FetchResult
from device_price_service.domain.enums import FetchMethod
from device_price_service.normalization.devices import DeviceCategoryRule
from device_price_service.runtime import build_catalog_rule_registry
from device_price_service.services.artifact_store import RawArtifactStore
from device_price_service.services.catalog_crawl_pipeline import CatalogCrawlPipeline
from device_price_service.services.catalog_preparation import prepare_catalog_product

pytestmark = pytest.mark.integration
FIXTURES = Path(__file__).parents[1] / "fixtures"
OBSERVED_AT = datetime(2026, 9, 11, 8)


class DeviceFixtureFetcher:
    def __init__(self, brand: str, *, conditional: bool = False) -> None:
        self.brand = brand
        self.discovery_url = {
            "huawei": "https://www.vmall.com/",
            "xiaomi": "https://www.mi.com/shop",
            "oppo": "https://www.opposhop.cn/",
            "vivo": "https://shop.vivo.com.cn/product/10001186",
        }[brand]
        self.product_url = {
            "huawei": "https://www.vmall.com/product/42002.html",
            "xiaomi": "https://www.mi.com/shop/buy/detail?product_id=91002",
            "oppo": "https://www.opposhop.cn/cn/web/products/43001.html",
            "vivo": "https://shop.vivo.com.cn/product/44001",
        }[brand]
        self.product_fixture = (
            "product_conditional_price.json" if conditional else "product_snapshots.json"
        )
        self.fetched_at = OBSERVED_AT
        self.discovery_calls = 0
        self.product_calls = 0

    async def fetch(self, url: str, *, allowed_domains: list[str]) -> FetchResult:
        assert url == self.discovery_url
        assert urlsplit(url).hostname in allowed_domains
        self.discovery_calls += 1
        return self._result(url, "discovery_one.html", FetchMethod.HTTP, "text/html")

    async def fetch_snapshots(
        self,
        url: str,
        *,
        allowed_domains: list[str],
        plan: BrowserSnapshotPlan,
    ) -> FetchResult:
        assert url == self.product_url
        assert urlsplit(url).hostname in allowed_domains
        assert {dimension.name for dimension in plan.dimensions} == {"version", "color"}
        self.product_calls += 1
        return self._result(url, self.product_fixture, FetchMethod.BROWSER, "application/json")

    def _result(
        self, url: str, filename: str, method: FetchMethod, content_type: str
    ) -> FetchResult:
        return FetchResult(
            request_url=url,
            final_url=url,
            status_code=200,
            headers={"content-type": content_type},
            body=(FIXTURES / self.brand / filename).read_bytes(),
            fetched_at=self.fetched_at,
            duration_ms=5,
            fetch_method=method,
        )


def _connector(fetcher: DeviceFixtureFetcher) -> CatalogConnector:
    constructors = {
        "huawei": HuaweiCatalogConnector,
        "xiaomi": XiaomiCatalogConnector,
        "oppo": OppoCatalogConnector,
        "vivo": VivoCatalogConnector,
    }
    return constructors[fetcher.brand](discovery_url=fetcher.discovery_url)


def _request(brand: str) -> CatalogCollectionRequest:
    return CatalogCollectionRequest(
        region_scope=RegionScope.NATIONAL,
        region_code="CN",
        category_codes=("TABLET" if brand in {"huawei", "xiaomi"} else "PHONE",),
    )


def _pipeline(
    engine: Engine,
    factory: sessionmaker[Session],
    fetcher: DeviceFixtureFetcher,
    store: RawArtifactStore,
) -> CatalogCrawlPipeline:
    with factory.begin() as session:
        seed_device_catalog(session, enable=True)
    return CatalogCrawlPipeline(
        engine=engine,
        session_factory=factory,
        http_fetcher=fetcher,
        browser_fetcher=fetcher,
        artifact_store=store,
        category_rules=build_catalog_rule_registry(),
    )


@pytest.mark.parametrize("synchronize_at", ["start_run", "first_match"])
def test_different_device_sources_can_start_and_match_concurrently(
    mysql_engine: Engine,
    migrated_session_factory: sessionmaker[Session],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    synchronize_at: str,
) -> None:
    fetchers = [DeviceFixtureFetcher(brand) for brand in ("huawei", "xiaomi")]
    pipelines = [
        _pipeline(
            mysql_engine,
            migrated_session_factory,
            fetcher,
            RawArtifactStore(tmp_path / fetcher.brand),
        )
        for fetcher in fetchers
    ]
    rendezvous = Barrier(2, timeout=10)
    thread_state = local()
    match_isolation_levels: list[str] = []
    device_connections: set[int] = set()
    returned_isolation_levels: list[str] = []
    start_lock = Lock()
    original_start = CatalogCrawlPipeline._start_run

    def serialized_start(self: CatalogCrawlPipeline, *args: Any, **kwargs: Any) -> int:
        # Isolate the match regression from the independent startup gap-lock regression.
        with start_lock:
            return original_start(self, *args, **kwargs)

    if synchronize_at == "first_match":
        monkeypatch.setattr(CatalogCrawlPipeline, "_start_run", serialized_start)

    def synchronize_reads(
        connection: Any,
        _cursor: Any,
        statement: str,
        _parameters: Any,
        _context: Any,
        _executemany: bool,
    ) -> None:
        is_start = "FROM v2_crawl_run" in statement and "v2_crawl_run.started_at <" in statement
        is_match = (
            "FROM v2_listing_match" in statement
            and "v2_listing_match.effective_to IS NULL" in statement
        )
        if is_match:
            device_connections.add(id(connection.connection.driver_connection))
        selected = is_start if synchronize_at == "start_run" else is_match
        if selected and not getattr(thread_state, "synchronized", False):
            thread_state.synchronized = True
            if is_match:
                match_isolation_levels.append(connection.get_isolation_level())
            # Both empty-range reads must finish before either transaction inserts.
            # Under REPEATABLE READ + FOR UPDATE, their gap locks deadlock on insert.
            rendezvous.wait()

    def check_returned_isolation(dbapi_connection: Any, _record: Any) -> None:
        if id(dbapi_connection) in device_connections:
            returned_isolation_levels.append(
                mysql_engine.dialect.get_isolation_level(dbapi_connection)
            )

    def collect(index: int):
        fetcher = fetchers[index]
        return asyncio.run(pipelines[index].run(_connector(fetcher), _request(fetcher.brand)))

    event.listen(mysql_engine, "after_cursor_execute", synchronize_reads)
    event.listen(mysql_engine.pool, "checkin", check_returned_isolation)
    try:
        with ThreadPoolExecutor(max_workers=2) as executor:
            outcomes = list(executor.map(collect, range(2)))
    finally:
        event.remove(mysql_engine, "after_cursor_execute", synchronize_reads)
        event.remove(mysql_engine.pool, "checkin", check_returned_isolation)

    assert all(outcome.status is RunStatus.SUCCEEDED for outcome in outcomes)
    assert [outcome.accepted_count for outcome in outcomes] == [2, 3]
    assert all(outcome.failed_count == 0 for outcome in outcomes)
    if synchronize_at == "first_match":
        assert match_isolation_levels == ["READ COMMITTED", "READ COMMITTED"]
        assert len(device_connections) == 2
    assert device_connections
    assert len(returned_isolation_levels) >= 2
    assert set(returned_isolation_levels) == {"REPEATABLE READ"}
    with migrated_session_factory() as session:
        assert set(session.scalars(select(CatalogCrawlRun.status))) == {RunStatus.SUCCEEDED.value}
        assert session.scalar(select(func.count()).select_from(CatalogCrawlRun)) == 2
        assert session.scalar(select(func.count()).select_from(CatalogItem)) == 2
        for model in (ListingMatch, CatalogPriceCurrent, CatalogPriceObservationRecord):
            assert session.scalar(select(func.count()).select_from(model)) == 5
    # A unit's isolation override must not leak into later pool checkouts or server defaults.
    with mysql_engine.connect() as connection:
        assert connection.get_isolation_level() == "REPEATABLE READ"


def test_device_transaction_isolation_resets_after_product_rollback(
    mysql_engine: Engine,
    migrated_session_factory: sessionmaker[Session],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fetcher = DeviceFixtureFetcher("huawei")
    pipeline = _pipeline(
        mysql_engine, migrated_session_factory, fetcher, RawArtifactStore(tmp_path / "raw")
    )
    device_connections: set[int] = set()
    returned_isolation_levels: list[str] = []

    def fail_product(session: Session, *_args: Any, **_kwargs: Any) -> None:
        connection = session.connection()
        assert connection.get_isolation_level() == "READ COMMITTED"
        device_connections.add(id(connection.connection.driver_connection))
        raise RuntimeError("force a unit rollback after evidence insertion")

    def check_returned_isolation(dbapi_connection: Any, _record: Any) -> None:
        if id(dbapi_connection) in device_connections:
            returned_isolation_levels.append(
                mysql_engine.dialect.get_isolation_level(dbapi_connection)
            )

    monkeypatch.setattr(pipeline, "_persist_row", fail_product)
    event.listen(mysql_engine.pool, "checkin", check_returned_isolation)
    try:
        outcome = asyncio.run(pipeline.run(_connector(fetcher), _request(fetcher.brand)))
    finally:
        event.remove(mysql_engine.pool, "checkin", check_returned_isolation)

    assert outcome.status is RunStatus.FAILED and outcome.failed_count == 1
    assert device_connections and returned_isolation_levels
    assert set(returned_isolation_levels) == {"REPEATABLE READ"}
    with migrated_session_factory() as session:
        assert session.scalar(select(func.count()).select_from(CatalogItem)) == 0
        assert session.scalar(select(func.count()).select_from(ListingMatch)) == 0
        assert session.scalar(select(func.count()).select_from(CatalogPriceCurrent)) == 0
        record = session.scalars(select(CatalogCrawlRecord)).one()
        assert record.error_code == "RUNTIMEERROR"


@pytest.mark.parametrize("brand", ["huawei", "xiaomi", "oppo", "vivo"])
def test_native_brand_fixture_is_idempotent_replayable_and_preserves_v2_catalog_chain(
    mysql_engine: Engine,
    migrated_session_factory: sessionmaker[Session],
    tmp_path: Path,
    brand: str,
) -> None:
    fetcher = DeviceFixtureFetcher(brand)
    connector = _connector(fetcher)
    store = RawArtifactStore(tmp_path / "raw")
    pipeline = _pipeline(mysql_engine, migrated_session_factory, fetcher, store)
    request = _request(brand)
    sku_count = 3 if brand == "xiaomi" else 2

    for _ in range(2):
        outcome = asyncio.run(pipeline.run(connector, request))
        assert outcome.status is RunStatus.SUCCEEDED
        assert outcome.discovered_count == outcome.fetched_count == 1
        assert outcome.accepted_count == sku_count
        assert outcome.failed_count == 0

    assert fetcher.discovery_calls == fetcher.product_calls == 2
    with migrated_session_factory() as session:
        assert session.scalar(select(func.count()).select_from(CatalogItem)) == 1
        for model in (
            ItemVariant,
            SourceListing,
            ListingRevision,
            ListingMatch,
            CatalogPriceCurrent,
            CatalogPriceObservationRecord,
        ):
            assert session.scalar(select(func.count()).select_from(model)) == sku_count
        for table in reflect_legacy_tables(mysql_engine).tables.values():
            assert session.scalar(select(func.count()).select_from(table)) == 0
        assert (
            session.scalar(
                select(CatalogBrand.code).join(CatalogItem, CatalogItem.brand_id == CatalogBrand.id)
            )
            == brand.upper()
        )
        listings = session.scalars(select(SourceListing)).all()
        variants = session.scalars(select(ItemVariant)).all()
        prices = session.scalars(select(CatalogPriceObservationRecord)).all()
        assert all(price.observed_at == OBSERVED_AT for price in prices)
        assert all(price.quality_status == "ACCEPTED" for price in prices)
        assert all(price.price_type == "DIRECT_UNCONDITIONAL" for price in prices)
        assert all(price.region_code == "CN" and price.currency == "CNY" for price in prices)
        assert all(
            variant.base_unit == "PIECE" and variant.quantity_value == 1 for variant in variants
        )
        assert all(
            variant.attributes["color"] and variant.attributes["capacity"] for variant in variants
        )
        assert all(variant.manufacturer_part_number is None for variant in variants)
        assert {price.current_price for price in prices} == {
            "huawei": {Decimal("2999"), Decimal("3999")},
            "xiaomi": {Decimal("2999"), Decimal("3299"), Decimal("3999")},
            "oppo": {Decimal("5999"), Decimal("6499")},
            "vivo": {Decimal("4799"), Decimal("5299")},
        }[brand]
        assert {price.original_price for price in prices} == {
            None,
            {
                "huawei": Decimal("4299"),
                "xiaomi": Decimal("3499"),
                "oppo": Decimal("6999"),
                "vivo": Decimal("5799"),
            }[brand],
        }
        if brand == "xiaomi":
            assert all(listing.external_sku_id is None for listing in listings)
            assert all('"spec"' in listing.listing_key for listing in listings)
        else:
            assert all(listing.external_sku_id for listing in listings)
        if brand == "vivo":
            assert {price.availability for price in prices} == {"ON_SALE", "OFF_SHELF"}
        if brand in {"huawei", "xiaomi"}:
            assert {price.availability for price in prices} == {"ON_SALE", "OUT_OF_STOCK"}

        records = session.scalars(select(CatalogCrawlRecord).order_by(CatalogCrawlRecord.id)).all()
        assert len(records) == 2
        assert len({price.crawl_record_id for price in prices}) == 1
        for record in records:
            assert record.entity_type == "PRODUCT" and record.source_listing_id is None
            assert record.raw_path and record.raw_hash
            context = next(
                entry for entry in record.artifact_manifest if entry["role"] == "discovery_context"
            )
            product = DiscoveredCatalogProduct.model_validate(context["discovery"])
            replay_request = CatalogCollectionRequest.model_validate(context["request"])
            raw = store.load(record.raw_path, expected_hash=record.raw_hash)
            result = FetchResult(
                request_url=record.request_url,
                final_url=record.final_url,
                status_code=record.http_status,
                headers={"content-type": record.content_type},
                body=raw,
                fetched_at=record.fetched_at,
                duration_ms=record.duration_ms,
                fetch_method=FetchMethod.REPLAY,
            )
            prepared = prepare_catalog_product(
                connector.parse_product(product, result),
                discovered=product,
                expected_brand_code=connector.brand_code,
                request=replay_request,
                allowed_domains=connector.allowed_domains,
                rule_for_category=lambda code: DeviceCategoryRule(),
            )
            assert prepared.complete and prepared.accepted_count == sku_count

    # A later real collection is a new point, even when the quoted amounts are unchanged.
    fetcher.fetched_at += timedelta(hours=1)
    outcome = asyncio.run(pipeline.run(connector, request))
    assert outcome.status is RunStatus.SUCCEEDED
    with migrated_session_factory() as session:
        assert (
            session.scalar(select(func.count()).select_from(CatalogPriceObservationRecord))
            == 2 * sku_count
        )
        assert session.scalar(select(func.count()).select_from(ListingRevision)) == sku_count
        assert session.scalar(select(func.count()).select_from(ListingMatch)) == sku_count
        assert set(session.scalars(select(CatalogPriceCurrent.observed_at))) == {fetcher.fetched_at}


@pytest.mark.parametrize("brand", ["huawei", "xiaomi", "oppo", "vivo"])
def test_native_brand_conditional_price_cannot_write_trusted_catalog_prices(
    mysql_engine: Engine,
    migrated_session_factory: sessionmaker[Session],
    tmp_path: Path,
    brand: str,
) -> None:
    fetcher = DeviceFixtureFetcher(brand, conditional=True)
    pipeline = _pipeline(
        mysql_engine, migrated_session_factory, fetcher, RawArtifactStore(tmp_path / "raw")
    )
    outcome = asyncio.run(pipeline.run(_connector(fetcher), _request(brand)))
    assert outcome.status is RunStatus.FAILED
    assert outcome.accepted_count == 0
    with migrated_session_factory() as session:
        assert session.scalar(select(func.count()).select_from(CatalogPriceCurrent)) == 0
        assert session.scalar(select(func.count()).select_from(CatalogItem)) == 0
        assert (
            session.scalar(
                select(func.count())
                .select_from(CatalogPriceObservationRecord)
                .where(CatalogPriceObservationRecord.quality_status == "ACCEPTED")
            )
            == 0
        )
        records = session.scalars(select(CatalogCrawlRecord)).all()
        assert records and all(record.raw_path and record.raw_hash for record in records)
