from datetime import datetime
from decimal import Decimal

import pytest

from device_price_service.domain.catalog_crawl import (
    CatalogCollectionRequest,
    CatalogRegion,
    DiscoveredCatalogListing,
    DiscoveredCatalogProduct,
    ParsedCatalogListing,
    ParsedCatalogProduct,
    ParsedCatalogRow,
    SourceMerchant,
    SourcePriceCandidate,
)
from device_price_service.domain.catalog_enums import (
    Availability,
    FeeStatus,
    PriceNature,
    PriceType,
    PricingBasis,
    QualityStatus,
    RegionScope,
    RunStatus,
    SellerType,
    VerificationStatus,
)
from device_price_service.normalization.devices import (
    DeviceCategoryRule,
    DeviceSpecification,
    device_listing_key,
)
from device_price_service.services.catalog_preparation import (
    CatalogPreparationError,
    prepare_catalog_product,
    prepare_catalog_rows,
)

URL = "https://www.apple.com.cn/shop/buy-iphone/fixture"
REQUEST = CatalogCollectionRequest(
    region_scope=RegionScope.NATIONAL, region_code="CN", category_codes=("PHONE",)
)
DISCOVERED = DiscoveredCatalogProduct(external_product_id="fixture", category_code="PHONE", url=URL)


def _candidate(**changes: object) -> SourcePriceCandidate:
    values: dict[str, object] = {
        "current_price": Decimal("5999"),
        "price_type": PriceType.DIRECT_UNCONDITIONAL,
        "pricing_basis": PricingBasis.PACKAGE_TOTAL,
        "availability": Availability.ON_SALE,
        "fee_status": FeeStatus.ITEM_ONLY,
    }
    values.update(changes)
    return SourcePriceCandidate(**values)


def _row(sku: str = "fixture-sku", **changes: object) -> ParsedCatalogRow:
    specification = DeviceSpecification(color="黑色", capacity="256GB")
    item = DiscoveredCatalogListing(
        listing_key=device_listing_key(
            product_id="fixture", sku_id=sku, specification=specification
        ),
        external_product_id="fixture",
        external_sku_id=sku,
        category_code="PHONE",
        url=URL,
        price_nature=PriceNature.RETAIL_OFFER,
        merchant=SourceMerchant(
            merchant_key="official",
            name="官方直营",
            seller_type=SellerType.BRAND_OFFICIAL,
            verification_status=VerificationStatus.VERIFIED,
        ),
    )
    parsed = ParsedCatalogListing(
        source_title="Fixture phone",
        source_attributes={"device_specification": specification.model_dump(mode="json")},
        price_candidates=[_candidate()],
    )
    return ParsedCatalogRow(item=item, parsed=parsed.model_copy(update=changes))


def _product(rows: list[ParsedCatalogRow]) -> ParsedCatalogProduct:
    return ParsedCatalogProduct(
        external_product_id="fixture",
        category_code="PHONE",
        brand_code="APPLE",
        name="Fixture phone",
        rows=rows,
    )


def _prepare(rows: list[ParsedCatalogRow], **changes: object):
    values = {
        "discovered": DISCOVERED,
        "expected_brand_code": "APPLE",
        "request": REQUEST,
        "allowed_domains": ("www.apple.com.cn",),
        "rule_for_category": lambda _: DeviceCategoryRule(),
    }
    values.update(changes)
    return prepare_catalog_product(_product(rows), **values)


def test_extra_conditional_candidate_does_not_fail_an_accepted_sku() -> None:
    prepared = _prepare(
        [_row(price_candidates=[_candidate(), _candidate(price_type=PriceType.COUPON)])]
    )

    assert prepared.status is RunStatus.SUCCEEDED
    assert prepared.complete
    assert prepared.accepted_count == 1
    assert prepared.rejected_count == 1
    assert prepared.error_code is None


def test_all_conditional_candidates_fail_without_a_fetch_failure() -> None:
    prepared = _prepare([_row(price_candidates=[_candidate(price_type=PriceType.COUPON)])])

    assert prepared.status is RunStatus.FAILED
    assert not prepared.complete
    assert prepared.accepted_count == 0
    assert prepared.rejected_count == 1
    assert prepared.error_code == "CONDITIONAL_PRICE"


def test_missing_trusted_price_in_one_sku_is_partial() -> None:
    prepared = _prepare(
        [
            _row("first"),
            _row("second", price_candidates=[_candidate(price_type=PriceType.MEMBER)]),
        ]
    )

    assert prepared.status is RunStatus.PARTIAL
    assert prepared.accepted_count == prepared.rejected_count == 1


def test_one_untrusted_configuration_blocks_trusted_product_price_advancement() -> None:
    prepared = _prepare([_row("first"), _row("second", source_attributes={})])

    assert prepared.status is RunStatus.FAILED
    assert prepared.accepted_count == 0
    assert prepared.review_count == 2
    assert all(
        candidate.quality_status is not QualityStatus.ACCEPTED
        for row in prepared.rows
        for candidate in row.evaluated
    )


def test_multi_region_requires_a_trusted_candidate_in_each_reported_region() -> None:
    row = _row(
        price_candidates=[
            _candidate(region=CatalogRegion(scope=RegionScope.CITY, code="310100")),
            _candidate(
                region=CatalogRegion(scope=RegionScope.CITY, code="110100"),
                price_type=PriceType.COUPON,
            ),
        ]
    )
    prepared = prepare_catalog_rows(
        [row],
        request=CatalogCollectionRequest(
            region_scope=RegionScope.MULTI, region_code="*", category_codes=("PHONE",)
        ),
        allowed_domains=("www.apple.com.cn",),
        rule_for_category=lambda _: DeviceCategoryRule(),
    )

    assert prepared.status is RunStatus.PARTIAL
    assert not prepared.complete
    assert prepared.accepted_count == prepared.rejected_count == 1


def test_empty_rows_are_not_a_success() -> None:
    prepared = prepare_catalog_rows(
        [],
        request=REQUEST,
        allowed_domains=("www.apple.com.cn",),
        rule_for_category=lambda _: DeviceCategoryRule(),
    )

    assert prepared.status is RunStatus.FAILED
    assert prepared.error_code == "NO_ACCEPTED_PRICE"


@pytest.mark.parametrize(
    ("override", "error_code"),
    [
        ({"region": CatalogRegion(scope=RegionScope.CITY, code="310100")}, "REGION_OUT_OF_SCOPE"),
        ({"source_observed_at": datetime(2026, 9, 11, 8)}, "DEVICE_EVIDENCE_OVERRIDE"),
        ({"evidence_hash": "a" * 64}, "DEVICE_EVIDENCE_OVERRIDE"),
    ],
)
def test_product_rejects_region_and_shared_evidence_overrides(
    override: dict[str, object], error_code: str
) -> None:
    with pytest.raises(CatalogPreparationError) as error:
        _prepare([_row(price_candidates=[_candidate(**override)])])

    assert error.value.error_code == error_code


def test_product_rejects_a_brand_other_than_its_registered_source() -> None:
    with pytest.raises(CatalogPreparationError) as error:
        _prepare([_row()], expected_brand_code="HUAWEI")

    assert error.value.error_code == "PRODUCT_BRAND_MISMATCH"


@pytest.mark.parametrize(
    "merchant_change",
    [
        {"seller_type": SellerType.THIRD_PARTY},
        {"verification_status": VerificationStatus.UNVERIFIED},
    ],
)
def test_product_rejects_non_official_or_unverified_sellers(merchant_change: dict) -> None:
    row = _row()
    row = row.model_copy(
        update={
            "item": row.item.model_copy(
                update={"merchant": row.item.merchant.model_copy(update=merchant_change)}
            )
        }
    )

    with pytest.raises(CatalogPreparationError) as error:
        _prepare([row])

    assert error.value.error_code == "SELLER_UNTRUSTED"


def test_each_output_listing_url_must_be_in_the_source_allowlist() -> None:
    row = _row()
    row = row.model_copy(
        update={"item": row.item.model_copy(update={"url": "https://third-party.example.test/sku"})}
    )

    with pytest.raises(ValueError, match="domain|host"):
        _prepare([row])


def test_prepared_rows_reject_duplicate_discoveries_and_out_of_scope_categories() -> None:
    with pytest.raises(CatalogPreparationError) as error:
        prepare_catalog_rows(
            [_row(), _row()],
            request=REQUEST,
            allowed_domains=("www.apple.com.cn",),
            rule_for_category=lambda _: DeviceCategoryRule(),
        )
    assert error.value.error_code == "DUPLICATE_LISTING"

    with pytest.raises(CatalogPreparationError) as error:
        _prepare([_row()], request=REQUEST.model_copy(update={"category_codes": ("TABLET",)}))
    assert error.value.error_code == "CATEGORY_OUT_OF_SCOPE"
