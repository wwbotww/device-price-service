from __future__ import annotations

import asyncio
import base64
from datetime import datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import Engine, event, inspect, select, text
from sqlalchemy.orm import Session, sessionmaker

from device_price_service.crawlers.apple import AppleCatalogConnector
from device_price_service.crawlers.base import AdapterContext
from device_price_service.crawlers.shanghai_fresh import (
    SHANGHAI_FRESH_CATEGORY_CODE,
    SHANGHAI_FRESH_INDEX_URL,
    ShanghaiFreshRetailConnector,
)
from device_price_service.db.catalog_models import (
    CatalogCrawlRecord,
    CatalogPriceCurrent,
    CatalogPriceObservationRecord,
    ListingMatch,
    SourceListing,
)
from device_price_service.db.catalog_seed import seed_fresh_categories, seed_shanghai_fresh_source
from device_price_service.db.device_seed import seed_device_catalog
from device_price_service.domain.catalog_crawl import (
    CatalogCollectionRequest,
    DiscoveredCatalogProduct,
    SourcePriceCandidate,
)
from device_price_service.domain.catalog_enums import (
    Availability,
    CollectionFetchMethod,
    FeeStatus,
    PriceType,
    PricingBasis,
    RegionScope,
    RunStatus,
)
from device_price_service.domain.crawl import FetchResult
from device_price_service.runtime import build_catalog_rule_registry
from device_price_service.services.artifact_store import RawArtifactStore
from device_price_service.services.catalog_crawl_pipeline import CatalogCrawlPipeline
from device_price_service.services.database_audit import audit_database

pytestmark = pytest.mark.integration
FIXTURES = Path(__file__).parents[1] / "fixtures"
OBSERVED_AT = datetime(2020, 1, 1, 8)
PRODUCT = DiscoveredCatalogProduct(
    external_product_id="iphone-fixture-pro",
    category_code="PHONE",
    url="https://www.apple.com.cn/shop/buy-iphone/iphone-fixture-pro",
)
REQUEST = CatalogCollectionRequest(
    region_scope=RegionScope.NATIONAL, region_code="CN", category_codes=("PHONE",)
)


class FixedAppleConnector(AppleCatalogConnector):
    async def discover_products(
        self, context: AdapterContext, request: CatalogCollectionRequest
    ) -> list[DiscoveredCatalogProduct]:
        return [PRODUCT]


class FixtureFetcher:
    def __init__(self) -> None:
        self.calls = 0
        self.body = (FIXTURES / "apple/product_iphone.html").read_bytes()
        self.government_revision = False

    async def fetch(self, url: str, *, allowed_domains: list[str]) -> FetchResult:
        body, content_type = self.body, "text/html"
        observed_at = OBSERVED_AT
        if allowed_domains == ["fgw.sh.gov.cn"]:
            observed_at = datetime(2026, 9, 1)
            folder = FIXTURES / "catalog_fresh"
            if url == SHANGHAI_FRESH_INDEX_URL:
                body = (folder / "shanghai_index.html").read_bytes()
            elif url.endswith(".xls"):
                suffix = "_revision" if self.government_revision else ""
                body = base64.b64decode(
                    (folder / f"shanghai_daily_20260819{suffix}.xls.b64").read_text()
                )
                content_type = "application/vnd.ms-excel"
            else:
                body = (folder / "shanghai_article.html").read_bytes()
        self.calls += 1
        return FetchResult(
            request_url=url,
            final_url=url,
            status_code=200,
            headers={"content-type": content_type},
            body=body,
            fetched_at=observed_at + timedelta(hours=self.calls),
            duration_ms=1,
            fetch_method=CollectionFetchMethod.HTTP,
        )


def _pipeline(engine: Engine, factory: sessionmaker[Session], raw_path: Path):
    fetcher = FixtureFetcher()
    store = RawArtifactStore(raw_path)
    pipeline = CatalogCrawlPipeline(
        engine=engine,
        session_factory=factory,
        http_fetcher=fetcher,
        browser_fetcher=fetcher,  # type: ignore[arg-type]
        artifact_store=store,
        category_rules=build_catalog_rule_registry(),
    )
    return pipeline, fetcher, store


def _collect(engine: Engine, factory: sessionmaker[Session], raw_path: Path):
    with factory.begin() as session:
        seed_device_catalog(session, enable=True)
    pipeline, fetcher, store = _pipeline(engine, factory, raw_path)
    result = asyncio.run(pipeline.run(FixedAppleConnector(), REQUEST))
    assert result.status is RunStatus.SUCCEEDED
    return pipeline, fetcher, store


def _audit(engine: Engine, store: RawArtifactStore | None = None):
    with engine.connect() as connection:
        return audit_database(
            connection,
            stale_run_minutes=120,
            stale_price_hours=12,
            artifact_store=store,
        )


def test_v2_only_audit_is_read_only_and_disabled_sources_do_not_warn(
    mysql_engine: Engine, session_factory: sessionmaker[Session], tmp_path: Path
) -> None:
    assert "official_offer" not in inspect(mysql_engine).get_table_names()
    with session_factory.begin() as session:
        seed_device_catalog(session)
    assert _audit(mysql_engine).warnings["channels_without_completed_run"] == 0
    _collect(mysql_engine, session_factory, tmp_path / "raw")
    statements: list[str] = []

    def capture(connection, cursor, statement, parameters, context, many):
        statements.append(statement)

    event.listen(mysql_engine, "before_cursor_execute", capture)
    try:
        report = _audit(mysql_engine)
    finally:
        event.remove(mysql_engine, "before_cursor_execute", capture)
    assert report.healthy, report.critical
    assert report.warnings["channels_without_completed_run"] == 4
    assert report.warnings["stale_current_price"] == 3
    assert statements and all(sql.lstrip().upper().startswith("SELECT") for sql in statements)
    assert all(" price_current " not in sql and " official_offer " not in sql for sql in statements)


@pytest.mark.parametrize(
    ("mutation", "error"),
    [
        ("DELETE FROM v2_price_current LIMIT 1", "missing_current_pointer"),
        (
            "UPDATE v2_price_observation SET observed_at = '2019-01-01' LIMIT 1",
            "current_pointer_mismatch",
        ),
        ("DELETE FROM v2_listing_match LIMIT 1", "device_match_cardinality"),
        (
            "UPDATE v2_item_variant SET attributes = JSON_OBJECT('color', 'changed') LIMIT 1",
            "device_standard_identity_mismatch",
        ),
        (
            "UPDATE v2_crawl_record SET raw_hash = REPEAT('a', 64) LIMIT 1",
            "untraceable_accepted_observation",
        ),
        (
            "UPDATE v2_crawl_run SET finished_at = NULL WHERE status = 'SUCCEEDED'",
            "invalid_batch_terminal_state",
        ),
        (
            "UPDATE v2_crawl_record SET artifact_manifest = JSON_ARRAY(NULL)",
            "invalid_artifact_reference",
        ),
        (
            "UPDATE v2_crawl_record SET artifact_manifest = JSON_OBJECT('bad', 'shape')",
            "invalid_artifact_reference",
        ),
    ],
)
def test_audit_detects_inconsistent_v2_data_without_repairing_it(
    mysql_engine: Engine,
    migrated_session_factory: sessionmaker[Session],
    tmp_path: Path,
    mutation: str,
    error: str,
) -> None:
    _collect(mysql_engine, migrated_session_factory, tmp_path / "raw")
    with mysql_engine.begin() as connection:
        connection.execute(text(mutation))
    report = _audit(mysql_engine)
    assert not report.healthy
    assert report.critical[error] > 0
    assert _audit(mysql_engine) == report


def test_confirmed_price_jump_pending_evidence_is_not_an_audit_error(
    mysql_engine: Engine, migrated_session_factory: sessionmaker[Session], tmp_path: Path
) -> None:
    pipeline, fetcher, store = _collect(mysql_engine, migrated_session_factory, tmp_path / "raw")
    fetcher.body = fetcher.body.replace(b"8,999", b"4,999")
    result = asyncio.run(pipeline.run(FixedAppleConnector(), REQUEST))
    assert result.status is RunStatus.SUCCEEDED
    with migrated_session_factory() as session:
        initial = session.scalar(
            select(CatalogCrawlRecord).where(
                CatalogCrawlRecord.error_code == "PRICE_CHANGE_CONFIRMATION_REQUIRED"
            )
        )
        assert initial is not None
        assert initial.validation_status == "PENDING"
    report = _audit(mysql_engine, store)
    assert report.healthy, report.critical


def test_audit_recognizes_government_same_day_correction_and_row_evidence(
    mysql_engine: Engine, migrated_session_factory: sessionmaker[Session], tmp_path: Path
) -> None:
    with migrated_session_factory.begin() as session:
        seed_fresh_categories(session)
        seed_shanghai_fresh_source(session, enable=True)
    pipeline, fetcher, store = _pipeline(mysql_engine, migrated_session_factory, tmp_path / "raw")
    request = CatalogCollectionRequest(
        region_scope=RegionScope.CITY,
        region_code="310100",
        category_codes=(SHANGHAI_FRESH_CATEGORY_CODE,),
    )
    for revision in (False, True):
        fetcher.government_revision = revision
        result = asyncio.run(pipeline.run_dataset(ShanghaiFreshRetailConnector(), request))
        assert result.status is RunStatus.SUCCEEDED
    report = _audit(mysql_engine, store)
    assert report.healthy, report.critical
    assert report.warnings["channels_without_completed_run"] == 0


def test_audit_optional_artifact_verification_and_legacy_table_isolation(
    mysql_engine: Engine, migrated_session_factory: sessionmaker[Session], tmp_path: Path
) -> None:
    _, _, store = _collect(mysql_engine, migrated_session_factory, tmp_path / "raw")
    with mysql_engine.begin() as connection:
        connection.execute(
            text("INSERT INTO brand(code, name_zh, name_en) VALUES('KEEP', '保留', 'Keep')")
        )
        before = connection.execute(text("SELECT * FROM brand")).all()
    assert _audit(mysql_engine, store).healthy
    # An empty local directory is not evidence that the database fact is invalid.
    missing_store = RawArtifactStore(tmp_path / "missing-raw")
    assert _audit(mysql_engine).healthy
    assert _audit(mysql_engine, missing_store).critical["invalid_artifact_reference"] == 1
    with mysql_engine.connect() as connection:
        assert connection.execute(text("SELECT * FROM brand")).all() == before


def test_stale_running_batch_is_only_a_warning_and_is_never_finished_by_audit(
    mysql_engine: Engine, migrated_session_factory: sessionmaker[Session], tmp_path: Path
) -> None:
    _collect(mysql_engine, migrated_session_factory, tmp_path / "raw")
    with mysql_engine.begin() as connection:
        # Observation fixture time is historical, but the pipeline starts runs now.
        # Age the batch itself to exercise the 120-minute recovery warning.
        connection.execute(
            text(
                "UPDATE v2_crawl_run SET status = 'RUNNING', finished_at = NULL, "
                "started_at = UTC_TIMESTAMP(3) - INTERVAL 3 HOUR"
            )
        )
    report = _audit(mysql_engine)
    assert report.healthy, report.critical
    assert report.warnings["stale_running_crawl"] == 1
    assert _audit(mysql_engine).warnings["stale_running_crawl"] == 1


def test_audit_reports_missing_v2_table_without_reading_legacy_tables(
    mysql_engine: Engine, session_factory: sessionmaker[Session]
) -> None:
    ListingMatch.__table__.drop(mysql_engine)
    report = _audit(mysql_engine)
    assert not report.healthy
    assert report.critical == {"missing_v2_tables": 1}


def test_valid_availability_only_fact_is_healthy_and_replaces_stale_amount(
    mysql_engine: Engine, migrated_session_factory: sessionmaker[Session], tmp_path: Path
) -> None:
    class StateConnector(FixedAppleConnector):
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
                                            SourcePriceCandidate(
                                                price_type=PriceType.AVAILABILITY_ONLY,
                                                pricing_basis=PricingBasis.UNKNOWN,
                                                fee_status=FeeStatus.NOT_APPLICABLE,
                                                availability=Availability.OUT_OF_STOCK,
                                            )
                                        ]
                                    }
                                )
                            }
                        )
                        for row in product.rows
                    ]
                }
            )

    pipeline, _, store = _collect(mysql_engine, migrated_session_factory, tmp_path / "raw")
    assert asyncio.run(pipeline.run(StateConnector(), REQUEST)).status is RunStatus.SUCCEEDED
    report = _audit(mysql_engine, store)
    assert report.healthy, report.critical
    with mysql_engine.connect() as connection:
        assert (
            connection.scalar(
                text(
                    "SELECT COUNT(*) FROM v2_price_current pc JOIN v2_price_observation po "
                    "ON po.id = pc.price_observation_id WHERE po.current_price IS NULL "
                    "AND po.price_type = 'AVAILABILITY_ONLY'"
                )
            )
            == 3
        )


def test_audit_detects_corrupt_raw_file_without_updating_evidence(
    mysql_engine: Engine, migrated_session_factory: sessionmaker[Session], tmp_path: Path
) -> None:
    _, _, store = _collect(mysql_engine, migrated_session_factory, tmp_path / "raw")
    with migrated_session_factory() as session:
        path = session.scalar(select(CatalogCrawlRecord.raw_path))
    assert path is not None
    (store.root / path).write_bytes(b"invalid gzip content")
    assert _audit(mysql_engine).healthy
    assert _audit(mysql_engine, store).critical["invalid_artifact_reference"] == 1


def test_device_same_time_correction_is_invalid_even_with_a_linked_chain(
    mysql_engine: Engine, migrated_session_factory: sessionmaker[Session], tmp_path: Path
) -> None:
    _collect(mysql_engine, migrated_session_factory, tmp_path / "raw")
    with migrated_session_factory.begin() as session:
        original = session.scalar(select(CatalogPriceObservationRecord))
        assert original is not None
        data = {
            column.name: getattr(original, column.name)
            for column in CatalogPriceObservationRecord.__table__.columns
            if column.name not in {"id", "created_at", "observation_key"}
        }
        data.update(supersedes_observation_id=original.id, observation_key="f" * 64)
        session.add(CatalogPriceObservationRecord(**data))
    assert _audit(mysql_engine).critical["invalid_same_time_correction"] == 1


def test_coherent_but_old_revision_and_current_pointer_are_detected(
    mysql_engine: Engine, migrated_session_factory: sessionmaker[Session], tmp_path: Path
) -> None:
    pipeline, fetcher, _ = _collect(mysql_engine, migrated_session_factory, tmp_path / "raw")
    with migrated_session_factory() as session:
        original = session.scalar(
            select(CatalogPriceObservationRecord).order_by(CatalogPriceObservationRecord.id)
        )
        assert original is not None
        old_id, listing_id = original.id, original.source_listing_id
    fetcher.body = fetcher.body.replace(b"256<small>GB", b"384<small>GB")
    assert asyncio.run(pipeline.run(FixedAppleConnector(), REQUEST)).status is RunStatus.SUCCEEDED
    assert _audit(mysql_engine).healthy
    with migrated_session_factory.begin() as session:
        original = session.get(CatalogPriceObservationRecord, old_id)
        listing = session.get(SourceListing, listing_id)
        pointer = session.scalar(
            select(CatalogPriceCurrent).where(CatalogPriceCurrent.source_listing_id == listing_id)
        )
        assert original is not None and listing is not None and pointer is not None
        assert listing.current_revision_id != original.listing_revision_id
        listing.current_revision_id = original.listing_revision_id
        # MySQL 5.7 validates a pointer against the persisted listing revision.
        # Persist that revision first instead of relying on ORM table flush order.
        session.flush([listing])
        pointer.listing_revision_id = original.listing_revision_id
        pointer.price_observation_id = original.id
        pointer.observed_at = original.observed_at
    report = _audit(mysql_engine)
    assert report.critical["current_pointer_mismatch"] == 0
    assert report.critical["outdated_current_revision"] == 1
