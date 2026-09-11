from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import Engine, func, inspect, select, text, update
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session, sessionmaker
from typer.testing import CliRunner

import device_price_service.cli as cli
from device_price_service.cli import V1_TABLES, V2_TABLES
from device_price_service.db.base import Base
from device_price_service.db.catalog_models import (
    CatalogBrand,
    CatalogCrawlRecord,
    CatalogItem,
    CatalogPriceCurrent,
    CatalogPriceObservationRecord,
    ItemVariant,
    ListingMatch,
    SourceChannel,
    TaxonomyCategory,
)
from device_price_service.db.catalog_repositories import (
    CatalogCrawlRepository,
    CatalogPriceRepository,
    GeneralCatalogRepository,
)
from device_price_service.db.catalog_seed import seed_fresh_categories, seed_shanghai_fresh_source
from device_price_service.db.device_seed import seed_device_catalog
from device_price_service.db.session import create_session_factory
from device_price_service.domain.catalog_crawl import NormalizedListingIdentity
from device_price_service.domain.catalog_enums import (
    Availability,
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
    PriceNature,
    PriceType,
    PricingBasis,
    QualityStatus,
    RecordOrigin,
    RegionScope,
    RunType,
    SellerType,
    VerificationStatus,
)
from device_price_service.domain.catalog_models import CatalogPriceObservation
from device_price_service.normalization.devices import DeviceSpecification, device_item_key

pytestmark = pytest.mark.integration
OBSERVED_AT = datetime(2026, 9, 11, 8)


@dataclass(frozen=True)
class DeviceFixture:
    listing_id: int
    revision_id: int
    record_id: int


def _source(factory: sessionmaker[Session]) -> DeviceFixture:
    with factory.begin() as session:
        channel = seed_device_catalog(session)[0]
        catalog = GeneralCatalogRepository(session)
        brand = session.scalar(select(CatalogBrand).where(CatalogBrand.code == "APPLE"))
        category = session.scalar(select(TaxonomyCategory).where(TaxonomyCategory.code == "PHONE"))
        assert brand is not None and category is not None
        item = catalog.get_or_create_item(
            category_id=category.id,
            brand_id=brand.id,
            canonical_key=device_item_key(
                brand_code=brand.code,
                channel_code=channel.code,
                product_id="test-phone",
            ),
            item_type=ItemType.MODEL,
            name="Fixture phone",
            record_origin=RecordOrigin.RULE,
        )
        specification = DeviceSpecification(
            color="黑色",
            capacity="256GB",
            manufacturer_part_number="TESTPART",
        )
        identity = NormalizedListingIdentity(
            normalized_attributes=specification.identity_attributes(),
            measure_type=MeasureType.COUNT,
            quantity_value=Decimal(1),
            quantity_unit="PIECE",
            base_quantity_value=Decimal(1),
            base_unit="PIECE",
            package_count=1,
            quality_status=QualityStatus.ACCEPTED,
        )
        variant = catalog.get_or_create_variant(
            catalog_item_id=item.id,
            variant_key=identity.build_fingerprint(),
            name="黑色 256GB",
            condition_code=ConditionCode.NEW,
            measure_type=MeasureType.COUNT,
            base_unit="PIECE",
            quantity_value=Decimal(1),
            package_count=1,
            attributes=identity.normalized_attributes,
            identity_fingerprint=identity.build_fingerprint(),
            manufacturer_part_number=specification.manufacturer_part_number,
        )
        merchant = catalog.get_or_create_merchant(
            source_channel_id=channel.id,
            merchant_key_hash="a" * 64,
            name="官方直营",
            seller_type=SellerType.BRAND_OFFICIAL,
            verification_status=VerificationStatus.VERIFIED,
            observed_at=OBSERVED_AT,
        )
        listing = catalog.get_or_create_source_listing(
            source_channel_id=channel.id,
            merchant_id=merchant.id,
            listing_key="test-phone:TESTPART",
            listing_key_hash="b" * 64,
            price_nature=PriceNature.RETAIL_OFFER,
            canonical_url="https://www.apple.com.cn/shop/product/TESTPART",
            url_hash="c" * 64,
            external_product_id="test-phone",
            external_sku_id="TESTPART",
            observed_at=OBSERVED_AT,
        )
        crawl = CatalogCrawlRepository(session)
        run = crawl.start_run(
            source_channel_id=channel.id,
            region_scope=RegionScope.NATIONAL,
            region_code="CN",
            category_scope=["PHONE"],
            run_type=RunType.FULL,
            trigger_type=CollectionTriggerType.MANUAL,
            adapter_version="fixture-device",
            policy_version="fixture-policy",
            started_at=OBSERVED_AT,
        )
        record = crawl.add_record(
            crawl_run_id=run.id,
            source_listing_id=None,
            entity_type=CatalogEntityType.PRODUCT,
            entity_key="test-phone",
            request_url=listing.canonical_url,
            final_url=listing.canonical_url,
            fetch_method=CollectionFetchMethod.HTTP,
            http_status=200,
            fetch_status=OperationStatus.SUCCEEDED,
            parse_status=OperationStatus.SUCCEEDED,
            validation_status=OperationStatus.SUCCEEDED,
            raw_hash="d" * 64,
            raw_path="fixture/test-phone.json",
            duration_ms=1,
            fetched_at=OBSERVED_AT,
        )
        revision = catalog.get_or_create_listing_revision(
            source_listing_id=listing.id,
            first_crawl_record_id=record.id,
            source_title="Fixture phone",
            source_attributes=specification.model_dump(),
            normalized_attributes=identity.normalized_attributes,
            condition_code=ConditionCode.NEW,
            measure_type=MeasureType.COUNT,
            quantity_value=Decimal(1),
            quantity_unit="PIECE",
            base_quantity_value=Decimal(1),
            base_unit="PIECE",
            package_count=1,
            identity_fingerprint=identity.build_fingerprint(),
            normalizer_version="electronic-device@1",
            quality_status=QualityStatus.ACCEPTED,
            observed_at=OBSERVED_AT,
        )
        catalog.set_current_revision(
            source_listing_id=listing.id,
            revision_id=revision.id,
            observed_at=OBSERVED_AT,
        )
        match = catalog.create_match(
            listing_revision_id=revision.id,
            item_variant_id=variant.id,
            match_status=MatchStatus.ACCEPTED,
            match_method=MatchMethod.RULE,
            confidence=Decimal(1),
            matcher_version="official-device-1",
            effective_from=OBSERVED_AT,
            matched_fields=identity.normalized_attributes,
        )
        assert (
            catalog.create_match(
                listing_revision_id=revision.id,
                item_variant_id=variant.id,
                match_status=MatchStatus.ACCEPTED,
                match_method=MatchMethod.RULE,
                confidence=Decimal(1),
                matcher_version="official-device-1",
                effective_from=OBSERVED_AT,
                matched_fields=identity.normalized_attributes,
            ).id
            == match.id
        )
        return DeviceFixture(listing.id, revision.id, record.id)


def _observation(source: DeviceFixture, *, minute: int, state: bool = False):
    return CatalogPriceObservation(
        source_listing_id=source.listing_id,
        listing_revision_id=source.revision_id,
        crawl_record_id=source.record_id,
        region_scope=RegionScope.NATIONAL,
        region_code="CN",
        current_price=None if state else Decimal("5999"),
        price_nature=PriceNature.RETAIL_OFFER,
        price_type=PriceType.AVAILABILITY_ONLY if state else PriceType.DIRECT_UNCONDITIONAL,
        pricing_basis=PricingBasis.UNKNOWN if state else PricingBasis.PACKAGE_TOTAL,
        availability=Availability.OFF_SHELF if state else Availability.ON_SALE,
        fee_status=FeeStatus.NOT_APPLICABLE if state else FeeStatus.ITEM_ONLY,
        quality_status=QualityStatus.ACCEPTED,
        source_hash="d" * 64,
        observed_at=OBSERVED_AT + timedelta(minutes=minute),
    )


def test_device_seed_and_catalog_chain_work_with_no_v1_tables(
    mysql_engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    Base.metadata.drop_all(mysql_engine)
    tables = [Base.metadata.tables[name] for name in sorted(V2_TABLES)]
    Base.metadata.create_all(mysql_engine, tables=tables)
    factory = create_session_factory(mysql_engine)
    try:
        assert V1_TABLES.isdisjoint(inspect(mysql_engine).get_table_names())
        monkeypatch.setattr(cli, "create_database_engine", lambda: mysql_engine)
        result = CliRunner().invoke(cli.app, ["db", "seed-devices"])
        assert result.exit_code == 0, result.output
        assert "5 brands, 7 categories, 5 sources; enabled=0" in result.output
        _source(factory)
        _source(factory)
        with factory() as session:
            for model, count in (
                (CatalogBrand, 5),
                (TaxonomyCategory, 7),
                (SourceChannel, 5),
                (CatalogItem, 1),
                (ItemVariant, 1),
                (ListingMatch, 1),
            ):
                assert session.scalar(select(func.count()).select_from(model)) == count
            assert (
                session.scalar(
                    select(func.count())
                    .select_from(SourceChannel)
                    .where(
                        SourceChannel.enabled.is_(True),
                    )
                )
                == 0
            )
    finally:
        Base.metadata.drop_all(mysql_engine, tables=tables)


def test_device_seeding_preserves_fresh_rows_and_existing_switches(session_factory) -> None:
    with session_factory.begin() as session:
        categories = seed_fresh_categories(session)
        source = seed_shanghai_fresh_source(session, enable=True)
        fresh_before = {
            category.id: {
                col.name: getattr(category, col.name) for col in category.__table__.columns
            }
            for category in categories.values()
        }
        source_before = {col.name: getattr(source, col.name) for col in source.__table__.columns}
    with session_factory.begin() as session:
        assert all(not channel.enabled for channel in seed_device_catalog(session))
        assert all(channel.enabled for channel in seed_device_catalog(session, enable=True))
        assert all(channel.enabled for channel in seed_device_catalog(session))
    with session_factory() as session:
        for key, expected in fresh_before.items():
            category = session.get(TaxonomyCategory, key)
            assert {
                col.name: getattr(category, col.name) for col in category.__table__.columns
            } == expected
        source = session.scalar(
            select(SourceChannel).where(SourceChannel.code == "SH_FGW_FRESH_RETAIL")
        )
        assert {
            col.name: getattr(source, col.name) for col in source.__table__.columns
        } == source_before


def test_migrated_state_transitions_idempotency_order_and_rebuild(migrated_session_factory) -> None:
    source = _source(migrated_session_factory)
    with migrated_session_factory.begin() as session:
        prices = CatalogPriceRepository(session)
        first = prices.record(_observation(source, minute=0))
        state = _observation(source, minute=10, state=True)
        cleared = prices.record(state)
        replay = prices.record(CatalogPriceObservation.model_validate_json(state.model_dump_json()))
        assert cleared.current_advanced
        assert replay.observation_id == cleared.observation_id
        assert not replay.observation_created
        assert not prices.record(_observation(source, minute=5)).current_advanced
        assert prices.rebuild_current(source_listing_id=source.listing_id) == 1
        current = session.scalar(select(CatalogPriceCurrent))
        assert current.price_observation_id == cleared.observation_id
        assert (
            session.get(CatalogPriceObservationRecord, cleared.observation_id).current_price is None
        )
        assert (
            session.get(CatalogPriceObservationRecord, first.observation_id).current_price == 5999
        )
        restored = prices.record(_observation(source, minute=20))
        assert restored.current_advanced
        assert prices.rebuild_current(source_listing_id=source.listing_id) == 1
        session.expire_all()
        assert (
            session.scalar(select(CatalogPriceCurrent)).price_observation_id
            == restored.observation_id
        )


@pytest.mark.parametrize(
    "invalid",
    [
        {"current_price": Decimal("5999")},
        {"availability": "ON_SALE"},
        {"availability": "UNKNOWN"},
        {"pricing_basis": "PACKAGE_TOTAL"},
        {"fee_status": "ITEM_ONLY"},
        {"promotion_label": "coupon"},
    ],
)
def test_migrated_database_rejects_invalid_state_even_when_application_is_bypassed(
    migrated_session_factory,
    invalid: dict[str, object],
) -> None:
    source = _source(migrated_session_factory)
    with migrated_session_factory.begin() as session:
        fact = CatalogPriceRepository(session).record(_observation(source, minute=10, state=True))
        fact_id = fact.observation_id
    with pytest.raises(DBAPIError), migrated_session_factory.begin() as session:
        session.execute(
            update(CatalogPriceObservationRecord)
            .where(
                CatalogPriceObservationRecord.id == fact_id,
            )
            .values(**invalid)
        )


def test_device_migration_refuses_lossy_downgrade_before_changing_schema(
    mysql_engine: Engine,
    migrated_session_factory,
) -> None:
    source = _source(migrated_session_factory)
    with migrated_session_factory.begin() as session:
        CatalogPriceRepository(session).record(_observation(source, minute=10, state=True))
    config = Config("alembic.ini")
    config.set_main_option("sqlalchemy.url", mysql_engine.url.render_as_string(hide_password=False))
    with pytest.raises(RuntimeError, match="requires no PRODUCT"):
        command.downgrade(config, "96524222b3ec")
    with mysql_engine.connect() as connection:
        assert connection.scalar(text("SELECT version_num FROM alembic_version")) == "b72c910e4f31"
        assert connection.scalar(select(func.count()).select_from(CatalogCrawlRecord)) == 1
        assert (
            connection.scalar(select(func.count()).select_from(CatalogPriceObservationRecord)) == 1
        )


def test_incremental_migration_preserves_existing_prices_references_and_v1_rows(
    mysql_engine: Engine,
    migrated_session_factory,
) -> None:
    source = _source(migrated_session_factory)
    with migrated_session_factory.begin() as session:
        seed_fresh_categories(session)
        seed_shanghai_fresh_source(session, enable=True)
        CatalogPriceRepository(session).record(_observation(source, minute=0))
        # Express the fixture as an old-format record before testing old -> new.
        session.execute(update(CatalogCrawlRecord).values(entity_type="LISTING"))
        session.execute(
            text("INSERT INTO brand (code, name_zh, name_en) VALUES ('KEEP', '保留', 'Keep')")
        )
    config = Config("alembic.ini")
    config.set_main_option("sqlalchemy.url", mysql_engine.url.render_as_string(hide_password=False))
    command.downgrade(config, "96524222b3ec")

    def contents():
        with mysql_engine.connect() as connection:
            return {
                name: connection.execute(text(f"SELECT * FROM `{name}` ORDER BY id")).all()
                for name in sorted(V1_TABLES | V2_TABLES)
            }

    before = contents()
    command.upgrade(config, "head")
    assert contents() == before
