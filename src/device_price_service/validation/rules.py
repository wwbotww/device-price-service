from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum

from device_price_service.domain.crawl import NormalizedProduct
from device_price_service.domain.enums import Availability
from device_price_service.fetchers.url_policy import UrlPolicy, UrlPolicyError


class Severity(StrEnum):
    ERROR = "ERROR"
    WARNING = "WARNING"


@dataclass(frozen=True, slots=True)
class ValidationIssue:
    severity: Severity
    code: str
    message: str
    path: str


@dataclass(frozen=True, slots=True)
class ValidationReport:
    issues: tuple[ValidationIssue, ...]

    @property
    def is_valid(self) -> bool:
        return not any(issue.severity is Severity.ERROR for issue in self.issues)


class QualityValidator:
    def __init__(self, *, price_change_threshold: float) -> None:
        self.price_change_threshold = Decimal(str(price_change_threshold))

    def validate(
        self,
        product: NormalizedProduct,
        *,
        expected_brand_code: str,
        expected_channel_code: str,
        allowed_domains: list[str],
        previous_prices: dict[str, Decimal] | None = None,
    ) -> ValidationReport:
        issues: list[ValidationIssue] = []
        policy = UrlPolicy(allowed_domains)
        self._expect_equal(
            issues,
            actual=product.brand_code,
            expected=expected_brand_code,
            code="BRAND_MISMATCH",
            path="brand_code",
        )
        self._expect_equal(
            issues,
            actual=product.channel_code,
            expected=expected_channel_code,
            code="CHANNEL_MISMATCH",
            path="channel_code",
        )
        self._validate_url(issues, policy, product.official_url, "official_url")

        fingerprints: set[str] = set()
        official_sku_ids: set[str] = set()
        official_offer_ids: set[str] = set()
        prior = previous_prices or {}

        for sku_index, sku in enumerate(product.skus):
            sku_path = f"skus[{sku_index}]"
            if len(sku.offers) != 1:
                issues.append(
                    self._error(
                        "OFFER_COUNT_INVALID",
                        "each V1 SKU must have exactly one offer in its normalized channel",
                        sku_path,
                    )
                )
            if sku.spec_fingerprint in fingerprints:
                issues.append(
                    self._error("DUPLICATE_SKU_FINGERPRINT", "duplicate SKU fingerprint", sku_path)
                )
            fingerprints.add(sku.spec_fingerprint)
            if sku.official_sku_id:
                if sku.official_sku_id in official_sku_ids:
                    issues.append(
                        self._error(
                            "DUPLICATE_OFFICIAL_SKU_ID", "duplicate official SKU id", sku_path
                        )
                    )
                official_sku_ids.add(sku.official_sku_id)

            for offer_index, offer in enumerate(sku.offers):
                offer_path = f"{sku_path}.offers[{offer_index}]"
                self._validate_url(issues, policy, offer.source_url, f"{offer_path}.source_url")
                if offer.official_offer_id:
                    if offer.official_offer_id in official_offer_ids:
                        issues.append(
                            self._error(
                                "DUPLICATE_OFFICIAL_OFFER_ID",
                                "duplicate official offer id",
                                offer_path,
                            )
                        )
                    official_offer_ids.add(offer.official_offer_id)
                if (
                    offer.availability in {Availability.ON_SALE, Availability.PRE_SALE}
                    and offer.current_price is None
                ):
                    issues.append(
                        self._error(
                            "MISSING_DIRECT_PRICE",
                            "on-sale and pre-sale offers require a direct total price",
                            offer_path,
                        )
                    )
                if (
                    offer.original_price is not None
                    and offer.current_price is not None
                    and offer.original_price < offer.current_price
                ):
                    issues.append(
                        self._warning(
                            "ORIGINAL_BELOW_CURRENT",
                            "original price is below current price and requires review",
                            offer_path,
                        )
                    )

                previous = prior.get(sku.spec_fingerprint)
                if previous and offer.current_price:
                    ratio = abs(offer.current_price - previous) / previous
                    if ratio > self.price_change_threshold:
                        issues.append(
                            self._warning(
                                "LARGE_PRICE_CHANGE",
                                f"price change ratio {ratio:.2%} exceeds threshold",
                                offer_path,
                            )
                        )

        return ValidationReport(tuple(issues))

    @staticmethod
    def _validate_url(
        issues: list[ValidationIssue],
        policy: UrlPolicy,
        url: str,
        path: str,
    ) -> None:
        try:
            policy.validate(url)
        except UrlPolicyError as error:
            issues.append(ValidationIssue(Severity.ERROR, "URL_NOT_ALLOWED", str(error), path))

    @staticmethod
    def _expect_equal(
        issues: list[ValidationIssue],
        *,
        actual: str,
        expected: str,
        code: str,
        path: str,
    ) -> None:
        if actual != expected:
            issues.append(
                ValidationIssue(
                    Severity.ERROR,
                    code,
                    f"expected {expected}, got {actual}",
                    path,
                )
            )

    @staticmethod
    def _error(code: str, message: str, path: str) -> ValidationIssue:
        return ValidationIssue(Severity.ERROR, code, message, path)

    @staticmethod
    def _warning(code: str, message: str, path: str) -> ValidationIssue:
        return ValidationIssue(Severity.WARNING, code, message, path)
