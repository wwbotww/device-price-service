from __future__ import annotations

import json
from datetime import UTC, datetime
from decimal import Decimal
from hashlib import sha256
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

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


def _utc_naive_milliseconds(value: datetime) -> datetime:
    if value.tzinfo is not None:
        value = value.astimezone(UTC).replace(tzinfo=None)
    return value.replace(microsecond=(value.microsecond // 1000) * 1000)


def _normalize_optional_text(value: str | None) -> str | None:
    if value is None:
        return None
    normalized = value.strip()
    return normalized or None


def _decimal_token(value: Decimal | None) -> str | None:
    if value is None:
        return None
    normalized = value.normalize()
    return "0" if normalized == 0 else format(normalized, "f")


def _normalize_optional_sha256(value: str | None, *, field_name: str) -> str | None:
    if value is None:
        return None
    normalized = value.lower()
    if len(normalized) != 64 or any(
        character not in "0123456789abcdef" for character in normalized
    ):
        raise ValueError(f"{field_name} must be a SHA-256 hexadecimal string")
    return normalized


class CatalogRegion(BaseModel):
    """One concrete mainland-China pricing region."""

    model_config = ConfigDict(frozen=True)

    scope: RegionScope
    code: str = Field(min_length=1, max_length=32)

    @field_validator("code")
    @classmethod
    def normalize_code(cls, value: str) -> str:
        return value.strip().upper()

    @model_validator(mode="after")
    def validate_semantics(self) -> CatalogRegion:
        if self.scope is RegionScope.MULTI:
            raise ValueError("a concrete catalog region cannot use MULTI")
        if self.scope is RegionScope.NATIONAL and self.code != "CN":
            raise ValueError("NATIONAL regions must use code CN")
        return self


class SourceMerchant(BaseModel):
    """Stable seller identity emitted by a platform connector."""

    model_config = ConfigDict(frozen=True)

    merchant_key: str = Field(min_length=1, max_length=512)
    name: str = Field(min_length=1, max_length=255)
    seller_type: SellerType
    verification_status: VerificationStatus
    external_merchant_id: str | None = Field(default=None, max_length=128)

    @field_validator("merchant_key", "name")
    @classmethod
    def strip_required_text(cls, value: str) -> str:
        return value.strip()

    @field_validator("external_merchant_id")
    @classmethod
    def strip_optional_text(cls, value: str | None) -> str | None:
        return _normalize_optional_text(value)


class DiscoveredCatalogListing(BaseModel):
    """A source listing discovered without assuming a standard-product match."""

    model_config = ConfigDict(frozen=True)

    listing_key: str = Field(min_length=1, max_length=512)
    url: str = Field(min_length=1, max_length=1024)
    category_code: str = Field(min_length=1, max_length=64)
    merchant: SourceMerchant
    price_nature: PriceNature
    external_product_id: str | None = Field(default=None, max_length=128)
    external_sku_id: str | None = Field(default=None, max_length=128)
    metadata: dict[str, Any] = Field(default_factory=dict)

    @field_validator("listing_key", "url", "category_code")
    @classmethod
    def strip_required_text(cls, value: str) -> str:
        return value.strip()

    @field_validator("category_code")
    @classmethod
    def normalize_category_code(cls, value: str) -> str:
        return value.strip().upper()

    @field_validator("external_product_id", "external_sku_id")
    @classmethod
    def strip_optional_text(cls, value: str | None) -> str | None:
        return _normalize_optional_text(value)

    @property
    def discovery_key(self) -> tuple[str, str, str]:
        return (self.merchant.merchant_key, self.listing_key, self.price_nature.value)


class SourcePriceCandidate(BaseModel):
    """Connector-normalized price semantics before central quality policy."""

    model_config = ConfigDict(frozen=True)

    current_price: Decimal | None = None
    original_price: Decimal | None = None
    original_price_type: OriginalPriceType = OriginalPriceType.NONE
    price_type: PriceType
    pricing_basis: PricingBasis
    availability: Availability
    fee_status: FeeStatus
    promotion_label: str | None = Field(default=None, max_length=128)
    displayed_price_text: str | None = Field(default=None, max_length=255)
    unit_price: Decimal | None = None
    unit_price_unit: str | None = Field(default=None, max_length=16)
    evidence_hash: str | None = Field(default=None, min_length=64, max_length=64)
    region: CatalogRegion | None = None
    source_observed_at: datetime | None = None
    rejection_code: str | None = Field(default=None, max_length=64)

    @field_validator("promotion_label", "displayed_price_text", "rejection_code")
    @classmethod
    def strip_optional_text(cls, value: str | None) -> str | None:
        return _normalize_optional_text(value)

    @field_validator("unit_price_unit")
    @classmethod
    def normalize_unit(cls, value: str | None) -> str | None:
        normalized = _normalize_optional_text(value)
        return normalized.upper() if normalized else None

    @field_validator("evidence_hash")
    @classmethod
    def normalize_evidence_hash(cls, value: str | None) -> str | None:
        return _normalize_optional_sha256(value, field_name="evidence_hash")

    @field_validator("source_observed_at")
    @classmethod
    def normalize_source_observed_at(cls, value: datetime | None) -> datetime | None:
        return _utc_naive_milliseconds(value) if value is not None else None

    @model_validator(mode="after")
    def validate_amount_pairs(self) -> SourcePriceCandidate:
        for field_name in ("current_price", "original_price", "unit_price"):
            value = getattr(self, field_name)
            if value is not None and value <= 0:
                raise ValueError(f"{field_name} must be greater than zero")
        if self.original_price is None and self.original_price_type is not OriginalPriceType.NONE:
            raise ValueError("original_price_type must be NONE when original_price is null")
        if self.original_price is not None and self.original_price_type is OriginalPriceType.NONE:
            raise ValueError("original_price_type must describe original_price")
        if (self.unit_price is None) != (self.unit_price_unit is None):
            raise ValueError("unit_price and unit_price_unit must be provided together")
        return self


class ParsedCatalogListing(BaseModel):
    """Platform-neutral parse result; category rules still own identity normalization."""

    model_config = ConfigDict(frozen=True)

    source_title: str = Field(min_length=1, max_length=512)
    source_category_path: str | None = Field(default=None, max_length=512)
    source_attributes: dict[str, Any] = Field(default_factory=dict)
    price_candidates: list[SourcePriceCandidate] = Field(min_length=1)

    @field_validator("source_title")
    @classmethod
    def strip_title(cls, value: str) -> str:
        return value.strip()

    @field_validator("source_category_path")
    @classmethod
    def strip_category_path(cls, value: str | None) -> str | None:
        return _normalize_optional_text(value)


class DiscoveredCatalogDataset(BaseModel):
    """One public document that contains multiple independently priced rows."""

    model_config = ConfigDict(frozen=True)

    dataset_key: str = Field(min_length=1, max_length=512)
    url: str = Field(min_length=1, max_length=1024)
    source_page_url: str = Field(min_length=1, max_length=1024)
    source_observed_at: datetime
    metadata: dict[str, Any] = Field(default_factory=dict)

    @field_validator("dataset_key", "url", "source_page_url")
    @classmethod
    def strip_required_text(cls, value: str) -> str:
        return value.strip()

    @field_validator("source_observed_at")
    @classmethod
    def normalize_source_observed_at(cls, value: datetime) -> datetime:
        return _utc_naive_milliseconds(value)


class ParsedCatalogDatasetRow(BaseModel):
    """One listing-shaped row parsed from a shared public data document."""

    model_config = ConfigDict(frozen=True)

    item: DiscoveredCatalogListing
    parsed: ParsedCatalogListing


class ParsedCatalogDataset(BaseModel):
    """Validated non-empty result of parsing one public data document."""

    model_config = ConfigDict(frozen=True)

    rows: list[ParsedCatalogDatasetRow] = Field(min_length=1)


class NormalizedListingIdentity(BaseModel):
    """Category-normalized fields that determine whether two prices are comparable."""

    model_config = ConfigDict(frozen=True)

    normalized_attributes: dict[str, Any] = Field(default_factory=dict)
    condition_code: ConditionCode = ConditionCode.NEW
    measure_type: MeasureType
    quantity_value: Decimal | None = None
    quantity_min: Decimal | None = None
    quantity_max: Decimal | None = None
    quantity_unit: str | None = Field(default=None, max_length=16)
    base_quantity_value: Decimal | None = None
    base_quantity_min: Decimal | None = None
    base_quantity_max: Decimal | None = None
    base_unit: str | None = Field(default=None, max_length=16)
    package_count: int | None = Field(default=None, gt=0)
    quality_status: QualityStatus
    rejection_code: str | None = Field(default=None, max_length=64)

    @field_validator("quantity_unit", "base_unit")
    @classmethod
    def normalize_unit(cls, value: str | None) -> str | None:
        normalized = _normalize_optional_text(value)
        return normalized.upper() if normalized else None

    @field_validator("rejection_code")
    @classmethod
    def strip_rejection_code(cls, value: str | None) -> str | None:
        return _normalize_optional_text(value)

    @model_validator(mode="after")
    def validate_identity(self) -> NormalizedListingIdentity:
        self._validate_quantity_group(
            self.quantity_value,
            self.quantity_min,
            self.quantity_max,
            self.quantity_unit,
            "source",
        )
        self._validate_quantity_group(
            self.base_quantity_value,
            self.base_quantity_min,
            self.base_quantity_max,
            self.base_unit,
            "base",
        )
        if self.quality_status is QualityStatus.ACCEPTED and self.rejection_code is not None:
            raise ValueError("accepted identity cannot have a rejection_code")
        if self.quality_status is not QualityStatus.ACCEPTED and self.rejection_code is None:
            raise ValueError("non-accepted identity requires a rejection_code")
        return self

    @staticmethod
    def _validate_quantity_group(
        exact: Decimal | None,
        minimum: Decimal | None,
        maximum: Decimal | None,
        unit: str | None,
        label: str,
    ) -> None:
        for value in (exact, minimum, maximum):
            if value is not None and value <= 0:
                raise ValueError(f"{label} quantities must be greater than zero")
        if (minimum is None) != (maximum is None):
            raise ValueError(f"{label} quantity range requires both bounds")
        if exact is not None and minimum is not None:
            raise ValueError(f"{label} quantity cannot be exact and a range")
        if minimum is not None and maximum is not None and maximum < minimum:
            raise ValueError(f"{label} quantity maximum cannot be below minimum")
        has_quantity = exact is not None or minimum is not None
        if has_quantity != (unit is not None):
            raise ValueError(f"{label} quantity and unit must be provided together")

    def build_fingerprint(self) -> str:
        has_base_quantity = (
            self.base_quantity_value is not None or self.base_quantity_min is not None
        )
        payload = {
            "normalized_attributes": self.normalized_attributes,
            "condition_code": self.condition_code.value,
            "measure_type": self.measure_type.value,
            "quantity_value": (
                None if has_base_quantity else _decimal_token(self.quantity_value)
            ),
            "quantity_min": None if has_base_quantity else _decimal_token(self.quantity_min),
            "quantity_max": None if has_base_quantity else _decimal_token(self.quantity_max),
            "quantity_unit": None if has_base_quantity else self.quantity_unit,
            "base_quantity_value": _decimal_token(self.base_quantity_value),
            "base_quantity_min": _decimal_token(self.base_quantity_min),
            "base_quantity_max": _decimal_token(self.base_quantity_max),
            "base_unit": self.base_unit,
            "package_count": self.package_count,
        }
        encoded = json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        ).encode()
        return sha256(encoded).hexdigest()


class EvaluatedPriceCandidate(BaseModel):
    """Price candidate after central policy, ready to bind to database identities."""

    model_config = ConfigDict(frozen=True)

    region: CatalogRegion
    current_price: Decimal | None
    original_price: Decimal | None
    original_price_type: OriginalPriceType
    price_nature: PriceNature
    price_type: PriceType
    pricing_basis: PricingBasis
    availability: Availability
    fee_status: FeeStatus
    quality_status: QualityStatus
    rejection_code: str | None
    promotion_label: str | None = None
    displayed_price_text: str | None = None
    unit_price: Decimal | None = None
    unit_price_unit: str | None = None
    evidence_hash: str | None = Field(default=None, min_length=64, max_length=64)
    source_observed_at: datetime | None = None

    @field_validator("evidence_hash")
    @classmethod
    def normalize_evidence_hash(cls, value: str | None) -> str | None:
        return _normalize_optional_sha256(value, field_name="evidence_hash")

    @field_validator("source_observed_at")
    @classmethod
    def normalize_source_observed_at(cls, value: datetime | None) -> datetime | None:
        return _utc_naive_milliseconds(value) if value is not None else None

    @field_validator("rejection_code")
    @classmethod
    def normalize_rejection_code(cls, value: str | None) -> str | None:
        return _normalize_optional_text(value)

    @model_validator(mode="after")
    def validate_quality_pair(self) -> EvaluatedPriceCandidate:
        if self.quality_status is QualityStatus.ACCEPTED and self.rejection_code is not None:
            raise ValueError("accepted price cannot have a rejection_code")
        if self.quality_status is not QualityStatus.ACCEPTED and self.rejection_code is None:
            raise ValueError("non-accepted price requires a rejection_code")
        return self


class CatalogCollectionRequest(BaseModel):
    """One connector run scope; MULTI is represented explicitly at batch level."""

    model_config = ConfigDict(frozen=True)

    region_scope: RegionScope
    region_code: str = Field(min_length=1, max_length=32)
    category_codes: tuple[str, ...] = ()
    source_item_codes: tuple[str, ...] = ()

    @field_validator("region_code")
    @classmethod
    def normalize_region_code(cls, value: str) -> str:
        return value.strip().upper()

    @field_validator("category_codes")
    @classmethod
    def normalize_category_codes(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        return tuple(sorted({value.strip().upper() for value in values if value.strip()}))

    @field_validator("source_item_codes")
    @classmethod
    def normalize_source_item_codes(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        """Normalize explicit source selections without assigning taxonomy meaning to them."""

        return tuple(sorted({value.strip().upper() for value in values if value.strip()}))

    @model_validator(mode="after")
    def validate_region(self) -> CatalogCollectionRequest:
        if self.region_scope is RegionScope.NATIONAL and self.region_code != "CN":
            raise ValueError("NATIONAL runs must use region_code CN")
        if self.region_scope is RegionScope.MULTI and self.region_code != "*":
            raise ValueError("MULTI runs must use region_code *")
        if self.region_scope is not RegionScope.MULTI and self.region_code == "*":
            raise ValueError("only MULTI runs may use region_code *")
        return self

    @property
    def default_region(self) -> CatalogRegion | None:
        if self.region_scope is RegionScope.MULTI:
            return None
        return CatalogRegion(scope=self.region_scope, code=self.region_code)


def normalized_observed_at(value: datetime) -> datetime:
    """Expose the timestamp normalization shared by connector-facing contracts."""

    return _utc_naive_milliseconds(value)
