"""Network/database-free validation shared by catalog smoke and persistence."""

from __future__ import annotations

from collections import Counter
from collections.abc import Callable, Iterable
from dataclasses import dataclass

from device_price_service.domain.catalog_crawl import (
    CatalogCollectionRequest,
    DiscoveredCatalogProduct,
    EvaluatedPriceCandidate,
    NormalizedListingIdentity,
    ParsedCatalogProduct,
    ParsedCatalogRow,
)
from device_price_service.domain.catalog_enums import (
    PriceNature,
    QualityStatus,
    RegionScope,
    RunStatus,
    SellerType,
    VerificationStatus,
)
from device_price_service.fetchers.url_policy import UrlPolicy
from device_price_service.normalization.catalog_rules import CategoryRule
from device_price_service.validation.catalog_price_policy import CatalogPricePolicy


class CatalogPreparationError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.error_code = code


@dataclass(frozen=True, slots=True)
class PreparedCatalogRow:
    row: ParsedCatalogRow
    identity: NormalizedListingIdentity
    evaluated: list[EvaluatedPriceCandidate]
    normalizer_version: str


@dataclass(frozen=True, slots=True)
class PreparedCatalogRows:
    rows: list[PreparedCatalogRow]

    @property
    def quality_counts(self) -> Counter[QualityStatus]:
        counts: Counter[QualityStatus] = Counter()
        for row in self.rows:
            if row.identity.quality_status is QualityStatus.ACCEPTED:
                counts.update(candidate.quality_status for candidate in row.evaluated)
            else:
                counts[row.identity.quality_status] += 1
        return counts

    @property
    def accepted_count(self) -> int:
        return self.quality_counts[QualityStatus.ACCEPTED]

    @property
    def review_count(self) -> int:
        return self.quality_counts[QualityStatus.REVIEW_REQUIRED]

    @property
    def rejected_count(self) -> int:
        return self.quality_counts[QualityStatus.REJECTED]

    @property
    def complete(self) -> bool:
        if not self.rows:
            return False
        for row in self.rows:
            if row.identity.quality_status is not QualityStatus.ACCEPTED:
                return False
            regions = {candidate.region for candidate in row.evaluated}
            accepted_regions = {
                candidate.region
                for candidate in row.evaluated
                if candidate.quality_status is QualityStatus.ACCEPTED
            }
            if not regions or regions != accepted_regions:
                return False
        return True

    @property
    def status(self) -> RunStatus:
        if not self.accepted_count:
            return RunStatus.FAILED
        return RunStatus.SUCCEEDED if self.complete else RunStatus.PARTIAL

    @property
    def error_code(self) -> str | None:
        if self.complete:
            return None
        return next(
            (
                code
                for row in self.rows
                for code in [
                    row.identity.rejection_code,
                    *(candidate.rejection_code for candidate in row.evaluated),
                ]
                if code
            ),
            "NO_ACCEPTED_PRICE",
        )


def prepare_catalog_rows(
    rows: list[ParsedCatalogRow],
    *,
    request: CatalogCollectionRequest,
    allowed_domains: Iterable[str],
    rule_for_category: Callable[[str], CategoryRule],
    price_policy: CatalogPricePolicy | None = None,
) -> PreparedCatalogRows:
    policy = price_policy or CatalogPricePolicy()
    urls = UrlPolicy(allowed_domains)
    prepared: list[PreparedCatalogRow] = []
    seen: set[tuple[str, str, str]] = set()
    for row in rows:
        item = row.item
        urls.validate(item.url)
        if item.discovery_key in seen:
            raise CatalogPreparationError("DUPLICATE_LISTING", "duplicate source listing identity")
        seen.add(item.discovery_key)
        if request.category_codes and item.category_code not in request.category_codes:
            raise CatalogPreparationError("CATEGORY_OUT_OF_SCOPE", "row category is out of scope")
        rule = rule_for_category(item.category_code)
        normalizer_version = f"{rule.profile_code}@{rule.version}"
        if len(normalizer_version) > 64:
            raise ValueError("normalizer version cannot exceed 64 characters")
        identity = rule.normalize(item, row.parsed)
        evaluated = [
            policy.evaluate(
                candidate,
                price_nature=item.price_nature,
                default_region=request.default_region,
                identity=identity,
            )
            for candidate in row.parsed.price_candidates
        ]
        for candidate in evaluated:
            if (
                request.region_scope is not RegionScope.MULTI
                and candidate.region != request.default_region
            ):
                raise CatalogPreparationError("REGION_OUT_OF_SCOPE", "row region is out of scope")
        prepared.append(PreparedCatalogRow(row, identity, evaluated, normalizer_version))
    return PreparedCatalogRows(prepared)


def prepare_catalog_product(
    product: ParsedCatalogProduct,
    *,
    discovered: DiscoveredCatalogProduct,
    expected_brand_code: str,
    request: CatalogCollectionRequest,
    allowed_domains: Iterable[str],
    rule_for_category: Callable[[str], CategoryRule],
    price_policy: CatalogPricePolicy | None = None,
) -> PreparedCatalogRows:
    product.validate_discovery(discovered)
    if product.brand_code != expected_brand_code.strip().upper():
        raise CatalogPreparationError("PRODUCT_BRAND_MISMATCH", "product brand differs from source")
    for row in product.rows:
        if (
            row.item.price_nature is not PriceNature.RETAIL_OFFER
            or row.item.merchant.seller_type is not SellerType.BRAND_OFFICIAL
            or row.item.merchant.verification_status is not VerificationStatus.VERIFIED
        ):
            raise CatalogPreparationError("SELLER_UNTRUSTED", "device rows require official retail")
        if any(
            candidate.source_observed_at is not None or candidate.evidence_hash is not None
            for candidate in row.parsed.price_candidates
        ):
            raise CatalogPreparationError(
                "DEVICE_EVIDENCE_OVERRIDE",
                "device facts must use their shared fetch time and hash",
            )
    prepared = prepare_catalog_rows(
        product.rows,
        request=request,
        allowed_domains=allowed_domains,
        rule_for_category=rule_for_category,
        price_policy=price_policy,
    )
    # A partial specification parse must not switch any SKU identity in this product.
    if any(row.identity.quality_status is not QualityStatus.ACCEPTED for row in prepared.rows):
        for prepared_row in prepared.rows:
            prepared_row.evaluated[:] = [
                candidate.model_copy(
                    update={
                        "quality_status": QualityStatus.REVIEW_REQUIRED,
                        "rejection_code": "PRODUCT_IDENTITY_UNTRUSTED",
                    }
                )
                if candidate.quality_status is QualityStatus.ACCEPTED
                else candidate
                for candidate in prepared_row.evaluated
            ]
    return prepared
