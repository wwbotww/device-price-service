from __future__ import annotations

import asyncio
import base64
import json
from collections.abc import Iterator
from datetime import datetime, timedelta
from decimal import Decimal
from hashlib import sha256
from pathlib import Path

import pytest
from schema_support import LEGACY_TABLE_NAMES, drop_test_tables, reflect_legacy_tables
from sqlalchemy import Engine, func, inspect, select, text
from sqlalchemy.orm import Session, sessionmaker

from device_price_service.cli import V2_TABLES
from device_price_service.crawlers.apple import AppleCatalogConnector
from device_price_service.crawlers.base import AdapterContext
from device_price_service.crawlers.shanghai_fresh import (
    SHANGHAI_FRESH_CATEGORY_CODE,
    SHANGHAI_FRESH_INDEX_URL,
    ShanghaiFreshRetailConnector,
)
from device_price_service.db.base import Base
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
from device_price_service.db.catalog_repositories import CatalogPriceRepository
from device_price_service.db.catalog_seed import seed_fresh_categories, seed_shanghai_fresh_source
from device_price_service.db.device_seed import seed_device_catalog
from device_price_service.db.session import create_session_factory
from device_price_service.domain.catalog_crawl import (
    CatalogCollectionRequest,
    DiscoveredCatalogProduct,
    ParsedCatalogProduct,
)
from device_price_service.domain.catalog_enums import PriceType, RegionScope, RunStatus
from device_price_service.domain.crawl import FetchResult
from device_price_service.domain.enums import FetchMethod
from device_price_service.normalization.devices import DeviceCategoryRule
from device_price_service.normalization.fresh_food import build_fresh_food_rule_registry
from device_price_service.services.artifact_store import RawArtifactStore
from device_price_service.services.catalog_crawl_pipeline import CatalogCrawlPipeline

pytestmark = pytest.mark.integration
FIXTURES = Path(__file__).parents[1] / "fixtures"
OBSERVED_AT = datetime(2026, 9, 11, 8)
PRODUCT = DiscoveredCatalogProduct(
    external_product_id="iphone-fixture-pro",
    category_code="PHONE",
    url="https://www.apple.com.cn/shop/buy-iphone/iphone-fixture-pro",
)
REQUEST = CatalogCollectionRequest(
    region_scope=RegionScope.NATIONAL, region_code="CN", category_codes=("PHONE",)
)


class SequenceAppleFetcher:
    def __init__(self, responses: list[tuple[datetime, bytes]]) -> None:
        self.responses = responses
        self.calls: list[str] = []

    async def fetch(self, url: str, *, allowed_domains: list[str]) -> FetchResult:
        assert allowed_domains == ["www.apple.com.cn"]
        fetched_at, body = self.responses[len(self.calls)]
        self.calls.append(url)
        return FetchResult(
            request_url=url,
            final_url=url,
            status_code=200,
            headers={"content-type": "text/html; charset=utf-8"},
            body=body,
            fetched_at=fetched_at,
            duration_ms=1,
            fetch_method=FetchMethod.REPLAY,
        )


class FixedAppleConnector(AppleCatalogConnector):
    def __init__(self, products: list[DiscoveredCatalogProduct] | None = None) -> None:
        super().__init__()
        self.products = [PRODUCT] if products is None else products

    async def discover_products(
        self, context: AdapterContext, request: CatalogCollectionRequest
    ) -> list[DiscoveredCatalogProduct]:
        return self.products


def _body() -> bytes:
    return (FIXTURES / "apple" / "product_iphone.html").read_bytes()


def _seed(factory: sessionmaker[Session]) -> None:
    with factory.begin() as session:
        seed_device_catalog(session, enable=True)


def _pipeline(
    engine: Engine, factory: sessionmaker[Session], fetcher: SequenceAppleFetcher, raw_path: Path
) -> CatalogCrawlPipeline:
    rules = build_fresh_food_rule_registry()
    rules.register(DeviceCategoryRule())
    return CatalogCrawlPipeline(
        engine=engine,
        session_factory=factory,
        http_fetcher=fetcher,
        browser_fetcher=fetcher,  # type: ignore[arg-type]
        artifact_store=RawArtifactStore(raw_path),
        category_rules=rules,
    )


def _count(session: Session, model: type) -> int:
    return session.scalar(select(func.count()).select_from(model)) or 0


@pytest.fixture()
def v2_only_factory(mysql_engine: Engine) -> Iterator[sessionmaker[Session]]:
    drop_test_tables(mysql_engine)
    tables = [Base.metadata.tables[name] for name in sorted(V2_TABLES)]
    Base.metadata.create_all(mysql_engine, tables=tables)
    try:
        assert LEGACY_TABLE_NAMES.isdisjoint(inspect(mysql_engine).get_table_names())
        yield create_session_factory(mysql_engine)
    finally:
        Base.metadata.drop_all(mysql_engine, tables=tables)


def test_apple_product_multi_sku_has_one_shared_evidence_and_full_v2_catalog_chain(
    mysql_engine: Engine, v2_only_factory: sessionmaker[Session], tmp_path: Path
) -> None:
    _seed(v2_only_factory)
    fetcher = SequenceAppleFetcher([(OBSERVED_AT, _body())])
    raw_path = tmp_path / "raw"
    pipeline = _pipeline(mysql_engine, v2_only_factory, fetcher, raw_path)

    outcome = asyncio.run(pipeline.run(FixedAppleConnector(), REQUEST))

    assert outcome.status is RunStatus.SUCCEEDED
    assert outcome.discovered_count == outcome.fetched_count == 1
    assert outcome.accepted_count == 3
    assert outcome.failed_count == 0
    assert fetcher.calls == [PRODUCT.url]
    assert LEGACY_TABLE_NAMES.isdisjoint(inspect(mysql_engine).get_table_names())
    with v2_only_factory() as session:
        assert _count(session, CatalogItem) == _count(session, Merchant) == 1
        assert _count(session, ItemVariant) == _count(session, ListingRevision) == 3
        assert _count(session, SourceListing) == _count(session, ListingMatch) == 3
        record = session.scalar(select(CatalogCrawlRecord))
        assert record is not None
        assert record.entity_type == "PRODUCT"
        assert record.entity_key == PRODUCT.external_product_id
        assert record.source_listing_id is None
        assert record.raw_path and record.raw_hash
        assert (
            RawArtifactStore(raw_path).load(record.raw_path, expected_hash=record.raw_hash)
            == _body()
        )
        manifest = json.dumps(record.artifact_manifest, ensure_ascii=False)
        for token in ("iphone-fixture-pro", "PHONE", "APPLE", "APPLE_CN_WEB", "CN"):
            assert token in manifest
        rows = session.execute(
            select(
                SourceListing.external_sku_id,
                CatalogPriceObservationRecord,
                ItemVariant,
                CatalogItem,
                CatalogBrand,
            )
            .select_from(SourceListing)
            .join(CatalogPriceCurrent, CatalogPriceCurrent.source_listing_id == SourceListing.id)
            .join(
                CatalogPriceObservationRecord,
                CatalogPriceObservationRecord.id == CatalogPriceCurrent.price_observation_id,
            )
            .join(
                ListingMatch, ListingMatch.listing_revision_id == SourceListing.current_revision_id
            )
            .join(ItemVariant, ItemVariant.id == ListingMatch.item_variant_id)
            .join(CatalogItem, CatalogItem.id == ItemVariant.catalog_item_id)
            .join(CatalogBrand, CatalogBrand.id == CatalogItem.brand_id)
        ).all()
        assert len(rows) == 3
        by_sku = {sku: (price, variant) for sku, price, variant, _, _ in rows}
        for _, price, variant, item, brand in rows:
            assert price.crawl_record_id == record.id
            assert price.observed_at == OBSERVED_AT
            assert price.price_nature == "RETAIL_OFFER"
            assert price.region_code == "CN"
            assert variant.quantity_value == 1 and variant.base_unit == "PIECE"
            assert item.item_type == "MODEL" and brand.code == "APPLE"
        assert by_sku["FX256BLUECH/A"][0].current_price == Decimal("8999")
        assert by_sku["FX256BLUECH/A"][0].original_price is None
        assert by_sku["FX512SILVERCH/A"][0].original_price == Decimal("10999")
        assert by_sku["FX512SILVERCH/A"][0].original_price_type == "EXPLICIT_ORIGINAL"
        assert by_sku["FX1TBLACKCH/A"][0].availability == "OUT_OF_STOCK"
        assert by_sku["FX256BLUECH/A"][1].manufacturer_part_number == "FX256BLUECH/A"


def test_apple_macbook_fixture_preserves_computer_specifications_in_v2_only_catalog(
    mysql_engine: Engine, v2_only_factory: sessionmaker[Session], tmp_path: Path
) -> None:
    _seed(v2_only_factory)
    product = DiscoveredCatalogProduct(
        external_product_id="macbook-fixture",
        category_code="LAPTOP",
        url="https://www.apple.com.cn/shop/buy-mac/macbook-fixture",
    )
    body = (FIXTURES / "apple" / "product_macbook.html").read_bytes()
    fetcher = SequenceAppleFetcher([(OBSERVED_AT, body)])
    pipeline = _pipeline(mysql_engine, v2_only_factory, fetcher, tmp_path / "raw")

    outcome = asyncio.run(
        pipeline.run(
            FixedAppleConnector([product]),
            REQUEST.model_copy(update={"category_codes": ("LAPTOP",)}),
        )
    )

    assert outcome.status is RunStatus.SUCCEEDED
    assert outcome.discovered_count == outcome.fetched_count == outcome.accepted_count == 1
    assert outcome.failed_count == 0
    assert fetcher.calls == [product.url]
    assert LEGACY_TABLE_NAMES.isdisjoint(inspect(mysql_engine).get_table_names())
    with v2_only_factory() as session:
        price, variant, category, brand, sku, evidence = session.execute(
            select(
                CatalogPriceObservationRecord,
                ItemVariant,
                TaxonomyCategory.code,
                CatalogBrand.code,
                SourceListing.external_sku_id,
                CatalogCrawlRecord,
            )
            .select_from(SourceListing)
            .join(CatalogPriceCurrent, CatalogPriceCurrent.source_listing_id == SourceListing.id)
            .join(
                CatalogPriceObservationRecord,
                CatalogPriceObservationRecord.id == CatalogPriceCurrent.price_observation_id,
            )
            .join(
                ListingMatch,
                ListingMatch.listing_revision_id == CatalogPriceCurrent.listing_revision_id,
            )
            .join(ItemVariant, ItemVariant.id == ListingMatch.item_variant_id)
            .join(CatalogItem, CatalogItem.id == ItemVariant.catalog_item_id)
            .join(TaxonomyCategory, TaxonomyCategory.id == CatalogItem.category_id)
            .join(CatalogBrand, CatalogBrand.id == CatalogItem.brand_id)
            .join(
                CatalogCrawlRecord,
                CatalogCrawlRecord.id == CatalogPriceObservationRecord.crawl_record_id,
            )
        ).one()
        assert category == "LAPTOP" and brand == "APPLE"
        assert sku == variant.manufacturer_part_number == "MBFIXTURECH/A"
        assert variant.attributes["memory"] == "16GB"
        assert variant.attributes["capacity"] == "512GB"
        assert variant.attributes["size"] == "14 英寸"
        assert variant.attributes["attributes"]["processor"] == "Fixture Pro 芯片"
        assert variant.attributes["attributes"]["model"] == "MacBook Fixture"
        assert price.current_price == Decimal("12999")
        assert price.original_price is None and price.original_price_type == "NONE"
        assert price.observed_at == OBSERVED_AT and price.region_code == "CN"
        assert evidence.entity_type == "PRODUCT" and evidence.entity_key == "macbook-fixture"
        assert evidence.source_listing_id is None
        for model in (
            CatalogItem,
            ItemVariant,
            ListingRevision,
            ListingMatch,
            SourceListing,
            CatalogPriceCurrent,
            CatalogPriceObservationRecord,
        ):
            assert _count(session, model) == 1


def test_apple_replay_new_time_and_older_evidence_preserve_identity_and_current(
    mysql_engine: Engine, migrated_session_factory: sessionmaker[Session], tmp_path: Path
) -> None:
    factory = migrated_session_factory
    _seed(factory)
    newer = OBSERVED_AT + timedelta(hours=1)
    older = OBSERVED_AT - timedelta(hours=1)
    fetcher = SequenceAppleFetcher(
        [
            (OBSERVED_AT, _body()),
            (OBSERVED_AT, _body()),
            (newer, _body()),
            (older, _body()),
        ]
    )
    pipeline = _pipeline(mysql_engine, factory, fetcher, tmp_path / "raw")
    connector = FixedAppleConnector()
    outcomes = [asyncio.run(pipeline.run(connector, REQUEST)) for _ in range(4)]

    assert all(outcome.status is RunStatus.SUCCEEDED for outcome in outcomes)
    assert [outcome.accepted_count for outcome in outcomes] == [3, 3, 3, 3]
    with factory.begin() as session:
        assert _count(session, CatalogPriceObservationRecord) == 9
        assert _count(session, CatalogCrawlRecord) == 4
        assert _count(session, ItemVariant) == _count(session, ListingRevision) == 3
        assert _count(session, ListingMatch) == 3
        assert {match.effective_from for match in session.scalars(select(ListingMatch))} == {
            OBSERVED_AT
        }
        listings = session.scalars(select(SourceListing)).all()
        before = {
            row.source_listing_id: row.price_observation_id
            for row in session.scalars(select(CatalogPriceCurrent))
        }
        assert {row.last_seen_at for row in listings} == {newer}
        prices = CatalogPriceRepository(session)
        for listing in listings:
            prices.rebuild_current(source_listing_id=listing.id)
        after = {
            row.source_listing_id: row.price_observation_id
            for row in session.scalars(select(CatalogPriceCurrent))
        }
        assert after == before
        for observation_id in after.values():
            assert session.get(CatalogPriceObservationRecord, observation_id).observed_at == newer


def test_apple_specification_changes_create_new_variant_but_title_changes_do_not(
    mysql_engine: Engine, migrated_session_factory: sessionmaker[Session], tmp_path: Path
) -> None:
    factory = migrated_session_factory
    _seed(factory)
    changed_title = _body().replace(b"iPhone Fixture Pro", b"iPhone Fixture Pro Special")
    changed_spec = changed_title.replace(b"256<small>GB", b"384<small>GB")
    fetcher = SequenceAppleFetcher(
        [
            (OBSERVED_AT, _body()),
            (OBSERVED_AT + timedelta(minutes=1), changed_title),
            (OBSERVED_AT + timedelta(minutes=2), changed_spec),
            (OBSERVED_AT, _body()),
        ]
    )
    pipeline = _pipeline(mysql_engine, factory, fetcher, tmp_path / "raw")
    for _ in range(2):
        assert (
            asyncio.run(pipeline.run(FixedAppleConnector(), REQUEST)).status is RunStatus.SUCCEEDED
        )
    with factory() as session:
        assert _count(session, CatalogItem) == 1
        assert _count(session, ListingRevision) == _count(session, ItemVariant) == 3
    third = asyncio.run(pipeline.run(FixedAppleConnector(), REQUEST))
    fourth = asyncio.run(pipeline.run(FixedAppleConnector(), REQUEST))

    assert third.status is fourth.status is RunStatus.SUCCEEDED
    with factory() as session:
        assert _count(session, SourceListing) == 3
        assert _count(session, CatalogItem) == 1
        assert _count(session, ListingRevision) == _count(session, ItemVariant) == 4
        assert _count(session, ListingMatch) == 4
        assert _count(session, CatalogPriceObservationRecord) == 9
        listing = session.scalar(
            select(SourceListing).where(SourceListing.external_sku_id == "FX256BLUECH/A")
        )
        assert listing is not None
        revision = session.get(ListingRevision, listing.current_revision_id)
        assert revision is not None
        assert revision.normalized_attributes["capacity"] == "384GB"
        currents = session.scalars(select(CatalogPriceCurrent)).all()
        assert len(currents) == 3
        assert all(
            session.get(CatalogPriceObservationRecord, row.price_observation_id).observed_at
            == OBSERVED_AT + timedelta(minutes=2)
            for row in currents
        )


def test_apple_one_product_parse_failure_does_not_rollback_another_product(
    mysql_engine: Engine, migrated_session_factory: sessionmaker[Session], tmp_path: Path
) -> None:
    factory = migrated_session_factory
    _seed(factory)
    broken = PRODUCT.model_copy(
        update={
            "external_product_id": "broken",
            "url": "https://www.apple.com.cn/shop/buy-iphone/broken",
        }
    )
    fetcher = SequenceAppleFetcher([(OBSERVED_AT, _body()), (OBSERVED_AT, b"invalid-page")])
    pipeline = _pipeline(mysql_engine, factory, fetcher, tmp_path / "raw")

    outcome = asyncio.run(pipeline.run(FixedAppleConnector([PRODUCT, broken]), REQUEST))

    assert outcome.status is RunStatus.PARTIAL
    assert outcome.discovered_count == outcome.fetched_count == 2
    assert outcome.accepted_count == 3 and outcome.failed_count == 1
    with factory() as session:
        assert _count(session, CatalogPriceObservationRecord) == 3
        assert _count(session, SourceListing) == 3
        records = session.scalars(select(CatalogCrawlRecord).order_by(CatalogCrawlRecord.id)).all()
        assert len(records) == 2
        assert records[1].entity_type == "PRODUCT" and records[1].entity_key == "broken"
        assert records[1].source_listing_id is None
        assert records[1].parse_status == "FAILED" and records[1].raw_hash is not None


def test_apple_write_failure_rolls_back_all_skus_and_keeps_failure_evidence(
    mysql_engine: Engine,
    migrated_session_factory: sessionmaker[Session],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    factory = migrated_session_factory
    _seed(factory)
    pipeline = _pipeline(
        mysql_engine, factory, SequenceAppleFetcher([(OBSERVED_AT, _body())]), tmp_path / "raw"
    )
    original = CatalogPriceRepository.record
    calls = 0

    def fail_second(self, observation):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise RuntimeError("injected second SKU persistence failure")
        return original(self, observation)

    monkeypatch.setattr(CatalogPriceRepository, "record", fail_second)
    outcome = asyncio.run(pipeline.run(FixedAppleConnector(), REQUEST))

    assert calls == 2
    assert outcome.status is RunStatus.FAILED
    assert outcome.accepted_count == 0 and outcome.failed_count == 1
    with factory() as session:
        for model in (
            CatalogItem,
            ItemVariant,
            ListingMatch,
            ListingRevision,
            SourceListing,
            CatalogPriceObservationRecord,
            CatalogPriceCurrent,
        ):
            assert _count(session, model) == 0
        record = session.scalar(select(CatalogCrawlRecord))
        assert record is not None and record.raw_hash and record.raw_path
        assert record.entity_type == "PRODUCT" and record.source_listing_id is None
        assert session.get(CatalogCrawlRun, outcome.crawl_run_id).status == "FAILED"


@pytest.mark.parametrize("body_change", ["amount", "title"])
def test_apple_nonidentical_same_time_evidence_does_not_create_a_government_correction(
    mysql_engine: Engine,
    migrated_session_factory: sessionmaker[Session],
    tmp_path: Path,
    body_change: str,
) -> None:
    factory = migrated_session_factory
    _seed(factory)
    changed = (
        _body().replace(b"RMB 8,999", b"RMB 8,499")
        if body_change == "amount"
        else _body().replace(b"iPhone Fixture Pro", b"iPhone Fixture Pro Updated")
    )
    pipeline = _pipeline(
        mysql_engine,
        factory,
        SequenceAppleFetcher([(OBSERVED_AT, _body()), (OBSERVED_AT, changed)]),
        tmp_path / "raw",
    )
    first = asyncio.run(pipeline.run(FixedAppleConnector(), REQUEST))
    second = asyncio.run(pipeline.run(FixedAppleConnector(), REQUEST))

    assert first.status is RunStatus.SUCCEEDED
    assert second.status is RunStatus.FAILED
    with factory.begin() as session:
        assert _count(session, CatalogPriceObservationRecord) == 3
        assert all(
            row.supersedes_observation_id is None
            for row in session.scalars(select(CatalogPriceObservationRecord))
        )
        before = {
            row.source_listing_id: row.price_observation_id
            for row in session.scalars(select(CatalogPriceCurrent))
        }
        for listing_id in before:
            CatalogPriceRepository(session).rebuild_current(source_listing_id=listing_id)
        assert {
            row.source_listing_id: row.price_observation_id
            for row in session.scalars(select(CatalogPriceCurrent))
        } == before


@pytest.mark.parametrize("empty", [True, False])
def test_empty_or_failed_apple_discovery_finishes_the_batch_as_failed(
    mysql_engine: Engine,
    migrated_session_factory: sessionmaker[Session],
    tmp_path: Path,
    empty: bool,
) -> None:
    class DiscoveryFailure(FixedAppleConnector):
        async def discover_products(self, context, request):
            if empty:
                return []
            raise ValueError("fixture discovery unavailable")

    _seed(migrated_session_factory)
    fetcher = SequenceAppleFetcher([])
    pipeline = _pipeline(mysql_engine, migrated_session_factory, fetcher, tmp_path / "raw")
    outcome = asyncio.run(pipeline.run(DiscoveryFailure(), REQUEST))

    assert outcome.status is RunStatus.FAILED
    assert outcome.accepted_count == outcome.fetched_count == 0
    assert not fetcher.calls
    with migrated_session_factory() as session:
        run = session.get(CatalogCrawlRun, outcome.crawl_run_id)
        assert run is not None and run.status == "FAILED" and run.finished_at is not None


@pytest.mark.parametrize("mode", ["conditional", "review", "extra-conditional"])
def test_apple_batch_status_tracks_trusted_skus_instead_of_only_parse_success(
    mysql_engine: Engine, migrated_session_factory: sessionmaker[Session], tmp_path: Path, mode: str
) -> None:
    class QualityFixtureConnector(FixedAppleConnector):
        def parse_product(self, item, result) -> ParsedCatalogProduct:
            product = super().parse_product(item, result)
            rows = []
            for row in product.rows:
                changes = {}
                if mode == "review":
                    changes["source_attributes"] = {}
                else:
                    conditional = row.parsed.price_candidates[0].model_copy(
                        update={"price_type": PriceType.COUPON}
                    )
                    changes["price_candidates"] = (
                        [*row.parsed.price_candidates, conditional]
                        if mode == "extra-conditional"
                        else [conditional]
                    )
                rows.append(
                    row.model_copy(update={"parsed": row.parsed.model_copy(update=changes)})
                )
            return product.model_copy(update={"rows": rows})

    factory = migrated_session_factory
    _seed(factory)
    pipeline = _pipeline(
        mysql_engine, factory, SequenceAppleFetcher([(OBSERVED_AT, _body())]), tmp_path / "raw"
    )
    outcome = asyncio.run(pipeline.run(QualityFixtureConnector(), REQUEST))

    assert outcome.failed_count == 0
    if mode == "extra-conditional":
        assert outcome.status is RunStatus.SUCCEEDED
        assert outcome.accepted_count == outcome.rejected_count == 3
    else:
        assert outcome.status is RunStatus.FAILED
        assert outcome.accepted_count == 0
        assert (outcome.review_count if mode == "review" else outcome.rejected_count) == 3
    with factory() as session:
        assert _count(session, CatalogPriceCurrent) == (3 if mode == "extra-conditional" else 0)


def test_apple_collection_preserves_existing_v1_and_government_rows(
    mysql_engine: Engine, migrated_session_factory: sessionmaker[Session], tmp_path: Path
) -> None:
    factory = migrated_session_factory
    with factory.begin() as session:
        session.execute(
            text("INSERT INTO brand (code, name_zh, name_en) VALUES ('KEEP', '保留', 'Keep')")
        )
        seed_fresh_categories(session)
        seed_shanghai_fresh_source(session, enable=True)

    class ShanghaiFixtureFetcher:
        async def fetch(self, url, *, allowed_domains):
            assert allowed_domains == ["fgw.sh.gov.cn"]
            fixtures = FIXTURES / "catalog_fresh"
            if url == SHANGHAI_FRESH_INDEX_URL:
                body, content_type = (fixtures / "shanghai_index.html").read_bytes(), "text/html"
            elif url.endswith(".xls"):
                body = base64.b64decode((fixtures / "shanghai_daily_20260819.xls.b64").read_text())
                content_type = "application/vnd.ms-excel"
            else:
                body, content_type = (fixtures / "shanghai_article.html").read_bytes(), "text/html"
            return FetchResult(
                request_url=url,
                final_url=url,
                status_code=200,
                headers={"content-type": content_type},
                body=body,
                fetched_at=OBSERVED_AT,
                duration_ms=1,
                fetch_method=FetchMethod.REPLAY,
            )

    government = _pipeline(mysql_engine, factory, ShanghaiFixtureFetcher(), tmp_path / "raw")
    outcome = asyncio.run(
        government.run_dataset(
            ShanghaiFreshRetailConnector(),
            CatalogCollectionRequest(
                region_scope=RegionScope.CITY,
                region_code="310100",
                category_codes=(SHANGHAI_FRESH_CATEGORY_CODE,),
            ),
        )
    )
    assert outcome.accepted_count == 8

    # Retain the exact full rows, not merely global counts in shared V2 tables.
    tables = {**Base.metadata.tables, **reflect_legacy_tables(mysql_engine).tables}
    with factory() as session:
        before = {
            table.name: session.execute(select(table).order_by(*table.primary_key)).mappings().all()
            for table in tables.values()
        }
    _seed(factory)
    pipeline = _pipeline(
        mysql_engine, factory, SequenceAppleFetcher([(OBSERVED_AT, _body())]), tmp_path / "raw"
    )
    assert asyncio.run(pipeline.run(FixedAppleConnector(), REQUEST)).status is RunStatus.SUCCEEDED

    with factory() as session:
        for name, original_rows in before.items():
            table = tables[name]
            if name in LEGACY_TABLE_NAMES:
                assert (
                    session.execute(select(table).order_by(*table.primary_key)).mappings().all()
                    == original_rows
                )
            else:
                for row in original_rows:
                    assert dict(
                        session.execute(select(table).where(table.c.id == row["id"]))
                        .mappings()
                        .one()
                    ) == dict(row)
        assert _count(session, CatalogPriceObservationRecord) == 11
        assert _count(session, SourceChannel) == 6


@pytest.mark.parametrize("mode", ["replay", "older", "conditional-new-spec", "parse-failure"])
def test_untrusted_or_old_apple_evidence_does_not_restore_listing_metadata_or_switch_revision(
    mysql_engine: Engine,
    migrated_session_factory: sessionmaker[Session],
    tmp_path: Path,
    mode: str,
) -> None:
    class ConditionalConnector(FixedAppleConnector):
        def parse_product(self, item, result):
            product = super().parse_product(item, result)
            return product.model_copy(
                update={
                    "rows": [
                        row.model_copy(
                            update={
                                "parsed": row.parsed.model_copy(
                                    update={
                                        "price_candidates": [
                                            candidate.model_copy(
                                                update={"price_type": PriceType.COUPON}
                                            )
                                            for candidate in row.parsed.price_candidates
                                        ]
                                    }
                                )
                            }
                        )
                        for row in product.rows
                    ]
                }
            )

    factory = migrated_session_factory
    _seed(factory)
    next_time = (
        OBSERVED_AT
        if mode == "replay"
        else OBSERVED_AT - timedelta(hours=1)
        if mode == "older"
        else OBSERVED_AT + timedelta(hours=1)
    )
    next_body = (
        b"invalid-page"
        if mode == "parse-failure"
        else _body().replace(b"256<small>GB", b"384<small>GB")
        if mode == "conditional-new-spec"
        else _body()
    )
    pipeline = _pipeline(
        mysql_engine,
        factory,
        SequenceAppleFetcher([(OBSERVED_AT, _body()), (next_time, next_body)]),
        tmp_path / "raw",
    )
    assert asyncio.run(pipeline.run(FixedAppleConnector(), REQUEST)).status is RunStatus.SUCCEEDED
    with factory.begin() as session:
        for listing in session.scalars(select(SourceListing)):
            listing.lifecycle_status = "INACTIVE"
            listing.consecutive_misses = 3
            listing.canonical_url = f"https://www.apple.com.cn/shop/kept/{listing.id}"
            listing.url_hash = sha256(listing.canonical_url.encode()).hexdigest()
        session.flush()
        before = session.execute(
            select(
                SourceListing.id,
                SourceListing.canonical_url,
                SourceListing.url_hash,
                SourceListing.lifecycle_status,
                SourceListing.consecutive_misses,
                SourceListing.current_revision_id,
                SourceListing.last_seen_at,
            ).order_by(SourceListing.id)
        ).all()
        current_ids = {
            row.price_observation_id for row in session.scalars(select(CatalogPriceCurrent))
        }
    connector = ConditionalConnector() if mode == "conditional-new-spec" else FixedAppleConnector()
    outcome = asyncio.run(pipeline.run(connector, REQUEST))

    assert outcome.status is (
        RunStatus.SUCCEEDED if mode in {"replay", "older"} else RunStatus.FAILED
    )
    with factory() as session:
        after = session.execute(
            select(
                SourceListing.id,
                SourceListing.canonical_url,
                SourceListing.url_hash,
                SourceListing.lifecycle_status,
                SourceListing.consecutive_misses,
                SourceListing.current_revision_id,
                SourceListing.last_seen_at,
            ).order_by(SourceListing.id)
        ).all()
        assert after == before
        assert {
            row.price_observation_id for row in session.scalars(select(CatalogPriceCurrent))
        } == current_ids
        if mode == "conditional-new-spec":
            assert outcome.rejected_count == 3
            assert _count(session, ListingRevision) == 4
            assert _count(session, CatalogPriceObservationRecord) == 6


def test_one_untrusted_apple_sku_prevents_all_current_prices_from_advancing(
    mysql_engine: Engine,
    migrated_session_factory: sessionmaker[Session],
    tmp_path: Path,
) -> None:
    class IncompleteSpecificationConnector(FixedAppleConnector):
        def parse_product(self, item, result):
            product = super().parse_product(item, result)
            rows = list(product.rows)
            rows[1] = rows[1].model_copy(
                update={"parsed": rows[1].parsed.model_copy(update={"source_attributes": {}})}
            )
            return product.model_copy(update={"rows": rows})

    factory = migrated_session_factory
    _seed(factory)
    pipeline = _pipeline(
        mysql_engine,
        factory,
        SequenceAppleFetcher(
            [
                (OBSERVED_AT, _body()),
                (
                    OBSERVED_AT + timedelta(minutes=1),
                    _body().replace(b"256<small>GB", b"384<small>GB"),
                ),
            ]
        ),
        tmp_path / "raw",
    )
    assert asyncio.run(pipeline.run(FixedAppleConnector(), REQUEST)).status is RunStatus.SUCCEEDED
    with factory() as session:
        before = (
            session.execute(select(CatalogPriceCurrent).order_by(CatalogPriceCurrent.id))
            .scalars()
            .all()
        )
        current_ids = {row.price_observation_id for row in before}
    outcome = asyncio.run(pipeline.run(IncompleteSpecificationConnector(), REQUEST))

    assert outcome.status is RunStatus.FAILED
    assert outcome.accepted_count == 0 and outcome.review_count == 3
    with factory() as session:
        assert {
            row.price_observation_id for row in session.scalars(select(CatalogPriceCurrent))
        } == current_ids
        assert _count(session, ItemVariant) == 3
        assert _count(session, ListingMatch) == 3


@pytest.mark.parametrize("rejected_specification", ["current", "incoming"])
def test_rejected_revision_audit_time_cannot_block_or_promote_a_trusted_specification(
    mysql_engine: Engine,
    migrated_session_factory: sessionmaker[Session],
    tmp_path: Path,
    rejected_specification: str,
) -> None:
    class SingleSkuConnector(FixedAppleConnector):
        def __init__(self, *, conditional: bool = False):
            super().__init__()
            self.conditional = conditional

        def parse_product(self, item, result):
            product = super().parse_product(item, result)
            row = product.rows[0]
            if self.conditional:
                row = row.model_copy(
                    update={
                        "parsed": row.parsed.model_copy(
                            update={
                                "price_candidates": [
                                    candidate.model_copy(update={"price_type": PriceType.COUPON})
                                    for candidate in row.parsed.price_candidates
                                ]
                            }
                        )
                    }
                )
            return product.model_copy(update={"rows": [row]})

    factory = migrated_session_factory
    _seed(factory)
    original_specification = _body()
    new_specification = _body().replace(b"256<small>GB", b"384<small>GB")
    initial_minute = 10 if rejected_specification == "current" else 25
    rejected_body = (
        original_specification if rejected_specification == "current" else new_specification
    )
    pipeline = _pipeline(
        mysql_engine,
        factory,
        SequenceAppleFetcher(
            [
                (OBSERVED_AT + timedelta(minutes=initial_minute), original_specification),
                (OBSERVED_AT + timedelta(minutes=30), rejected_body),
                (OBSERVED_AT + timedelta(minutes=20), new_specification),
            ]
        ),
        tmp_path / "raw",
    )
    first = asyncio.run(pipeline.run(SingleSkuConnector(), REQUEST))
    rejected = asyncio.run(pipeline.run(SingleSkuConnector(conditional=True), REQUEST))
    incoming = asyncio.run(pipeline.run(SingleSkuConnector(), REQUEST))

    assert first.status is incoming.status is RunStatus.SUCCEEDED
    assert rejected.status is RunStatus.FAILED and rejected.rejected_count == 1
    expected_capacity = "384GB" if rejected_specification == "current" else "256GB"
    expected_minute = 20 if rejected_specification == "current" else 25
    with factory.begin() as session:
        listing = session.scalar(select(SourceListing))
        assert listing is not None
        revision = session.get(ListingRevision, listing.current_revision_id)
        assert revision is not None
        assert revision.normalized_attributes["capacity"] == expected_capacity
        audited = session.scalar(
            select(ListingRevision).where(
                ListingRevision.source_listing_id == listing.id,
                ListingRevision.last_observed_at == OBSERVED_AT + timedelta(minutes=30),
            )
        )
        assert audited is not None
        assert audited.normalized_attributes["capacity"] == (
            "256GB" if rejected_specification == "current" else "384GB"
        )
        current = session.scalar(select(CatalogPriceCurrent))
        assert current is not None
        assert current.listing_revision_id == revision.id
        assert current.observed_at == OBSERVED_AT + timedelta(minutes=expected_minute)
        current_id = current.price_observation_id
        assert _count(session, CatalogPriceObservationRecord) == 3
        CatalogPriceRepository(session).rebuild_current(source_listing_id=listing.id)
        assert session.scalar(select(CatalogPriceCurrent.price_observation_id)) == current_id


def test_duplicate_apple_discovery_fails_the_whole_batch_before_fetching(
    mysql_engine: Engine,
    migrated_session_factory: sessionmaker[Session],
    tmp_path: Path,
) -> None:
    factory = migrated_session_factory
    _seed(factory)
    fetcher = SequenceAppleFetcher([(OBSERVED_AT, _body())])
    pipeline = _pipeline(mysql_engine, factory, fetcher, tmp_path / "raw")

    outcome = asyncio.run(pipeline.run(FixedAppleConnector([PRODUCT, PRODUCT]), REQUEST))

    assert outcome.status is RunStatus.FAILED
    assert outcome.fetched_count == outcome.accepted_count == 0
    assert not fetcher.calls
    with factory() as session:
        run = session.get(CatalogCrawlRun, outcome.crawl_run_id)
        assert run is not None and run.status == "FAILED"
        assert "DUPLICATE_DISCOVERY" in (run.error_summary or {})
        assert _count(session, SourceListing) == 0
        assert _count(session, CatalogPriceObservationRecord) == 0
        assert _count(session, CatalogPriceCurrent) == 0
