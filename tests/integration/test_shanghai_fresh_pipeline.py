from __future__ import annotations

import asyncio
import base64
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy import Engine, func, select
from sqlalchemy.orm import Session, sessionmaker

from device_price_service.crawlers.shanghai_fresh import (
    SHANGHAI_FRESH_CATEGORY_CODE,
    SHANGHAI_FRESH_INDEX_URL,
    ShanghaiFreshRetailConnector,
)
from device_price_service.db.catalog_models import (
    CatalogCrawlRecord,
    CatalogPriceCurrent,
    CatalogPriceObservationRecord,
    ListingRevision,
    SourceChannel,
    SourceListing,
)
from device_price_service.db.catalog_seed import (
    seed_fresh_categories,
    seed_shanghai_fresh_source,
)
from device_price_service.domain.catalog_crawl import CatalogCollectionRequest
from device_price_service.domain.catalog_enums import RegionScope
from device_price_service.domain.crawl import FetchResult
from device_price_service.domain.enums import FetchMethod
from device_price_service.normalization.fresh_food import build_fresh_food_rule_registry
from device_price_service.services.artifact_store import RawArtifactStore
from device_price_service.services.catalog_crawl_pipeline import CatalogCrawlPipeline

pytestmark = pytest.mark.integration

FIXTURE_ROOT = Path(__file__).parents[1] / "fixtures" / "catalog_fresh"
ARTICLE_URL = "https://fgw.sh.gov.cn/fgw_jgjgdt/20260819/fixture.html"
ATTACHMENT_URL = "https://fgw.sh.gov.cn/cmsres/sanitized/shanghai-fresh-20260819.xls"
FETCHED_AT = datetime(2026, 8, 20, 1)
SOURCE_OBSERVED_AT = datetime(2026, 8, 18, 16)


def _decoded_fixture(name: str) -> bytes:
    return base64.b64decode((FIXTURE_ROOT / name).read_text())


class ShanghaiFixtureFetcher:
    def __init__(self, attachments: list[bytes]) -> None:
        self.attachments = attachments
        self.calls: list[str] = []
        self.attachment_calls = 0

    async def fetch(self, url: str, *, allowed_domains: list[str]) -> FetchResult:
        assert allowed_domains == ["fgw.sh.gov.cn"]
        self.calls.append(url)
        if url == SHANGHAI_FRESH_INDEX_URL:
            body = (FIXTURE_ROOT / "shanghai_index.html").read_bytes()
            content_type = "text/html"
        elif url == ARTICLE_URL:
            body = (FIXTURE_ROOT / "shanghai_article.html").read_bytes()
            content_type = "text/html"
        elif url == ATTACHMENT_URL:
            body = self.attachments[min(self.attachment_calls, len(self.attachments) - 1)]
            self.attachment_calls += 1
            content_type = "application/vnd.ms-excel"
        else:
            raise AssertionError(f"unexpected fixture URL: {url}")
        return FetchResult(
            request_url=url,
            final_url=url,
            status_code=200,
            headers={"content-type": content_type},
            body=body,
            fetched_at=FETCHED_AT + timedelta(minutes=len(self.calls)),
            duration_ms=0,
            fetch_method=FetchMethod.REPLAY,
        )


def _seed(factory: sessionmaker[Session], *, enable: bool = True) -> None:
    with factory.begin() as session:
        seed_fresh_categories(session)
        seed_shanghai_fresh_source(session, enable=enable)


def _request() -> CatalogCollectionRequest:
    return CatalogCollectionRequest(
        region_scope=RegionScope.CITY,
        region_code="310100",
        category_codes=(SHANGHAI_FRESH_CATEGORY_CODE,),
    )


def _pipeline(
    engine: Engine,
    factory: sessionmaker[Session],
    fetcher: ShanghaiFixtureFetcher,
    raw_path: Path,
) -> CatalogCrawlPipeline:
    return CatalogCrawlPipeline(
        engine=engine,
        session_factory=factory,
        http_fetcher=fetcher,
        browser_fetcher=fetcher,  # type: ignore[arg-type]
        artifact_store=RawArtifactStore(raw_path),
        category_rules=build_fresh_food_rule_registry(),
    )


def test_shanghai_dataset_replay_is_idempotent_and_revisions_are_linked(
    mysql_engine: Engine,
    migrated_session_factory: sessionmaker[Session],
    tmp_path: Path,
) -> None:
    _seed(migrated_session_factory)
    first_file = _decoded_fixture("shanghai_daily_20260819.xls.b64")
    revised_file = _decoded_fixture("shanghai_daily_20260819_revision.xls.b64")
    fetcher = ShanghaiFixtureFetcher(
        [first_file, first_file, revised_file, revised_file]
    )
    pipeline = _pipeline(
        mysql_engine,
        migrated_session_factory,
        fetcher,
        tmp_path / "raw",
    )
    connector = ShanghaiFreshRetailConnector()

    outcomes = [asyncio.run(pipeline.run_dataset(connector, _request())) for _ in range(4)]

    assert [outcome.accepted_count for outcome in outcomes] == [8, 8, 8, 8]
    assert all(outcome.discovered_count == 8 for outcome in outcomes)
    assert all(outcome.fetched_count == 1 for outcome in outcomes)
    assert all(outcome.failed_count == 0 for outcome in outcomes)
    assert fetcher.attachment_calls == 4

    with migrated_session_factory() as session:
        assert session.scalar(select(func.count()).select_from(SourceListing)) == 8
        assert session.scalar(select(func.count()).select_from(ListingRevision)) == 8
        assert (
            session.scalar(select(func.count()).select_from(CatalogPriceObservationRecord))
            == 9
        )
        assert session.scalar(select(func.count()).select_from(CatalogPriceCurrent)) == 8
        assert session.scalar(select(func.count()).select_from(CatalogCrawlRecord)) == 4

        correction_count = session.scalar(
            select(func.count())
            .select_from(CatalogPriceObservationRecord)
            .where(CatalogPriceObservationRecord.supersedes_observation_id.is_not(None))
        )
        assert correction_count == 1

        current = session.execute(
            select(
                SourceListing.external_product_id,
                CatalogPriceObservationRecord,
            )
            .join(CatalogPriceCurrent, CatalogPriceCurrent.source_listing_id == SourceListing.id)
            .join(
                CatalogPriceObservationRecord,
                CatalogPriceObservationRecord.id == CatalogPriceCurrent.price_observation_id,
            )
        ).all()
        by_code = {code: observation for code, observation in current}
        assert by_code["QINGCAI"].current_price == Decimal("4.33")
        assert by_code["QINGCAI"].unit_price == Decimal("8.660000")
        assert all(row.original_price is None for row in by_code.values())
        assert all(row.price_nature == "RETAIL_AVERAGE" for row in by_code.values())
        assert all(row.observed_at == SOURCE_OBSERVED_AT for row in by_code.values())

        records = session.scalars(select(CatalogCrawlRecord)).all()
        assert all(record.source_listing_id is None for record in records)
        assert all(record.entity_type == "PUBLIC_PRICE" for record in records)
        assert all(record.raw_hash and record.raw_path for record in records)
        assert all(record.raw_path.endswith(".xls.gz") for record in records)
        assert all(record.artifact_manifest for record in records)


def test_shanghai_malformed_xls_keeps_failed_evidence_without_price_rows(
    mysql_engine: Engine,
    session_factory: sessionmaker[Session],
    tmp_path: Path,
) -> None:
    _seed(session_factory)
    fetcher = ShanghaiFixtureFetcher([b"<html>not an xls</html>"])
    pipeline = _pipeline(mysql_engine, session_factory, fetcher, tmp_path / "raw")

    outcome = asyncio.run(
        pipeline.run_dataset(ShanghaiFreshRetailConnector(), _request())
    )

    assert outcome.failed_count == 1
    assert outcome.accepted_count == 0
    with session_factory() as session:
        assert session.scalar(select(func.count()).select_from(SourceListing)) == 0
        assert (
            session.scalar(select(func.count()).select_from(CatalogPriceObservationRecord))
            == 0
        )
        record = session.scalar(select(CatalogCrawlRecord))
        assert record is not None
        assert record.fetch_status == "SUCCEEDED"
        assert record.parse_status == "FAILED"
        assert record.error_code == "ATTACHMENT_INVALID"
        assert record.raw_hash is not None


def test_shanghai_source_seed_is_disabled_until_explicitly_enabled(
    mysql_engine: Engine,
    session_factory: sessionmaker[Session],
    tmp_path: Path,
) -> None:
    _seed(session_factory, enable=False)
    fetcher = ShanghaiFixtureFetcher([_decoded_fixture("shanghai_daily_20260819.xls.b64")])
    pipeline = _pipeline(mysql_engine, session_factory, fetcher, tmp_path / "raw")

    with pytest.raises(RuntimeError, match="not enabled"):
        asyncio.run(pipeline.run_dataset(ShanghaiFreshRetailConnector(), _request()))

    with session_factory() as session:
        channel = session.scalar(select(SourceChannel))
        assert channel is not None
        assert channel.enabled is False
        assert session.scalar(select(func.count()).select_from(CatalogCrawlRecord)) == 0
