from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy import Engine, func, select
from sqlalchemy.orm import Session, sessionmaker

from device_price_service.crawlers.mofcom_fresh import (
    MOFCOM_FRESH_BASE_URL,
    MofcomFreshWholesaleConnector,
)
from device_price_service.db.catalog_models import (
    CatalogCrawlRecord,
    CatalogPriceCurrent,
    CatalogPriceObservationRecord,
    ListingRevision,
    Merchant,
    SourceChannel,
    SourceListing,
)
from device_price_service.db.catalog_seed import (
    seed_fresh_categories,
    seed_mofcom_fresh_source,
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
PAGE_URL = f"{MOFCOM_FRESH_BASE_URL}?commdityid=170130"
FETCHED_AT = datetime(2026, 8, 24, 1)
SOURCE_OBSERVED_AT = datetime(2026, 8, 22, 16)


def _fixture(name: str) -> bytes:
    return (FIXTURE_ROOT / name).read_bytes()


class MofcomFixtureFetcher:
    def __init__(self, responses: list[bytes]) -> None:
        self.responses = responses
        self.calls: list[str] = []

    async def fetch(
        self,
        url: str,
        *,
        allowed_domains: list[str],
        tls_profile: str = "DEFAULT",
    ) -> FetchResult:
        assert url == PAGE_URL
        assert allowed_domains == ["cif.mofcom.gov.cn"]
        assert tls_profile == "TLS12_COMPAT"
        response_index = min(len(self.calls), len(self.responses) - 1)
        self.calls.append(url)
        return FetchResult(
            request_url=url,
            final_url=url,
            status_code=200,
            headers={"content-type": "text/html; charset=UTF-8"},
            body=self.responses[response_index],
            fetched_at=FETCHED_AT + timedelta(minutes=len(self.calls)),
            duration_ms=0,
            fetch_method=FetchMethod.REPLAY,
        )


def _seed(factory: sessionmaker[Session], *, enable: bool = True) -> None:
    with factory.begin() as session:
        seed_fresh_categories(session)
        seed_mofcom_fresh_source(session, enable=enable)


def _request() -> CatalogCollectionRequest:
    return CatalogCollectionRequest(
        region_scope=RegionScope.MULTI,
        region_code="*",
        category_codes=("FRESH_MONITORED_COMMODITY",),
        source_item_codes=("CUCUMBER",),
    )


def _pipeline(
    engine: Engine,
    factory: sessionmaker[Session],
    fetcher: MofcomFixtureFetcher,
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


def test_mofcom_dataset_replay_is_idempotent_and_same_day_revision_is_linked(
    mysql_engine: Engine,
    migrated_session_factory: sessionmaker[Session],
    tmp_path: Path,
) -> None:
    _seed(migrated_session_factory)
    first = _fixture("mofcom_cucumber_20260823.html")
    revised = _fixture("mofcom_cucumber_20260823_revision.html")
    fetcher = MofcomFixtureFetcher(
        [first, first, first, first, revised, revised, revised, revised]
    )
    pipeline = _pipeline(
        mysql_engine,
        migrated_session_factory,
        fetcher,
        tmp_path / "raw",
    )
    connector = MofcomFreshWholesaleConnector()

    outcomes = [asyncio.run(pipeline.run_dataset(connector, _request())) for _ in range(4)]

    assert [outcome.accepted_count for outcome in outcomes] == [5, 5, 5, 5]
    assert all(outcome.discovered_count == 4 for outcome in outcomes)
    assert all(outcome.fetched_count == 1 for outcome in outcomes)
    assert all(outcome.failed_count == 0 for outcome in outcomes)
    assert len(fetcher.calls) == 8

    with migrated_session_factory() as session:
        assert session.scalar(select(func.count()).select_from(Merchant)) == 4
        assert session.scalar(select(func.count()).select_from(SourceListing)) == 4
        assert session.scalar(select(func.count()).select_from(ListingRevision)) == 4
        assert (
            session.scalar(select(func.count()).select_from(CatalogPriceObservationRecord))
            == 6
        )
        assert session.scalar(select(func.count()).select_from(CatalogPriceCurrent)) == 5
        assert session.scalar(select(func.count()).select_from(CatalogCrawlRecord)) == 4
        assert (
            session.scalar(
                select(func.count())
                .select_from(CatalogPriceObservationRecord)
                .where(CatalogPriceObservationRecord.supersedes_observation_id.is_not(None))
            )
            == 1
        )

        beijing = session.scalar(
            select(CatalogPriceObservationRecord)
            .join(
                CatalogPriceCurrent,
                CatalogPriceCurrent.price_observation_id
                == CatalogPriceObservationRecord.id,
            )
            .where(CatalogPriceObservationRecord.region_code == "110000")
        )
        assert beijing is not None
        assert beijing.current_price == Decimal("8.60")
        assert beijing.unit_price == Decimal("8.600000")
        assert beijing.original_price is None
        assert beijing.price_nature == "WHOLESALE_AVERAGE"
        assert beijing.observed_at == SOURCE_OBSERVED_AT

        records = session.scalars(select(CatalogCrawlRecord)).all()
        assert all(record.source_listing_id is None for record in records)
        assert all(record.entity_type == "PUBLIC_PRICE" for record in records)
        assert all(record.raw_hash and record.raw_path for record in records)
        assert all(record.raw_path.endswith(".html.gz") for record in records)


def test_mofcom_structure_failure_preserves_evidence_without_price_rows(
    mysql_engine: Engine,
    session_factory: sessionmaker[Session],
    tmp_path: Path,
) -> None:
    _seed(session_factory)
    valid = _fixture("mofcom_cucumber_20260823.html")
    fetcher = MofcomFixtureFetcher([valid, b"<html>changed</html>"])
    pipeline = _pipeline(mysql_engine, session_factory, fetcher, tmp_path / "raw")

    outcome = asyncio.run(
        pipeline.run_dataset(MofcomFreshWholesaleConnector(), _request())
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
        assert record.error_code == "DISCOVERY_STRUCTURE_CHANGED"
        assert record.raw_hash is not None


def test_mofcom_source_seed_is_disabled_until_explicitly_enabled(
    mysql_engine: Engine,
    session_factory: sessionmaker[Session],
    tmp_path: Path,
) -> None:
    _seed(session_factory, enable=False)
    fetcher = MofcomFixtureFetcher([_fixture("mofcom_cucumber_20260823.html")])
    pipeline = _pipeline(mysql_engine, session_factory, fetcher, tmp_path / "raw")

    with pytest.raises(RuntimeError, match="not enabled"):
        asyncio.run(pipeline.run_dataset(MofcomFreshWholesaleConnector(), _request()))

    with session_factory() as session:
        channel = session.scalar(select(SourceChannel))
        assert channel is not None
        assert channel.enabled is False
        assert session.scalar(select(func.count()).select_from(CatalogCrawlRecord)) == 0
    assert fetcher.calls == []
