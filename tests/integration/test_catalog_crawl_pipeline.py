from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy import Engine, func, select
from sqlalchemy.orm import Session, sessionmaker

from device_price_service.crawlers.base import AdapterContext
from device_price_service.crawlers.catalog import CatalogConnector
from device_price_service.db.catalog_models import (
    CatalogCrawlRecord,
    CatalogCrawlRun,
    CatalogPriceCurrent,
    CatalogPriceObservationRecord,
    ListingRevision,
    Merchant,
    SourceListing,
)
from device_price_service.db.catalog_repositories import GeneralCatalogRepository
from device_price_service.domain.catalog_crawl import (
    CatalogCollectionRequest,
    DiscoveredCatalogListing,
    NormalizedListingIdentity,
    ParsedCatalogListing,
    SourceMerchant,
    SourcePriceCandidate,
)
from device_price_service.domain.catalog_enums import (
    AccessMode,
    Availability,
    BusinessMode,
    CollectionFetchMethod,
    ConditionCode,
    FeeStatus,
    MeasureType,
    OriginalPriceType,
    PriceNature,
    PriceType,
    PricingBasis,
    QualityStatus,
    RegionMode,
    RegionScope,
    SellerType,
    SourceType,
    VerificationStatus,
)
from device_price_service.domain.crawl import FetchResult
from device_price_service.domain.enums import FetchMethod
from device_price_service.normalization.catalog_rules import CategoryRule, CategoryRuleRegistry
from device_price_service.services.artifact_store import RawArtifactStore
from device_price_service.services.catalog_crawl_pipeline import CatalogCrawlPipeline

pytestmark = pytest.mark.integration

LISTING_URL = "https://catalog.example.test/product-1"
OBSERVED_AT = datetime(2026, 8, 22, 8)


class SequenceFetcher:
    def __init__(self, payloads: list[dict[str, object] | bytes]) -> None:
        self.payloads = payloads
        self.calls = 0

    async def fetch(self, url: str, *, allowed_domains: list[str]) -> FetchResult:
        assert url == LISTING_URL
        assert allowed_domains == ["catalog.example.test"]
        payload = self.payloads[self.calls]
        fetched_at = OBSERVED_AT + timedelta(minutes=10 * self.calls)
        self.calls += 1
        body = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
        return FetchResult(
            request_url=url,
            final_url=url,
            status_code=200,
            headers={"content-type": "application/json"},
            body=body,
            fetched_at=fetched_at,
            duration_ms=12,
            fetch_method=FetchMethod.HTTP,
        )


class FixtureConnector(CatalogConnector):
    channel_code = "CATALOG_FIXTURE"
    connector_code = "fixture-catalog"
    version = "fixture-catalog-1"
    fetch_method = CollectionFetchMethod.HTTP

    async def discover(
        self,
        context: AdapterContext,
        request: CatalogCollectionRequest,
    ) -> list[DiscoveredCatalogListing]:
        return [
            DiscoveredCatalogListing(
                listing_key="product-1:sku-main:retail",
                url=LISTING_URL,
                category_code="FRESH_FRUIT",
                merchant=SourceMerchant(
                    merchant_key="platform-self",
                    external_merchant_id="self-1",
                    name="测试平台自营",
                    seller_type=SellerType.PLATFORM_SELF,
                    verification_status=VerificationStatus.VERIFIED,
                ),
                price_nature=PriceNature.RETAIL_OFFER,
                external_product_id="product-1",
                external_sku_id="sku-main",
            )
        ]

    async def fetch_listing(
        self,
        context: AdapterContext,
        item: DiscoveredCatalogListing,
    ) -> FetchResult:
        return await context.http.fetch(item.url, allowed_domains=context.allowed_domains)

    def parse_listing(
        self,
        item: DiscoveredCatalogListing,
        result: FetchResult,
    ) -> ParsedCatalogListing:
        payload = json.loads(result.body)
        price_type = PriceType(str(payload.get("price_type", "DIRECT_UNCONDITIONAL")))
        return ParsedCatalogListing(
            source_title=str(payload["title"]),
            source_category_path="食品/生鲜/水果/苹果",
            source_attributes={
                "weight_kg": str(payload["weight_kg"]),
                "origin": str(payload["origin"]),
                "grade": str(payload["grade"]),
            },
            price_candidates=[
                SourcePriceCandidate(
                    current_price=Decimal(str(payload["price"])),
                    original_price=Decimal(str(payload["original_price"])),
                    original_price_type=OriginalPriceType.EXPLICIT_ORIGINAL,
                    price_type=price_type,
                    pricing_basis=PricingBasis.PACKAGE_TOTAL,
                    availability=Availability.ON_SALE,
                    fee_status=FeeStatus.ITEM_ONLY,
                    displayed_price_text=f"¥{payload['price']}",
                )
            ],
        )


class FruitFixtureRule(CategoryRule):
    profile_code = "fresh-fruit-fixture"
    version = "1"

    def normalize(
        self,
        item: DiscoveredCatalogListing,
        parsed: ParsedCatalogListing,
    ) -> NormalizedListingIdentity:
        weight = Decimal(str(parsed.source_attributes["weight_kg"]))
        return NormalizedListingIdentity(
            normalized_attributes={
                "origin": parsed.source_attributes["origin"],
                "grade": parsed.source_attributes["grade"],
            },
            condition_code=ConditionCode.NEW,
            measure_type=MeasureType.WEIGHT,
            quantity_value=weight,
            quantity_unit="KG",
            base_quantity_value=weight,
            base_unit="KG",
            package_count=1,
            quality_status=QualityStatus.ACCEPTED,
        )


def _payload(
    *,
    weight: str = "5",
    price: str = "79.00",
    price_type: str = "DIRECT_UNCONDITIONAL",
) -> dict[str, object]:
    return {
        "title": f"陕西红富士苹果 {weight}kg",
        "weight_kg": weight,
        "origin": "陕西洛川",
        "grade": "一级",
        "price": price,
        "original_price": "99.00" if weight == "5" else "169.00",
        "price_type": price_type,
    }


def _seed_catalog(factory: sessionmaker[Session]) -> None:
    with factory.begin() as session:
        catalog = GeneralCatalogRepository(session)
        catalog.get_or_create_source_channel(
            code=FixtureConnector.channel_code,
            name="测试综合平台",
            source_type=SourceType.MAJOR_ECOMMERCE,
            business_mode=BusinessMode.MARKETPLACE,
            access_mode=AccessMode.HTTP,
            base_url="https://catalog.example.test",
            allowed_domains=["catalog.example.test"],
            region_mode=RegionMode.MIXED,
            connector_code=FixtureConnector.connector_code,
        )
        catalog.get_or_create_category(
            code="FRESH_FRUIT",
            name_zh="新鲜水果",
            level=3,
            path="/FOOD/FRESH/FRUIT/",
            is_leaf=True,
            default_measure_type=MeasureType.WEIGHT,
            attribute_profile_code=FruitFixtureRule.profile_code,
            attribute_profile_version=FruitFixtureRule.version,
        )


def _pipeline(
    engine: Engine,
    factory: sessionmaker[Session],
    fetcher: SequenceFetcher,
    raw_path: Path,
) -> CatalogCrawlPipeline:
    rules = CategoryRuleRegistry()
    rules.register(FruitFixtureRule())
    return CatalogCrawlPipeline(
        engine=engine,
        session_factory=factory,
        http_fetcher=fetcher,
        browser_fetcher=fetcher,  # type: ignore[arg-type]
        artifact_store=RawArtifactStore(raw_path),
        category_rules=rules,
    )


def _request() -> CatalogCollectionRequest:
    return CatalogCollectionRequest(
        region_scope=RegionScope.CITY,
        region_code="310100",
        category_codes=("FRESH_FRUIT",),
    )


def test_general_pipeline_keeps_point_prices_and_switches_listing_revision(
    mysql_engine: Engine,
    session_factory: sessionmaker[Session],
    tmp_path: Path,
) -> None:
    _seed_catalog(session_factory)
    fetcher = SequenceFetcher(
        [
            _payload(),
            _payload(),
            _payload(weight="10", price="139.00"),
        ]
    )
    pipeline = _pipeline(mysql_engine, session_factory, fetcher, tmp_path / "raw")
    connector = FixtureConnector()

    first = asyncio.run(pipeline.run(connector, _request()))
    second = asyncio.run(pipeline.run(connector, _request()))
    third = asyncio.run(pipeline.run(connector, _request()))

    assert first.accepted_count == second.accepted_count == third.accepted_count == 1
    assert first.failed_count == second.failed_count == third.failed_count == 0
    with session_factory() as session:
        assert session.scalar(select(func.count()).select_from(Merchant)) == 1
        assert session.scalar(select(func.count()).select_from(SourceListing)) == 1
        assert session.scalar(select(func.count()).select_from(ListingRevision)) == 2
        assert (
            session.scalar(select(func.count()).select_from(CatalogPriceObservationRecord)) == 3
        )
        current = session.scalar(select(CatalogPriceCurrent))
        assert current is not None
        observation = session.get(CatalogPriceObservationRecord, current.price_observation_id)
        assert observation is not None
        assert observation.current_price == Decimal("139.00")
        assert observation.unit_price == Decimal("13.900000")
        listing = session.scalar(select(SourceListing))
        assert listing is not None
        assert current.listing_revision_id == listing.current_revision_id
        runs = session.scalars(select(CatalogCrawlRun).order_by(CatalogCrawlRun.id)).all()
        assert [run.status for run in runs] == ["SUCCEEDED", "SUCCEEDED", "SUCCEEDED"]
        assert [run.accepted_count for run in runs] == [1, 1, 1]


def test_rejected_conditional_price_is_a_fact_but_does_not_replace_current(
    mysql_engine: Engine,
    session_factory: sessionmaker[Session],
    tmp_path: Path,
) -> None:
    _seed_catalog(session_factory)
    fetcher = SequenceFetcher(
        [
            _payload(),
            _payload(price="59.00", price_type="COUPON"),
        ]
    )
    pipeline = _pipeline(mysql_engine, session_factory, fetcher, tmp_path / "raw")
    connector = FixtureConnector()

    accepted = asyncio.run(pipeline.run(connector, _request()))
    rejected = asyncio.run(pipeline.run(connector, _request()))

    assert accepted.accepted_count == 1
    assert rejected.rejected_count == 1
    assert rejected.failed_count == 0
    with session_factory() as session:
        observations = session.scalars(
            select(CatalogPriceObservationRecord).order_by(CatalogPriceObservationRecord.id)
        ).all()
        assert len(observations) == 2
        assert observations[1].quality_status == "REJECTED"
        assert observations[1].rejection_code == "CONDITIONAL_PRICE"
        current = session.scalar(select(CatalogPriceCurrent))
        assert current is not None
        assert current.price_observation_id == observations[0].id
        run = session.get(CatalogCrawlRun, rejected.crawl_run_id)
        assert run is not None
        assert run.status == "SUCCEEDED"
        assert run.rejected_count == 1


def test_parse_failure_keeps_evidence_and_does_not_create_price(
    mysql_engine: Engine,
    session_factory: sessionmaker[Session],
    tmp_path: Path,
) -> None:
    _seed_catalog(session_factory)
    pipeline = _pipeline(
        mysql_engine,
        session_factory,
        SequenceFetcher([b"not-json"]),
        tmp_path / "raw",
    )

    outcome = asyncio.run(pipeline.run(FixtureConnector(), _request()))

    assert outcome.failed_count == 1
    assert outcome.accepted_count == 0
    with session_factory() as session:
        run = session.get(CatalogCrawlRun, outcome.crawl_run_id)
        assert run is not None
        assert run.status == "FAILED"
        record = session.scalar(
            select(CatalogCrawlRecord).where(
                CatalogCrawlRecord.crawl_run_id == outcome.crawl_run_id
            )
        )
        assert record is not None
        assert record.fetch_status == "SUCCEEDED"
        assert record.parse_status == "FAILED"
        assert record.raw_hash is not None
        assert record.raw_path is not None
        assert session.scalar(select(func.count()).select_from(CatalogPriceCurrent)) == 0
        assert (
            session.scalar(select(func.count()).select_from(CatalogPriceObservationRecord)) == 0
        )
