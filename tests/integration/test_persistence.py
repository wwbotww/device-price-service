from __future__ import annotations

from datetime import datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from device_price_service.db.models import (
    Brand,
    Category,
    OfficialOffer,
    PriceCurrent,
    PriceHistory,
    Product,
    SalesChannel,
    Sku,
)
from device_price_service.db.repositories import (
    CatalogRepository,
    CrawlRunRepository,
    OutOfOrderObservationError,
    PriceRepository,
)
from device_price_service.db.seed import seed_reference_data
from device_price_service.domain.enums import (
    Availability,
    OriginalPriceType,
    RunType,
    TriggerType,
)
from device_price_service.domain.models import PriceObservation

pytestmark = pytest.mark.integration


def _build_offer(factory: sessionmaker[Session], observed_at: datetime) -> tuple[int, int]:
    with factory.begin() as session:
        seed_reference_data(session)
        catalog = CatalogRepository(session)
        apple = catalog.require_brand("APPLE")
        phone = catalog.require_category("PHONE")
        channel = catalog.require_channel("APPLE_CN_WEB")
        product = catalog.upsert_product(
            brand_id=apple.id,
            category_id=phone.id,
            official_product_id="iphone-test",
            name="iPhone Test",
            official_url="https://www.apple.com.cn/shop/buy-iphone/iphone-test",
            observed_at=observed_at,
        )
        sku = catalog.upsert_sku(
            product_id=product.id,
            official_sku_id="iphone-test-256-black",
            name="iPhone Test 256GB 黑色",
            color="黑色",
            capacity="256GB",
            attributes={"capacity": "256GB", "color": "黑色"},
            spec_fingerprint="1" * 64,
            observed_at=observed_at,
        )
        offer = catalog.upsert_offer(
            sku_id=sku.id,
            channel_id=channel.id,
            official_offer_id="offer-test-1",
            source_url="https://www.apple.com.cn/shop/buy-iphone/iphone-test",
            availability=Availability.ON_SALE.value,
            observed_at=observed_at,
        )
        run = CrawlRunRepository(session).start(
            channel_id=channel.id,
            run_type=RunType.FULL,
            trigger_type=TriggerType.MANUAL,
            adapter_version="test",
            started_at=observed_at,
        )
        return offer.id, run.id


def _observation(
    *,
    offer_id: int,
    run_id: int,
    observed_at: datetime,
    current_price: str,
    source_character: str,
    availability: Availability = Availability.ON_SALE,
) -> PriceObservation:
    return PriceObservation(
        offer_id=offer_id,
        crawl_run_id=run_id,
        original_price=Decimal("9999.00"),
        original_price_type=OriginalPriceType.MSRP,
        current_price=Decimal(current_price),
        availability=availability,
        observed_at=observed_at,
        source_hash=source_character * 64,
    )


def test_seed_and_catalog_upserts_are_idempotent(
    session_factory: sessionmaker[Session],
) -> None:
    observed_at = datetime(2026, 8, 5, 8)
    first_offer_id, _ = _build_offer(session_factory, observed_at)
    second_offer_id, _ = _build_offer(session_factory, observed_at + timedelta(minutes=1))

    with session_factory() as session:
        assert session.scalar(select(func.count()).select_from(Brand)) == 5
        assert session.scalar(select(func.count()).select_from(Category)) == 6
        assert session.scalar(select(func.count()).select_from(SalesChannel)) == 5
        assert session.scalar(select(func.count()).select_from(Product)) == 1
        assert session.scalar(select(func.count()).select_from(Sku)) == 1
        assert session.scalar(select(func.count()).select_from(OfficialOffer)) == 1
        assert first_offer_id == second_offer_id


def test_price_history_changes_only_when_state_changes(
    session_factory: sessionmaker[Session],
) -> None:
    t1 = datetime(2026, 8, 5, 8)
    t2 = t1 + timedelta(minutes=30)
    t3 = t2 + timedelta(minutes=30)
    offer_id, run_id = _build_offer(session_factory, t1)

    with session_factory.begin() as session:
        repository = PriceRepository(session)
        repository.record(
            _observation(
                offer_id=offer_id,
                run_id=run_id,
                observed_at=t1,
                current_price="8999.00",
                source_character="a",
            )
        )
        repository.record(
            _observation(
                offer_id=offer_id,
                run_id=run_id,
                observed_at=t2,
                current_price="8999.00",
                source_character="b",
            )
        )
        repository.record(
            _observation(
                offer_id=offer_id,
                run_id=run_id,
                observed_at=t3,
                current_price="8499.00",
                source_character="c",
            )
        )

    with session_factory() as session:
        current = session.scalar(select(PriceCurrent).where(PriceCurrent.offer_id == offer_id))
        assert current is not None
        assert current.current_price == Decimal("8499.00")
        assert current.observed_at == t3

        history = session.scalars(
            select(PriceHistory)
            .where(PriceHistory.offer_id == offer_id)
            .order_by(PriceHistory.valid_from)
        ).all()
        assert len(history) == 2
        assert history[0].valid_from == t1
        assert history[0].last_observed_at == t2
        assert history[0].valid_to == t3
        assert history[1].valid_from == t3
        assert history[1].valid_to is None


def test_availability_change_creates_history_without_changing_price(
    session_factory: sessionmaker[Session],
) -> None:
    t1 = datetime(2026, 8, 5, 8)
    t2 = t1 + timedelta(minutes=30)
    offer_id, run_id = _build_offer(session_factory, t1)

    with session_factory.begin() as session:
        repository = PriceRepository(session)
        repository.record(
            _observation(
                offer_id=offer_id,
                run_id=run_id,
                observed_at=t1,
                current_price="8999.00",
                source_character="d",
            )
        )
        repository.record(
            _observation(
                offer_id=offer_id,
                run_id=run_id,
                observed_at=t2,
                current_price="8999.00",
                source_character="e",
                availability=Availability.OUT_OF_STOCK,
            )
        )

    with session_factory() as session:
        history = session.scalars(
            select(PriceHistory)
            .where(PriceHistory.offer_id == offer_id)
            .order_by(PriceHistory.valid_from)
        ).all()
        assert [item.availability for item in history] == ["ON_SALE", "OUT_OF_STOCK"]


def test_out_of_order_observation_rolls_back(
    session_factory: sessionmaker[Session],
) -> None:
    current_time = datetime(2026, 8, 5, 9)
    old_time = current_time - timedelta(minutes=30)
    offer_id, run_id = _build_offer(session_factory, old_time)

    with session_factory.begin() as session:
        PriceRepository(session).record(
            _observation(
                offer_id=offer_id,
                run_id=run_id,
                observed_at=current_time,
                current_price="8999.00",
                source_character="f",
            )
        )

    with pytest.raises(OutOfOrderObservationError), session_factory.begin() as session:
        PriceRepository(session).record(
            _observation(
                offer_id=offer_id,
                run_id=run_id,
                observed_at=old_time,
                current_price="7999.00",
                source_character="0",
            )
        )

    with session_factory() as session:
        current = session.scalar(select(PriceCurrent).where(PriceCurrent.offer_id == offer_id))
        assert current is not None
        assert current.current_price == Decimal("8999.00")
        assert session.scalar(select(func.count()).select_from(PriceHistory)) == 1


def test_exception_rolls_back_first_price_write(
    session_factory: sessionmaker[Session],
) -> None:
    observed_at = datetime(2026, 8, 5, 8)
    offer_id, run_id = _build_offer(session_factory, observed_at)

    with pytest.raises(RuntimeError, match="force rollback"), session_factory.begin() as session:
        PriceRepository(session).record(
            _observation(
                offer_id=offer_id,
                run_id=run_id,
                observed_at=observed_at,
                current_price="8999.00",
                source_character="1",
            )
        )
        raise RuntimeError("force rollback")

    with session_factory() as session:
        assert session.scalar(select(func.count()).select_from(PriceCurrent)) == 0
        assert session.scalar(select(func.count()).select_from(PriceHistory)) == 0
