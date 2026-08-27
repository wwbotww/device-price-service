from datetime import UTC, datetime
from decimal import Decimal

import pytest
from pydantic import ValidationError

from device_price_service.domain.catalog_crawl import (
    CatalogCollectionRequest,
    CatalogRegion,
    DiscoveredCatalogListing,
    NormalizedListingIdentity,
    ParsedCatalogListing,
    SourceMerchant,
    SourcePriceCandidate,
)
from device_price_service.domain.catalog_enums import (
    Availability,
    ConditionCode,
    FeeStatus,
    MeasureType,
    OriginalPriceType,
    PriceNature,
    PriceType,
    PricingBasis,
    QualityStatus,
    RegionScope,
    SellerType,
    VerificationStatus,
)
from device_price_service.normalization.catalog_rules import CategoryRule, CategoryRuleRegistry
from device_price_service.validation.catalog_price_policy import CatalogPricePolicy


class FixtureRule(CategoryRule):
    profile_code = "fixture-weight"
    version = "1"

    def normalize(
        self,
        item: DiscoveredCatalogListing,
        parsed: ParsedCatalogListing,
    ) -> NormalizedListingIdentity:
        return _identity()


def _identity(**overrides: object) -> NormalizedListingIdentity:
    values: dict[str, object] = {
        "normalized_attributes": {"grade": "A", "origin": "陕西"},
        "condition_code": ConditionCode.NEW,
        "measure_type": MeasureType.WEIGHT,
        "quantity_value": Decimal("5"),
        "quantity_unit": "KG",
        "base_quantity_value": Decimal("5"),
        "base_unit": "KG",
        "package_count": 1,
        "quality_status": QualityStatus.ACCEPTED,
    }
    values.update(overrides)
    return NormalizedListingIdentity(**values)


def _candidate(**overrides: object) -> SourcePriceCandidate:
    values: dict[str, object] = {
        "current_price": Decimal("79.00"),
        "original_price": Decimal("99.00"),
        "original_price_type": OriginalPriceType.EXPLICIT_ORIGINAL,
        "price_type": PriceType.DIRECT_UNCONDITIONAL,
        "pricing_basis": PricingBasis.PACKAGE_TOTAL,
        "availability": Availability.ON_SALE,
        "fee_status": FeeStatus.ITEM_ONLY,
    }
    values.update(overrides)
    return SourcePriceCandidate(**values)


def test_identity_fingerprint_is_canonical_and_changes_with_comparable_fields() -> None:
    first = _identity(normalized_attributes={"origin": "陕西", "grade": "A"})
    reordered = _identity(normalized_attributes={"grade": "A", "origin": "陕西"})
    changed = _identity(quantity_value=Decimal("10"), base_quantity_value=Decimal("10"))

    assert first.build_fingerprint() == reordered.build_fingerprint()
    assert first.build_fingerprint() != changed.build_fingerprint()


def test_identity_rejects_incomplete_quantity_and_quality_pairs() -> None:
    with pytest.raises(ValidationError, match="provided together"):
        _identity(quantity_unit=None)
    with pytest.raises(ValidationError, match="requires a rejection_code"):
        _identity(quality_status=QualityStatus.REVIEW_REQUIRED)


def test_price_policy_accepts_direct_price_and_derives_exact_unit_price() -> None:
    evaluated = CatalogPricePolicy().evaluate(
        _candidate(),
        price_nature=PriceNature.RETAIL_OFFER,
        default_region=CatalogRegion(scope=RegionScope.CITY, code="310100"),
        identity=_identity(),
    )

    assert evaluated.quality_status is QualityStatus.ACCEPTED
    assert evaluated.rejection_code is None
    assert evaluated.unit_price == Decimal("15.800000")
    assert evaluated.unit_price_unit == "CNY_PER_KG"


def test_price_policy_accepts_published_retail_average_and_preserves_source_time() -> None:
    evaluated = CatalogPricePolicy().evaluate(
        _candidate(
            original_price=None,
            original_price_type=OriginalPriceType.NONE,
            current_price=Decimal("6.22"),
            price_type=PriceType.PUBLISHED_VALUE,
            pricing_basis=PricingBasis.UNIT_QUOTED,
            fee_status=FeeStatus.NOT_APPLICABLE,
            source_observed_at=datetime(2026, 8, 19, tzinfo=UTC),
            evidence_hash="A" * 64,
        ),
        price_nature=PriceNature.RETAIL_AVERAGE,
        default_region=CatalogRegion(scope=RegionScope.CITY, code="310100"),
        identity=_identity(
            quantity_value=Decimal("500"),
            quantity_unit="G",
            base_quantity_value=Decimal("0.5"),
        ),
    )

    assert evaluated.quality_status is QualityStatus.ACCEPTED
    assert evaluated.unit_price == Decimal("12.440000")
    assert evaluated.source_observed_at == datetime(2026, 8, 19)
    assert evaluated.evidence_hash == "a" * 64


def test_price_candidate_rejects_invalid_evidence_hash() -> None:
    with pytest.raises(ValidationError, match="SHA-256"):
        _candidate(evidence_hash="g" * 64)


def test_price_policy_rejects_connector_unit_price_that_conflicts_with_quantity() -> None:
    evaluated = CatalogPricePolicy().evaluate(
        _candidate(unit_price=Decimal("12"), unit_price_unit="CNY_PER_KG"),
        price_nature=PriceNature.RETAIL_OFFER,
        default_region=CatalogRegion(scope=RegionScope.CITY, code="310100"),
        identity=_identity(),
    )

    assert evaluated.quality_status is QualityStatus.REJECTED
    assert evaluated.rejection_code == "UNIT_PRICE_MISMATCH"


@pytest.mark.parametrize(
    ("candidate", "region", "code"),
    [
        (
            _candidate(price_type=PriceType.COUPON),
            CatalogRegion(scope=RegionScope.CITY, code="310100"),
            "CONDITIONAL_PRICE",
        ),
        (
            _candidate(fee_status=FeeStatus.INSEPARABLE),
            CatalogRegion(scope=RegionScope.CITY, code="310100"),
            "FEE_SEMANTICS_UNTRUSTED",
        ),
        (
            _candidate(pricing_basis=PricingBasis.VARIABLE_ESTIMATE),
            CatalogRegion(scope=RegionScope.CITY, code="310100"),
            "PRICING_BASIS_UNTRUSTED",
        ),
        (_candidate(), None, "REGION_UNKNOWN"),
    ],
)
def test_price_policy_retains_but_rejects_untrusted_candidates(
    candidate: SourcePriceCandidate,
    region: CatalogRegion | None,
    code: str,
) -> None:
    evaluated = CatalogPricePolicy().evaluate(
        candidate,
        price_nature=PriceNature.RETAIL_OFFER,
        default_region=region,
        identity=_identity(),
    )

    assert evaluated.quality_status is QualityStatus.REJECTED
    assert evaluated.rejection_code == code


def test_category_rule_registry_requires_exact_profile_version() -> None:
    registry = CategoryRuleRegistry()
    rule = FixtureRule()
    registry.register(rule)

    assert registry.get("FIXTURE-WEIGHT", "1") is rule
    with pytest.raises(ValueError, match="not registered"):
        registry.get("fixture-weight", "2")
    with pytest.raises(ValueError, match="already registered"):
        registry.register(FixtureRule())


def test_collection_request_has_explicit_multi_region_semantics() -> None:
    request = CatalogCollectionRequest(
        region_scope=RegionScope.MULTI,
        region_code="*",
        category_codes=(" fresh_fruit ", "FRESH_FRUIT"),
        source_item_codes=(" cucumber ", "CUCUMBER"),
    )

    assert request.default_region is None
    assert request.category_codes == ("FRESH_FRUIT",)
    assert request.source_item_codes == ("CUCUMBER",)
    with pytest.raises(ValidationError, match="MULTI runs"):
        CatalogCollectionRequest(region_scope=RegionScope.MULTI, region_code="CN")


def test_discovery_identity_includes_merchant_listing_and_price_nature() -> None:
    merchant = SourceMerchant(
        merchant_key="platform-self",
        name="平台自营",
        seller_type=SellerType.PLATFORM_SELF,
        verification_status=VerificationStatus.VERIFIED,
    )
    item = DiscoveredCatalogListing(
        listing_key="product-1:sku-5kg:retail",
        url="https://example.test/product-1",
        category_code="fresh_fruit",
        merchant=merchant,
        price_nature=PriceNature.RETAIL_OFFER,
    )

    assert item.category_code == "FRESH_FRUIT"
    assert item.discovery_key == (
        "platform-self",
        "product-1:sku-5kg:retail",
        "RETAIL_OFFER",
    )
