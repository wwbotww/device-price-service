from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import pytest
from sqlalchemy import Engine, func, select
from sqlalchemy.orm import Session, sessionmaker

from device_price_service.crawlers.base import AdapterContext
from device_price_service.crawlers.catalog import CatalogConnector
from device_price_service.db.catalog_models import (
    CatalogCrawlRecord,
    CatalogPriceCurrent,
    CatalogPriceObservationRecord,
    ListingRevision,
    Merchant,
    SourceListing,
    TaxonomyCategory,
)
from device_price_service.db.catalog_repositories import (
    CatalogIdentityConflictError,
    GeneralCatalogRepository,
)
from device_price_service.db.catalog_seed import seed_fresh_categories
from device_price_service.domain.catalog_crawl import (
    CatalogCollectionRequest,
    CatalogRegion,
    DiscoveredCatalogListing,
    ParsedCatalogListing,
    SourceMerchant,
    SourcePriceCandidate,
)
from device_price_service.domain.catalog_enums import (
    AccessMode,
    Availability,
    BusinessMode,
    CollectionFetchMethod,
    FeeStatus,
    MeasureType,
    OriginalPriceType,
    PriceNature,
    PriceType,
    PricingBasis,
    RegionMode,
    RegionScope,
    SellerType,
    SourceType,
    VerificationStatus,
)
from device_price_service.domain.crawl import FetchResult
from device_price_service.domain.enums import FetchMethod
from device_price_service.normalization.fresh_food import build_fresh_food_rule_registry
from device_price_service.services.artifact_store import RawArtifactStore
from device_price_service.services.catalog_crawl_pipeline import CatalogCrawlPipeline

pytestmark = pytest.mark.integration

FIXTURE_ROOT = Path(__file__).parents[1] / "fixtures" / "catalog_fresh"
BASE_OBSERVED_AT = datetime(2026, 8, 23, 8)


class FixtureReplayFetcher:
    def __init__(self, paths_by_url: dict[str, Path]) -> None:
        self.paths_by_url = paths_by_url
        self.calls = 0

    async def fetch(self, url: str, *, allowed_domains: list[str]) -> FetchResult:
        assert urlsplit(url).hostname in allowed_domains
        path = self.paths_by_url[url]
        fetched_at = BASE_OBSERVED_AT + timedelta(minutes=self.calls)
        self.calls += 1
        return FetchResult(
            request_url=url,
            final_url=url,
            status_code=200,
            headers={"content-type": "application/json"},
            body=path.read_bytes(),
            fetched_at=fetched_at,
            duration_ms=0,
            fetch_method=FetchMethod.REPLAY,
        )


class JsonFixtureConnector(CatalogConnector):
    version = "phase-d-fixture-1"
    fetch_method = CollectionFetchMethod.REPLAY

    def __init__(
        self,
        *,
        channel_code: str,
        connector_code: str,
        discovery_path: Path,
    ) -> None:
        self.channel_code = channel_code
        self.connector_code = connector_code
        self.discovery_path = discovery_path

    async def discover(
        self,
        context: AdapterContext,
        request: CatalogCollectionRequest,
    ) -> list[DiscoveredCatalogListing]:
        document = json.loads(self.discovery_path.read_bytes())
        merchant_data = document["merchant"]
        merchant = SourceMerchant(
            merchant_key=merchant_data["merchant_key"],
            external_merchant_id=merchant_data["external_merchant_id"],
            name=merchant_data["name"],
            seller_type=SellerType(merchant_data["seller_type"]),
            verification_status=VerificationStatus(merchant_data["verification_status"]),
        )
        return [
            DiscoveredCatalogListing(
                listing_key=row["listing_key"],
                url=row["url"],
                category_code=row["category_code"],
                merchant=merchant,
                price_nature=PriceNature(row["price_nature"]),
                external_product_id=row["external_product_id"],
                external_sku_id=row["external_sku_id"],
            )
            for row in document["listings"]
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
        document = json.loads(result.body)
        return ParsedCatalogListing(
            source_title=document["title"],
            source_category_path=document["category_path"],
            source_attributes=document["attributes"],
            price_candidates=[self._price(row) for row in document["prices"]],
        )

    @staticmethod
    def _price(row: dict[str, Any]) -> SourcePriceCandidate:
        region_data = row.get("region")
        region = (
            CatalogRegion(
                scope=RegionScope(region_data["scope"]),
                code=region_data["code"],
            )
            if region_data is not None
            else None
        )
        return SourcePriceCandidate(
            current_price=(
                Decimal(row["current_price"]) if row.get("current_price") is not None else None
            ),
            original_price=(
                Decimal(row["original_price"])
                if row.get("original_price") is not None
                else None
            ),
            original_price_type=OriginalPriceType(row["original_price_type"]),
            price_type=PriceType(row["price_type"]),
            pricing_basis=PricingBasis(row["pricing_basis"]),
            availability=Availability(row["availability"]),
            fee_status=FeeStatus(row["fee_status"]),
            displayed_price_text=row.get("displayed_price_text"),
            region=region,
        )


def _seed_sources(factory: sessionmaker[Session]) -> None:
    with factory.begin() as session:
        seed_fresh_categories(session)
        catalog = GeneralCatalogRepository(session)
        catalog.get_or_create_source_channel(
            code="FRESH_FIXTURE",
            name="脱敏生鲜电商样本",
            source_type=SourceType.MAJOR_ECOMMERCE,
            business_mode=BusinessMode.MARKETPLACE,
            access_mode=AccessMode.FILE,
            base_url="https://fresh.example.test",
            allowed_domains=["fresh.example.test"],
            region_mode=RegionMode.MIXED,
            connector_code="fresh-fixture",
        )
        catalog.get_or_create_source_channel(
            code="MARKET_FIXTURE",
            name="脱敏公共市场样本",
            source_type=SourceType.PUBLIC_DATA,
            business_mode=BusinessMode.WHOLESALE,
            access_mode=AccessMode.FILE,
            base_url="https://market.example.test",
            allowed_domains=["market.example.test"],
            region_mode=RegionMode.REGIONAL,
            connector_code="market-fixture",
        )


def _pipeline(
    engine: Engine,
    factory: sessionmaker[Session],
    fetcher: FixtureReplayFetcher,
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


def test_fresh_category_seed_is_idempotent_and_preserves_tree(
    session_factory: sessionmaker[Session],
) -> None:
    with session_factory.begin() as session:
        first = seed_fresh_categories(session)
        second = seed_fresh_categories(session)

        assert {code: row.id for code, row in first.items()} == {
            code: row.id for code, row in second.items()
        }
        assert len(first) == 8
        apple = first["FRESH_APPLE"]
        assert apple.parent_id == first["FRESH_FRUIT"].id
        assert apple.attribute_profile_code == "fresh-apple"
        assert apple.attribute_profile_version == "1"
        monitored = first["FRESH_MONITORED_COMMODITY"]
        assert monitored.parent_id == first["FRESH_FOOD"].id
        assert monitored.attribute_profile_code == "government-fresh"
        assert session.scalar(select(func.count()).select_from(TaxonomyCategory)) == 8
        with pytest.raises(CatalogIdentityConflictError, match="rule profile"):
            GeneralCatalogRepository(session).get_or_create_category(
                code="FRESH_APPLE",
                name_zh="鲜苹果",
                level=3,
                path="/FOOD/FRESH/FRUIT/APPLE/",
                is_leaf=True,
                attribute_profile_code="fresh-apple",
                attribute_profile_version="2",
                parent_id=first["FRESH_FRUIT"].id,
                default_measure_type=MeasureType.WEIGHT,
            )


def test_desensitized_fresh_and_public_market_fixtures_replay_end_to_end(
    mysql_engine: Engine,
    session_factory: sessionmaker[Session],
    tmp_path: Path,
) -> None:
    _seed_sources(session_factory)
    paths = {
        "https://fresh.example.test/items/apple-5kg": FIXTURE_ROOT / "apple_5kg.json",
        "https://fresh.example.test/items/egg-30": FIXTURE_ROOT / "egg_30.json",
        "https://fresh.example.test/items/pork-belly-500g": (
            FIXTURE_ROOT / "pork_belly_500g.json"
        ),
        "https://market.example.test/prices/red-fuji-grade1": (
            FIXTURE_ROOT / "public_market_apple.json"
        ),
    }
    fetcher = FixtureReplayFetcher(paths)
    pipeline = _pipeline(mysql_engine, session_factory, fetcher, tmp_path / "raw")
    commerce = JsonFixtureConnector(
        channel_code="FRESH_FIXTURE",
        connector_code="fresh-fixture",
        discovery_path=FIXTURE_ROOT / "ecommerce_discovery.json",
    )
    market = JsonFixtureConnector(
        channel_code="MARKET_FIXTURE",
        connector_code="market-fixture",
        discovery_path=FIXTURE_ROOT / "public_market_discovery.json",
    )
    commerce_request = CatalogCollectionRequest(
        region_scope=RegionScope.CITY,
        region_code="310100",
        category_codes=("FRESH_APPLE", "FRESH_EGG", "FRESH_PORK"),
    )
    market_request = CatalogCollectionRequest(
        region_scope=RegionScope.CITY,
        region_code="310100",
        category_codes=("FRESH_APPLE",),
    )

    commerce_first = asyncio.run(pipeline.run(commerce, commerce_request))
    commerce_second = asyncio.run(pipeline.run(commerce, commerce_request))
    market_first = asyncio.run(pipeline.run(market, market_request))
    market_second = asyncio.run(pipeline.run(market, market_request))

    assert commerce_first.accepted_count == commerce_second.accepted_count == 3
    assert commerce_first.rejected_count == commerce_second.rejected_count == 1
    assert market_first.accepted_count == market_second.accepted_count == 1
    assert commerce_first.failed_count == market_first.failed_count == 0

    with session_factory() as session:
        assert session.scalar(select(func.count()).select_from(Merchant)) == 2
        assert session.scalar(select(func.count()).select_from(SourceListing)) == 4
        assert session.scalar(select(func.count()).select_from(ListingRevision)) == 4
        assert (
            session.scalar(select(func.count()).select_from(CatalogPriceObservationRecord)) == 10
        )
        assert session.scalar(select(func.count()).select_from(CatalogPriceCurrent)) == 4

        accepted_current = session.execute(
            select(
                SourceListing.external_sku_id,
                CatalogPriceObservationRecord.current_price,
                CatalogPriceObservationRecord.unit_price,
                CatalogPriceObservationRecord.unit_price_unit,
                CatalogPriceObservationRecord.price_nature,
            )
            .join(
                CatalogPriceCurrent,
                CatalogPriceCurrent.source_listing_id == SourceListing.id,
            )
            .join(
                CatalogPriceObservationRecord,
                CatalogPriceObservationRecord.id == CatalogPriceCurrent.price_observation_id,
            )
        ).all()
        by_sku = {row.external_sku_id: row for row in accepted_current}
        assert by_sku["fixture-apple-5kg"].unit_price == Decimal("15.800000")
        assert by_sku["fixture-apple-5kg"].unit_price_unit == "CNY_PER_KG"
        assert by_sku["fixture-egg-30"].unit_price == Decimal("1.093333")
        assert by_sku["fixture-egg-30"].unit_price_unit == "CNY_PER_PIECE"
        assert by_sku["fixture-pork-belly-500g"].unit_price == Decimal("57.600000")
        assert by_sku["fixture-market-red-fuji-500g"].unit_price == Decimal("13.600000")
        assert by_sku["fixture-market-red-fuji-500g"].price_nature == "MARKET_AVERAGE"

        rejected = session.scalars(
            select(CatalogPriceObservationRecord).where(
                CatalogPriceObservationRecord.quality_status == "REJECTED"
            )
        ).all()
        assert len(rejected) == 2
        assert {row.rejection_code for row in rejected} == {"CONDITIONAL_PRICE"}
        records = session.scalars(select(CatalogCrawlRecord)).all()
        assert len(records) == 8
        assert all(row.fetch_method == "REPLAY" for row in records)
        assert all(row.raw_hash is not None and row.raw_path is not None for row in records)
