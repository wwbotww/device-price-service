from decimal import Decimal

import pytest

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
from device_price_service.validation.catalog_price_policy import CatalogPricePolicy


def _evaluate(candidate: SourcePriceCandidate, nature: PriceNature = PriceNature.RETAIL_OFFER):
    return CatalogPricePolicy().evaluate(
        candidate,
        price_nature=nature,
        default_region=CatalogRegion(scope=RegionScope.NATIONAL, code="CN"),
        identity=NormalizedListingIdentity(
            measure_type=MeasureType.COUNT,
            base_quantity_value=Decimal(1),
            base_unit="PIECE",
            quality_status=QualityStatus.ACCEPTED,
        ),
    )


def _retail_candidate(original: Decimal | None, kind: OriginalPriceType) -> SourcePriceCandidate:
    return SourcePriceCandidate(
        current_price=Decimal("100"),
        original_price=original,
        original_price_type=kind,
        price_type=PriceType.DIRECT_UNCONDITIONAL,
        pricing_basis=PricingBasis.PACKAGE_TOTAL,
        availability=Availability.ON_SALE,
        fee_status=FeeStatus.ITEM_ONLY,
    )


@pytest.mark.parametrize(
    "kind",
    [OriginalPriceType.CROSSED_OUT, OriginalPriceType.EXPLICIT_ORIGINAL, OriginalPriceType.MSRP],
)
def test_retail_original_below_current_cannot_be_a_trusted_quote(kind: OriginalPriceType) -> None:
    evaluated = _evaluate(_retail_candidate(Decimal("99"), kind))
    assert evaluated.quality_status is QualityStatus.REJECTED
    assert evaluated.rejection_code == "ORIGINAL_BELOW_CURRENT"
    # Both observed values remain available for audit; neither is swapped or guessed.
    assert evaluated.current_price == Decimal("100")
    assert evaluated.original_price == Decimal("99")


@pytest.mark.parametrize("original", [None, Decimal("100"), Decimal("101")])
def test_missing_equal_or_higher_retail_original_is_not_rejected(original: Decimal | None) -> None:
    kind = OriginalPriceType.NONE if original is None else OriginalPriceType.EXPLICIT_ORIGINAL
    evaluated = _evaluate(_retail_candidate(original, kind))
    assert evaluated.quality_status is QualityStatus.ACCEPTED
    assert evaluated.rejection_code is None


@pytest.mark.parametrize("nature", [PriceNature.RETAIL_AVERAGE, PriceNature.WHOLESALE_AVERAGE])
def test_government_published_values_keep_their_existing_policy(nature: PriceNature) -> None:
    evaluated = _evaluate(
        SourcePriceCandidate(
            current_price=Decimal("12.50"),
            price_type=PriceType.PUBLISHED_VALUE,
            pricing_basis=PricingBasis.UNIT_QUOTED,
            availability=Availability.UNKNOWN,
            fee_status=FeeStatus.NOT_APPLICABLE,
        ),
        nature,
    )
    assert evaluated.quality_status is QualityStatus.ACCEPTED
    assert evaluated.original_price is None
