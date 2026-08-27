from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session, sessionmaker

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
from device_price_service.db.catalog_repositories import (
    CatalogCrawlRepository,
    CatalogPriceRepository,
    GeneralCatalogRepository,
)
from device_price_service.domain.catalog_enums import (
    AccessMode,
    Availability,
    BusinessMode,
    CatalogEntityType,
    CollectionFetchMethod,
    CollectionTriggerType,
    ConditionCode,
    FeeStatus,
    ItemType,
    MatchMethod,
    MatchStatus,
    MeasureType,
    OperationStatus,
    OriginalPriceType,
    PriceNature,
    PriceType,
    PricingBasis,
    QualityStatus,
    RecordOrigin,
    RegionMode,
    RegionScope,
    RunType,
    SellerType,
    SourceType,
    VerificationStatus,
)
from device_price_service.domain.catalog_models import CatalogPriceObservation

pytestmark = pytest.mark.integration


@dataclass(frozen=True, slots=True)
class SourceFixture:
    listing_id: int
    revision_id: int
    crawl_record_id: int
    observed_at: datetime


def _build_source(
    factory: sessionmaker[Session],
    *,
    observed_at: datetime = datetime(2026, 8, 21, 8),
    run_region_scope: RegionScope = RegionScope.CITY,
    run_region_code: str = "310100",
) -> SourceFixture:
    with factory.begin() as session:
        catalog = GeneralCatalogRepository(session)
        channel = catalog.get_or_create_source_channel(
            code="PLATFORM_CN",
            name="测试平台",
            source_type=SourceType.MAJOR_ECOMMERCE,
            business_mode=BusinessMode.MARKETPLACE,
            access_mode=AccessMode.HTTP,
            base_url="https://example.test",
            allowed_domains=["example.test"],
            region_mode=RegionMode.MIXED,
            connector_code="fixture",
        )
        merchant = catalog.get_or_create_merchant(
            source_channel_id=channel.id,
            merchant_key_hash="a" * 64,
            name="平台自营",
            seller_type=SellerType.PLATFORM_SELF,
            verification_status=VerificationStatus.VERIFIED,
            observed_at=observed_at,
        )
        listing = catalog.get_or_create_source_listing(
            source_channel_id=channel.id,
            merchant_id=merchant.id,
            listing_key="product-1:sku-5kg:retail",
            listing_key_hash="b" * 64,
            price_nature=PriceNature.RETAIL_OFFER,
            canonical_url="https://example.test/product-1",
            url_hash="c" * 64,
            observed_at=observed_at,
            external_product_id="product-1",
            external_sku_id="sku-5kg",
        )
        crawl = CatalogCrawlRepository(session)
        run = crawl.start_run(
            source_channel_id=channel.id,
            region_scope=run_region_scope,
            region_code=run_region_code,
            category_scope=["FRESH_FRUIT"],
            run_type=RunType.FULL,
            trigger_type=CollectionTriggerType.MANUAL,
            adapter_version="fixture-adapter-1",
            policy_version="fixture-policy-1",
            started_at=observed_at,
        )
        record = crawl.add_record(
            crawl_run_id=run.id,
            source_listing_id=listing.id,
            entity_type=CatalogEntityType.LISTING,
            entity_key="product-1:sku-5kg",
            request_url="https://example.test/product-1",
            final_url="https://example.test/product-1",
            fetch_method=CollectionFetchMethod.HTTP,
            http_status=200,
            fetch_status=OperationStatus.SUCCEEDED,
            parse_status=OperationStatus.SUCCEEDED,
            validation_status=OperationStatus.SUCCEEDED,
            raw_hash="d" * 64,
            raw_path="fixture/product-1.json",
            duration_ms=10,
            fetched_at=observed_at,
        )
        revision = catalog.get_or_create_listing_revision(
            source_listing_id=listing.id,
            first_crawl_record_id=record.id,
            source_title="测试苹果 5kg",
            source_attributes={"weight": "5kg"},
            normalized_attributes={"weight_kg": "5.000000"},
            condition_code=ConditionCode.NEW,
            measure_type=MeasureType.WEIGHT,
            quantity_value=Decimal("5"),
            quantity_unit="KG",
            base_quantity_value=Decimal("5"),
            base_unit="KG",
            package_count=1,
            identity_fingerprint="e" * 64,
            normalizer_version="fixture-normalizer-1",
            quality_status=QualityStatus.ACCEPTED,
            observed_at=observed_at,
        )
        catalog.set_current_revision(source_listing_id=listing.id, revision_id=revision.id)
        return SourceFixture(listing.id, revision.id, record.id, observed_at)


def _observation(
    fixture: SourceFixture,
    *,
    observed_at: datetime,
    current_price: str = "79.00",
    source_hash: str = "f" * 64,
    region_scope: RegionScope = RegionScope.CITY,
    region_code: str = "310100",
    quality_status: QualityStatus = QualityStatus.ACCEPTED,
    price_type: PriceType = PriceType.DIRECT_UNCONDITIONAL,
    rejection_code: str | None = None,
    supersedes_observation_id: int | None = None,
    revision_id: int | None = None,
) -> CatalogPriceObservation:
    return CatalogPriceObservation(
        source_listing_id=fixture.listing_id,
        listing_revision_id=revision_id or fixture.revision_id,
        crawl_record_id=fixture.crawl_record_id,
        supersedes_observation_id=supersedes_observation_id,
        region_scope=region_scope,
        region_code=region_code,
        original_price=Decimal("99.00"),
        original_price_type=OriginalPriceType.EXPLICIT_ORIGINAL,
        current_price=Decimal(current_price),
        price_nature=PriceNature.RETAIL_OFFER,
        price_type=price_type,
        pricing_basis=PricingBasis.PACKAGE_TOTAL,
        availability=Availability.ON_SALE,
        unit_price=Decimal(current_price) / Decimal("5"),
        unit_price_unit="CNY_PER_KG",
        fee_status=FeeStatus.ITEM_ONLY,
        quality_status=quality_status,
        rejection_code=rejection_code,
        source_hash=source_hash,
        observed_at=observed_at,
    )


def test_v2_reference_and_source_identity_operations_are_idempotent(
    session_factory: sessionmaker[Session],
) -> None:
    first = _build_source(session_factory)
    second = _build_source(session_factory, observed_at=first.observed_at + timedelta(minutes=5))

    with session_factory.begin() as session:
        catalog = GeneralCatalogRepository(session)
        brand = catalog.get_or_create_brand(code="TEST", name_zh="测试")
        category = catalog.get_or_create_category(
            code="FRESH_FRUIT",
            name_zh="新鲜水果",
            level=1,
            path="/FOOD/FRESH_FRUIT",
            is_leaf=True,
            default_measure_type=MeasureType.WEIGHT,
            attribute_profile_code="fresh-fruit",
            attribute_profile_version="1",
        )
        item = catalog.get_or_create_item(
            category_id=category.id,
            brand_id=brand.id,
            canonical_key="test-apple",
            item_type=ItemType.COMMODITY,
            name="测试苹果",
            record_origin=RecordOrigin.MANUAL,
        )
        variant = catalog.get_or_create_variant(
            catalog_item_id=item.id,
            variant_key="test-apple-5kg",
            name="测试苹果 5kg",
            condition_code=ConditionCode.NEW,
            measure_type=MeasureType.WEIGHT,
            quantity_value=Decimal("5"),
            base_unit="KG",
            package_count=1,
            identity_fingerprint="1" * 64,
        )
        assert catalog.get_or_create_brand(code="TEST", name_zh="测试").id == brand.id
        assert (
            catalog.get_or_create_variant(
                catalog_item_id=item.id,
                variant_key="test-apple-5kg",
                name="测试苹果 5kg",
                condition_code=ConditionCode.NEW,
                measure_type=MeasureType.WEIGHT,
                quantity_value=Decimal("5"),
                base_unit="KG",
                package_count=1,
                identity_fingerprint="1" * 64,
            ).id
            == variant.id
        )

    with session_factory() as session:
        assert first.listing_id == second.listing_id
        assert first.revision_id == second.revision_id
        assert session.scalar(select(func.count()).select_from(SourceChannel)) == 1
        assert session.scalar(select(func.count()).select_from(Merchant)) == 1
        assert session.scalar(select(func.count()).select_from(SourceListing)) == 1
        assert session.scalar(select(func.count()).select_from(ListingRevision)) == 1
        assert session.scalar(select(func.count()).select_from(CatalogCrawlRun)) == 2
        assert session.scalar(select(func.count()).select_from(CatalogCrawlRecord)) == 2
        assert session.scalar(select(func.count()).select_from(CatalogBrand)) == 1
        assert session.scalar(select(func.count()).select_from(TaxonomyCategory)) == 1
        assert session.scalar(select(func.count()).select_from(CatalogItem)) == 1
        assert session.scalar(select(func.count()).select_from(ItemVariant)) == 1


def test_equivalent_source_units_reuse_the_same_listing_revision(
    session_factory: sessionmaker[Session],
) -> None:
    fixture = _build_source(session_factory)

    with session_factory.begin() as session:
        catalog = GeneralCatalogRepository(session)
        equivalent = catalog.get_or_create_listing_revision(
            source_listing_id=fixture.listing_id,
            first_crawl_record_id=fixture.crawl_record_id,
            source_title="测试苹果 5000g",
            source_attributes={"weight": "5000g"},
            normalized_attributes={"weight_kg": "5.000000"},
            condition_code=ConditionCode.NEW,
            measure_type=MeasureType.WEIGHT,
            quantity_value=Decimal("5000"),
            quantity_unit="G",
            base_quantity_value=Decimal("5"),
            base_unit="KG",
            package_count=1,
            identity_fingerprint="e" * 64,
            normalizer_version="fixture-normalizer-1",
            quality_status=QualityStatus.ACCEPTED,
            observed_at=fixture.observed_at + timedelta(minutes=5),
        )

        assert equivalent.id == fixture.revision_id

    with session_factory() as session:
        assert session.scalar(select(func.count()).select_from(ListingRevision)) == 1


def test_same_price_each_capture_is_a_point_observation_and_retry_is_idempotent(
    session_factory: sessionmaker[Session],
) -> None:
    fixture = _build_source(session_factory)
    t1 = fixture.observed_at + timedelta(minutes=10)
    t2 = t1 + timedelta(minutes=10)

    with session_factory.begin() as session:
        repository = CatalogPriceRepository(session)
        first = repository.record(_observation(fixture, observed_at=t1, source_hash="1" * 64))
        retry = repository.record(_observation(fixture, observed_at=t1, source_hash="1" * 64))
        second = repository.record(_observation(fixture, observed_at=t2, source_hash="2" * 64))
        assert first.observation_created is True
        assert retry.observation_created is False
        assert second.current_advanced is True

    with session_factory() as session:
        assert session.scalar(select(func.count()).select_from(CatalogPriceObservationRecord)) == 2
        current = session.scalar(select(CatalogPriceCurrent))
        assert current is not None
        assert current.observed_at == t2
        assert current.price_observation_id == second.observation_id


def test_out_of_order_and_rejected_observations_never_replace_current(
    session_factory: sessionmaker[Session],
) -> None:
    fixture = _build_source(session_factory)
    old_time = fixture.observed_at + timedelta(minutes=10)
    current_time = old_time + timedelta(minutes=20)

    with session_factory.begin() as session:
        repository = CatalogPriceRepository(session)
        current_result = repository.record(
            _observation(fixture, observed_at=current_time, current_price="79.00")
        )
        old_result = repository.record(
            _observation(
                fixture,
                observed_at=old_time,
                current_price="69.00",
                source_hash="3" * 64,
            )
        )
        rejected = repository.record(
            _observation(
                fixture,
                observed_at=current_time + timedelta(minutes=10),
                current_price="59.00",
                source_hash="4" * 64,
                price_type=PriceType.COUPON,
                quality_status=QualityStatus.REJECTED,
                rejection_code="CONDITIONAL_PRICE",
            )
        )
        assert old_result.current_advanced is False
        assert rejected.current_advanced is False

    with session_factory() as session:
        assert session.scalar(select(func.count()).select_from(CatalogPriceObservationRecord)) == 3
        current = session.scalar(select(CatalogPriceCurrent))
        assert current is not None
        assert current.price_observation_id == current_result.observation_id


def test_trusted_same_time_correction_replaces_pointer_without_mutating_history(
    session_factory: sessionmaker[Session],
) -> None:
    fixture = _build_source(session_factory)
    observed_at = fixture.observed_at + timedelta(minutes=10)

    with session_factory.begin() as session:
        repository = CatalogPriceRepository(session)
        first = repository.record(
            _observation(
                fixture,
                observed_at=observed_at,
                current_price="79.00",
                source_hash="5" * 64,
            )
        )
        correction = repository.record(
            _observation(
                fixture,
                observed_at=observed_at,
                current_price="78.00",
                source_hash="6" * 64,
                supersedes_observation_id=first.observation_id,
            )
        )
        assert correction.current_advanced is True

    with session_factory() as session:
        observations = session.scalars(
            select(CatalogPriceObservationRecord).order_by(CatalogPriceObservationRecord.id)
        ).all()
        current = session.scalar(select(CatalogPriceCurrent))
        assert len(observations) == 2
        assert observations[0].current_price == Decimal("79.00")
        assert observations[1].current_price == Decimal("78.00")
        assert current is not None
        assert current.price_observation_id == observations[1].id


def test_listing_identity_switch_invalidates_old_projection(
    session_factory: sessionmaker[Session],
) -> None:
    fixture = _build_source(session_factory)
    with session_factory.begin() as session:
        CatalogPriceRepository(session).record(
            _observation(fixture, observed_at=fixture.observed_at + timedelta(minutes=5))
        )

    with session_factory.begin() as session:
        catalog = GeneralCatalogRepository(session)
        revision = catalog.get_or_create_listing_revision(
            source_listing_id=fixture.listing_id,
            first_crawl_record_id=fixture.crawl_record_id,
            source_title="测试苹果 10kg",
            source_attributes={"weight": "10kg"},
            normalized_attributes={"weight_kg": "10.000000"},
            condition_code=ConditionCode.NEW,
            measure_type=MeasureType.WEIGHT,
            quantity_value=Decimal("10"),
            quantity_unit="KG",
            base_quantity_value=Decimal("10"),
            base_unit="KG",
            package_count=1,
            identity_fingerprint="6" * 64,
            normalizer_version="fixture-normalizer-1",
            quality_status=QualityStatus.ACCEPTED,
            observed_at=fixture.observed_at + timedelta(minutes=10),
        )
        catalog.set_current_revision(source_listing_id=fixture.listing_id, revision_id=revision.id)
        assert session.scalar(select(func.count()).select_from(CatalogPriceCurrent)) == 0

        result = CatalogPriceRepository(session).record(
            _observation(
                fixture,
                revision_id=revision.id,
                observed_at=fixture.observed_at + timedelta(minutes=10),
                current_price="139.00",
                source_hash="7" * 64,
            )
        )
        assert result.current_advanced is True

    with session_factory() as session:
        assert session.scalar(select(func.count()).select_from(CatalogPriceObservationRecord)) == 2
        current = session.scalar(select(CatalogPriceCurrent))
        assert current is not None
        assert current.listing_revision_id != fixture.revision_id


def test_regions_are_independent_and_projection_can_be_rebuilt(
    session_factory: sessionmaker[Session],
) -> None:
    fixture = _build_source(
        session_factory,
        run_region_scope=RegionScope.MULTI,
        run_region_code="*",
    )
    observed_at = fixture.observed_at + timedelta(minutes=5)
    with session_factory.begin() as session:
        repository = CatalogPriceRepository(session)
        repository.record(
            _observation(
                fixture,
                observed_at=observed_at,
                region_code="310100",
                current_price="79.00",
                source_hash="8" * 64,
            )
        )
        repository.record(
            _observation(
                fixture,
                observed_at=observed_at,
                region_code="110100",
                current_price="81.00",
                source_hash="9" * 64,
            )
        )
        assert session.scalar(select(func.count()).select_from(CatalogPriceCurrent)) == 2
        session.execute(delete(CatalogPriceCurrent))
        assert repository.rebuild_current(source_listing_id=fixture.listing_id) == 2

    with session_factory() as session:
        regions = set(
            session.execute(
                select(
                    CatalogPriceCurrent.region_code, CatalogPriceObservationRecord.current_price
                ).join(
                    CatalogPriceObservationRecord,
                    CatalogPriceObservationRecord.id == CatalogPriceCurrent.price_observation_id,
                )
            ).all()
        )
        assert regions == {("310100", Decimal("79.00")), ("110100", Decimal("81.00"))}


def test_price_write_rolls_back_observation_and_projection_together(
    session_factory: sessionmaker[Session],
) -> None:
    fixture = _build_source(session_factory)

    with pytest.raises(RuntimeError, match="force rollback"), session_factory.begin() as session:
        CatalogPriceRepository(session).record(
            _observation(fixture, observed_at=fixture.observed_at + timedelta(minutes=5))
        )
        raise RuntimeError("force rollback")

    with session_factory() as session:
        assert session.scalar(select(func.count()).select_from(CatalogPriceObservationRecord)) == 0
        assert session.scalar(select(func.count()).select_from(CatalogPriceCurrent)) == 0


def test_repository_flow_works_on_migrated_schema_with_compatibility_triggers(
    migrated_session_factory: sessionmaker[Session],
) -> None:
    fixture = _build_source(migrated_session_factory)
    observed_at = fixture.observed_at + timedelta(minutes=5)

    with migrated_session_factory.begin() as session:
        price_repository = CatalogPriceRepository(session)
        first = price_repository.record(
            _observation(
                fixture,
                observed_at=observed_at,
                current_price="79.00",
                source_hash="a" * 64,
            )
        )
        correction = price_repository.record(
            _observation(
                fixture,
                observed_at=observed_at,
                current_price="78.00",
                source_hash="d" * 64,
                supersedes_observation_id=first.observation_id,
            )
        )

        catalog = GeneralCatalogRepository(session)
        brand = catalog.get_or_create_brand(code="MATCH", name_zh="匹配测试")
        category = catalog.get_or_create_category(
            code="MATCH_FRUIT",
            name_zh="匹配水果",
            level=1,
            path="/FOOD/MATCH_FRUIT",
            is_leaf=True,
            default_measure_type=MeasureType.WEIGHT,
            attribute_profile_code="match-fruit",
            attribute_profile_version="1",
        )
        item = catalog.get_or_create_item(
            category_id=category.id,
            brand_id=brand.id,
            canonical_key="match-apple",
            item_type=ItemType.COMMODITY,
            name="匹配苹果",
            record_origin=RecordOrigin.MANUAL,
        )
        first_variant = catalog.get_or_create_variant(
            catalog_item_id=item.id,
            variant_key="match-apple-a",
            name="匹配苹果 A",
            condition_code=ConditionCode.NEW,
            measure_type=MeasureType.WEIGHT,
            quantity_value=Decimal("5"),
            base_unit="KG",
            identity_fingerprint="b" * 64,
        )
        second_variant = catalog.get_or_create_variant(
            catalog_item_id=item.id,
            variant_key="match-apple-b",
            name="匹配苹果 B",
            condition_code=ConditionCode.NEW,
            measure_type=MeasureType.WEIGHT,
            quantity_value=Decimal("5"),
            base_unit="KG",
            identity_fingerprint="c" * 64,
        )
        first_match = catalog.create_match(
            listing_revision_id=fixture.revision_id,
            item_variant_id=first_variant.id,
            match_status=MatchStatus.ACCEPTED,
            match_method=MatchMethod.MANUAL,
            confidence=Decimal("1.0000"),
            matcher_version="manual-1",
            effective_from=observed_at,
        )
        second_match = catalog.create_match(
            listing_revision_id=fixture.revision_id,
            item_variant_id=second_variant.id,
            match_status=MatchStatus.ACCEPTED,
            match_method=MatchMethod.MANUAL,
            confidence=Decimal("1.0000"),
            matcher_version="manual-2",
            effective_from=observed_at + timedelta(minutes=1),
        )
        assert correction.current_advanced is True
        assert first_match.effective_to == observed_at + timedelta(minutes=1)
        assert second_match.effective_to is None

    with migrated_session_factory() as session:
        assert session.scalar(select(func.count()).select_from(CatalogPriceObservationRecord)) == 2
        assert session.scalar(select(func.count()).select_from(CatalogPriceCurrent)) == 1
        assert (
            session.scalar(
                select(func.count())
                .select_from(ListingMatch)
                .where(
                    ListingMatch.match_status == MatchStatus.ACCEPTED.value,
                    ListingMatch.effective_to.is_(None),
                )
            )
            == 1
        )
