from datetime import datetime
from decimal import Decimal

import pytest
from pydantic import ValidationError

from device_price_service.domain.catalog_crawl import (
    CatalogRegion,
    NormalizedListingIdentity,
    SourcePriceCandidate,
)
from device_price_service.domain.catalog_enums import (
    Availability,
    FeeStatus,
    MeasureType,
    OriginalPriceType,
    PriceNature,
    PriceType,
    PricingBasis,
    QualityStatus,
    RegionScope,
)
from device_price_service.domain.catalog_models import CatalogPriceObservation
from device_price_service.validation.catalog_price_policy import CatalogPricePolicy


def _candidate(**updates: object) -> SourcePriceCandidate:
    values = {
        "price_type": PriceType.AVAILABILITY_ONLY,
        "pricing_basis": PricingBasis.UNKNOWN,
        "availability": Availability.OFF_SHELF,
        "fee_status": FeeStatus.NOT_APPLICABLE,
    }
    return SourcePriceCandidate.model_validate(values | updates)


def _evaluate(candidate: SourcePriceCandidate, *, nature: PriceNature, scope: RegionScope):
    return CatalogPricePolicy().evaluate(
        candidate,
        price_nature=nature,
        default_region=CatalogRegion(scope=scope, code="CN"),
        identity=NormalizedListingIdentity(
            measure_type=MeasureType.COUNT,
            base_quantity_value=Decimal(1),
            base_unit="PIECE",
            quality_status=QualityStatus.ACCEPTED,
        ),
    )


def _observation(candidate: SourcePriceCandidate, *, nature: PriceNature, scope: RegionScope):
    return CatalogPriceObservation(
        **candidate.model_dump(exclude={"region", "source_observed_at", "evidence_hash"}),
        source_listing_id=1,
        listing_revision_id=1,
        crawl_record_id=1,
        region_scope=scope,
        region_code="CN",
        price_nature=nature,
        quality_status=QualityStatus.ACCEPTED,
        source_hash="a" * 64,
        observed_at=datetime(2026, 9, 11, 8),
    )


@pytest.mark.parametrize(
    "availability",
    [
        Availability.OFF_SHELF,
        Availability.OUT_OF_STOCK,
        Availability.COMING_SOON,
    ],
)
def test_only_explicit_non_selling_states_are_current_eligible(availability: Availability) -> None:
    candidate = _candidate(availability=availability)
    evaluated = _evaluate(candidate, nature=PriceNature.RETAIL_OFFER, scope=RegionScope.NATIONAL)
    assert evaluated.quality_status is QualityStatus.ACCEPTED
    observation = _observation(
        candidate, nature=PriceNature.RETAIL_OFFER, scope=RegionScope.NATIONAL
    )
    assert observation.eligible_for_current
    assert observation.current_price is None
    assert observation.original_price is None
    assert observation.unit_price is None


INVALID_STATES = [
    {"availability": value}
    for value in (
        Availability.ON_SALE,
        Availability.PRE_SALE,
        Availability.UNKNOWN,
        Availability.RESERVATION,
    )
] + [
    {"current_price": Decimal("5999")},
    {"original_price": Decimal("5999"), "original_price_type": OriginalPriceType.MSRP},
    {"unit_price": Decimal("5999"), "unit_price_unit": "CNY_PER_PIECE"},
    {"pricing_basis": PricingBasis.PACKAGE_TOTAL},
    {"fee_status": FeeStatus.ITEM_ONLY},
    {"promotion_label": "限时促销"},
]


@pytest.mark.parametrize("updates", INVALID_STATES)
def test_policy_and_observation_both_refuse_invalid_accepted_states(
    updates: dict[str, object],
) -> None:
    candidate = _candidate(**updates)
    result = _evaluate(candidate, nature=PriceNature.RETAIL_OFFER, scope=RegionScope.NATIONAL)
    assert result.quality_status is QualityStatus.REJECTED
    with pytest.raises(ValidationError):
        _observation(candidate, nature=PriceNature.RETAIL_OFFER, scope=RegionScope.NATIONAL)


@pytest.mark.parametrize(
    "nature",
    [
        PriceNature.RETAIL_AVERAGE,
        PriceNature.WHOLESALE_AVERAGE,
        PriceNature.MARKET_AVERAGE,
        PriceNature.WHOLESALE_OFFER,
        PriceNature.UNKNOWN,
    ],
)
def test_status_exception_cannot_weaken_government_or_other_price_gates(
    nature: PriceNature,
) -> None:
    candidate = _candidate()
    assert (
        _evaluate(
            candidate,
            nature=nature,
            scope=RegionScope.NATIONAL,
        ).quality_status
        is QualityStatus.REJECTED
    )
    with pytest.raises(ValidationError):
        _observation(candidate, nature=nature, scope=RegionScope.NATIONAL)


def test_unknown_region_and_ordinary_priceless_quotes_remain_rejected() -> None:
    candidate = _candidate()
    assert (
        _evaluate(
            candidate,
            nature=PriceNature.RETAIL_OFFER,
            scope=RegionScope.UNKNOWN,
        ).quality_status
        is QualityStatus.REJECTED
    )
    with pytest.raises(ValidationError):
        _observation(candidate, nature=PriceNature.RETAIL_OFFER, scope=RegionScope.UNKNOWN)
    ordinary = _candidate(
        price_type=PriceType.DIRECT_UNCONDITIONAL,
        pricing_basis=PricingBasis.PACKAGE_TOTAL,
        fee_status=FeeStatus.ITEM_ONLY,
    )
    assert (
        _evaluate(
            ordinary,
            nature=PriceNature.RETAIL_OFFER,
            scope=RegionScope.NATIONAL,
        ).rejection_code
        == "CURRENT_PRICE_MISSING"
    )
    with pytest.raises(ValidationError, match="require current_price"):
        _observation(ordinary, nature=PriceNature.RETAIL_OFFER, scope=RegionScope.NATIONAL)
