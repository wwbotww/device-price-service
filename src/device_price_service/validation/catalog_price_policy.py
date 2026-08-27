from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal

from device_price_service.domain.catalog_crawl import (
    CatalogRegion,
    EvaluatedPriceCandidate,
    NormalizedListingIdentity,
    SourcePriceCandidate,
)
from device_price_service.domain.catalog_enums import (
    FeeStatus,
    PriceNature,
    PriceType,
    PricingBasis,
    QualityStatus,
    RegionScope,
)


class CatalogPricePolicy:
    """Classify source prices without platform- or category-specific branches."""

    version = "catalog-price-policy-1"
    _UNIT_PRICE_SCALE = Decimal("0.000001")

    def evaluate(
        self,
        candidate: SourcePriceCandidate,
        *,
        price_nature: PriceNature,
        default_region: CatalogRegion | None,
        identity: NormalizedListingIdentity,
    ) -> EvaluatedPriceCandidate:
        region = candidate.region or default_region or CatalogRegion(
            scope=RegionScope.UNKNOWN,
            code="UNKNOWN",
        )
        unit_price = candidate.unit_price
        unit_price_unit = candidate.unit_price_unit
        expected_unit_price: Decimal | None = None
        expected_unit: str | None = None
        if (
            candidate.pricing_basis in {
                PricingBasis.PACKAGE_TOTAL,
                PricingBasis.UNIT_QUOTED,
            }
            and candidate.current_price is not None
            and identity.base_quantity_value is not None
            and identity.base_unit is not None
        ):
            expected_unit_price = (
                candidate.current_price / identity.base_quantity_value
            ).quantize(
                self._UNIT_PRICE_SCALE,
                rounding=ROUND_HALF_UP,
            )
            candidate_unit = f"CNY_PER_{identity.base_unit}"
            if len(candidate_unit) <= 16:
                expected_unit = candidate_unit
                if unit_price is None:
                    unit_price = expected_unit_price
                    unit_price_unit = expected_unit

        rejection_code = candidate.rejection_code or self._rejection_code(
            candidate,
            price_nature=price_nature,
            region=region,
            unit_price=unit_price,
            unit_price_unit=unit_price_unit,
            expected_unit_price=expected_unit_price,
            expected_unit=expected_unit,
        )
        quality_status = (
            QualityStatus.ACCEPTED if rejection_code is None else QualityStatus.REJECTED
        )
        return EvaluatedPriceCandidate(
            region=region,
            current_price=candidate.current_price,
            original_price=candidate.original_price,
            original_price_type=candidate.original_price_type,
            price_nature=price_nature,
            price_type=candidate.price_type,
            pricing_basis=candidate.pricing_basis,
            availability=candidate.availability,
            fee_status=candidate.fee_status,
            quality_status=quality_status,
            rejection_code=rejection_code,
            promotion_label=candidate.promotion_label,
            displayed_price_text=candidate.displayed_price_text,
            unit_price=unit_price,
            unit_price_unit=unit_price_unit,
            evidence_hash=candidate.evidence_hash,
            source_observed_at=candidate.source_observed_at,
        )

    @staticmethod
    def _rejection_code(
        candidate: SourcePriceCandidate,
        *,
        price_nature: PriceNature,
        region: CatalogRegion,
        unit_price: Decimal | None,
        unit_price_unit: str | None,
        expected_unit_price: Decimal | None,
        expected_unit: str | None,
    ) -> str | None:
        if candidate.current_price is None:
            return "CURRENT_PRICE_MISSING"
        if region.scope is RegionScope.UNKNOWN:
            return "REGION_UNKNOWN"
        if price_nature is PriceNature.UNKNOWN:
            return "PRICE_NATURE_UNKNOWN"
        if candidate.pricing_basis not in {
            PricingBasis.PACKAGE_TOTAL,
            PricingBasis.UNIT_QUOTED,
        }:
            return "PRICING_BASIS_UNTRUSTED"
        if candidate.pricing_basis is PricingBasis.UNIT_QUOTED and unit_price is None:
            return "UNIT_PRICE_MISSING"
        if (
            candidate.unit_price is not None
            and expected_unit_price is not None
            and (
                candidate.unit_price.quantize(
                    CatalogPricePolicy._UNIT_PRICE_SCALE,
                    rounding=ROUND_HALF_UP,
                )
                != expected_unit_price
                or (expected_unit is not None and unit_price_unit != expected_unit)
            )
        ):
            return "UNIT_PRICE_MISMATCH"

        if price_nature in {PriceNature.RETAIL_OFFER, PriceNature.WHOLESALE_OFFER}:
            if candidate.price_type is not PriceType.DIRECT_UNCONDITIONAL:
                return "CONDITIONAL_PRICE"
            if candidate.fee_status not in {
                FeeStatus.ITEM_ONLY,
                FeeStatus.SEPARATE_FEES_EXCLUDED,
            }:
                return "FEE_SEMANTICS_UNTRUSTED"
            return None

        if price_nature in {
            PriceNature.RETAIL_AVERAGE,
            PriceNature.WHOLESALE_AVERAGE,
            PriceNature.MARKET_AVERAGE,
        }:
            if candidate.price_type is not PriceType.PUBLISHED_VALUE:
                return "MARKET_VALUE_UNPUBLISHED"
            if candidate.fee_status is not FeeStatus.NOT_APPLICABLE:
                return "MARKET_FEE_SEMANTICS_INVALID"
            return None
        return "PRICE_NATURE_UNKNOWN"
