from datetime import UTC, datetime
from decimal import Decimal

import pytest
from pydantic import ValidationError

from device_price_service.domain.enums import Availability, OriginalPriceType
from device_price_service.domain.models import PriceObservation


def test_price_observation_normalizes_currency_hash_and_time() -> None:
    observation = PriceObservation(
        offer_id=1,
        crawl_run_id=2,
        currency="cny",
        original_price=Decimal("9999.00"),
        original_price_type=OriginalPriceType.MSRP,
        current_price=Decimal("8999.00"),
        availability=Availability.ON_SALE,
        observed_at=datetime(2026, 8, 5, 8, tzinfo=UTC),
        source_hash="A" * 64,
    )

    assert observation.currency == "CNY"
    assert observation.source_hash == "a" * 64
    assert observation.observed_at == datetime(2026, 8, 5, 8)


@pytest.mark.parametrize("field", ["original_price", "current_price"])
def test_price_observation_rejects_non_positive_prices(field: str) -> None:
    values = {
        "offer_id": 1,
        "crawl_run_id": 2,
        "original_price_type": OriginalPriceType.NONE,
        "availability": Availability.ON_SALE,
        "source_hash": "a" * 64,
        field: Decimal("0"),
    }
    with pytest.raises(ValidationError, match="greater than zero"):
        PriceObservation(**values)


def test_price_observation_requires_original_price_type() -> None:
    with pytest.raises(ValidationError, match="must describe"):
        PriceObservation(
            offer_id=1,
            crawl_run_id=2,
            original_price=Decimal("9999"),
            original_price_type=OriginalPriceType.NONE,
            current_price=Decimal("8999"),
            availability=Availability.ON_SALE,
            source_hash="b" * 64,
        )


def test_price_observation_allows_unpriced_reservation() -> None:
    observation = PriceObservation(
        offer_id=1,
        crawl_run_id=2,
        availability=Availability.RESERVATION,
        source_hash="c" * 64,
    )
    assert observation.current_price is None
    assert observation.original_price is None
