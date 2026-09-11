from datetime import UTC, datetime
from decimal import Decimal

import pytest
from pydantic import ValidationError

from device_price_service.domain.catalog_enums import (
    Availability,
    FeeStatus,
    OriginalPriceType,
    PriceNature,
    PriceType,
    PricingBasis,
    QualityStatus,
    RegionScope,
)
from device_price_service.domain.catalog_models import CatalogPriceObservation


def _accepted_observation(**overrides: object) -> CatalogPriceObservation:
    values: dict[str, object] = {
        "source_listing_id": 1,
        "listing_revision_id": 2,
        "crawl_record_id": 3,
        "region_scope": RegionScope.CITY,
        "region_code": "310100",
        "original_price": Decimal("99.00"),
        "original_price_type": OriginalPriceType.EXPLICIT_ORIGINAL,
        "current_price": Decimal("79.00"),
        "price_nature": PriceNature.RETAIL_OFFER,
        "price_type": PriceType.DIRECT_UNCONDITIONAL,
        "pricing_basis": PricingBasis.PACKAGE_TOTAL,
        "availability": Availability.ON_SALE,
        "unit_price": Decimal("15.800000"),
        "unit_price_unit": "cny_per_kg",
        "fee_status": FeeStatus.ITEM_ONLY,
        "quality_status": QualityStatus.ACCEPTED,
        "source_hash": "A" * 64,
        "observed_at": datetime(2026, 8, 21, 8, 0, 0, 123456, tzinfo=UTC),
    }
    values.update(overrides)
    return CatalogPriceObservation(**values)


def test_catalog_observation_normalizes_time_hash_region_and_unit() -> None:
    observation = _accepted_observation(region_code=" 310100 ")

    assert observation.source_hash == "a" * 64
    assert observation.region_code == "310100"
    assert observation.unit_price_unit == "CNY_PER_KG"
    assert observation.observed_at == datetime(2026, 8, 21, 8, 0, 0, 123000)


def test_market_average_has_distinct_accepted_semantics() -> None:
    observation = _accepted_observation(
        original_price=None,
        original_price_type=OriginalPriceType.NONE,
        current_price=Decimal("6.120000"),
        price_nature=PriceNature.MARKET_AVERAGE,
        price_type=PriceType.PUBLISHED_VALUE,
        pricing_basis=PricingBasis.UNIT_QUOTED,
        unit_price=Decimal("6.120000"),
        unit_price_unit="CNY_PER_KG",
        fee_status=FeeStatus.NOT_APPLICABLE,
    )

    assert observation.eligible_for_current is True


@pytest.mark.parametrize(
    "price_nature",
    [PriceNature.RETAIL_AVERAGE, PriceNature.WHOLESALE_AVERAGE],
)
def test_explicit_average_natures_are_current_eligible(price_nature: PriceNature) -> None:
    observation = _accepted_observation(
        original_price=None,
        original_price_type=OriginalPriceType.NONE,
        price_nature=price_nature,
        price_type=PriceType.PUBLISHED_VALUE,
        pricing_basis=PricingBasis.UNIT_QUOTED,
        fee_status=FeeStatus.NOT_APPLICABLE,
    )

    assert observation.eligible_for_current is True


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"price_type": PriceType.COUPON}, "direct and unconditional"),
        ({"fee_status": FeeStatus.INSEPARABLE}, "separable item-only fees"),
        ({"region_scope": RegionScope.UNKNOWN}, "known region"),
        ({"pricing_basis": PricingBasis.VARIABLE_ESTIMATE}, "exact pricing_basis"),
        ({"current_price": Decimal("0")}, "greater than zero"),
        ({"original_price": Decimal("0")}, "greater than zero"),
        ({"unit_price_unit": None}, "both be null or non-null"),
    ],
)
def test_untrusted_price_semantics_cannot_be_accepted(
    overrides: dict[str, object], message: str
) -> None:
    with pytest.raises(ValidationError, match=message):
        _accepted_observation(**overrides)


def test_rejected_candidate_is_retained_with_stable_reason() -> None:
    observation = _accepted_observation(
        price_type=PriceType.COUPON,
        quality_status=QualityStatus.REJECTED,
        rejection_code="CONDITIONAL_PRICE",
    )

    assert observation.eligible_for_current is False


def test_nonaccepted_candidate_requires_rejection_code() -> None:
    with pytest.raises(ValidationError, match="require a rejection_code"):
        _accepted_observation(quality_status=QualityStatus.REVIEW_REQUIRED)


def test_national_region_requires_cn() -> None:
    with pytest.raises(ValidationError, match="region_code CN"):
        _accepted_observation(region_scope=RegionScope.NATIONAL, region_code="310100")


def test_observation_key_is_stable_and_includes_price_candidate_semantics() -> None:
    observation = _accepted_observation()
    same = _accepted_observation()
    changed = _accepted_observation(availability=Availability.OUT_OF_STOCK)

    key = observation.build_observation_key(adapter_version="adapter-1", policy_version="policy-1")
    assert key == same.build_observation_key(adapter_version="adapter-1", policy_version="policy-1")
    assert key != changed.build_observation_key(
        adapter_version="adapter-1", policy_version="policy-1"
    )
    assert key != observation.build_observation_key(
        adapter_version="adapter-2", policy_version="policy-1"
    )


def test_observation_key_versions_cannot_be_blank() -> None:
    with pytest.raises(ValueError, match="cannot be blank"):
        _accepted_observation().build_observation_key(
            adapter_version=" ", policy_version="policy-1"
        )
