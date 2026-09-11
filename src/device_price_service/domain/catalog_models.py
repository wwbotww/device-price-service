from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from hashlib import sha256

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

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
from device_price_service.domain.models import utc_now_naive


def _decimal_token(value: Decimal | None) -> str:
    return "" if value is None else format(value, "f")


class CatalogPriceObservation(BaseModel):
    """One parsed price candidate observed at a source and time.

    Facts are immutable. Rejected candidates are retained for audit, while only
    accepted observations with unconditional semantics may advance the current
    price projection.
    """

    model_config = ConfigDict(frozen=True)

    source_listing_id: int = Field(gt=0)
    listing_revision_id: int = Field(gt=0)
    crawl_record_id: int = Field(gt=0)
    supersedes_observation_id: int | None = Field(default=None, gt=0)
    region_scope: RegionScope
    region_code: str = Field(min_length=1, max_length=32)
    currency: str = "CNY"
    original_price: Decimal | None = None
    original_price_type: OriginalPriceType = OriginalPriceType.NONE
    current_price: Decimal | None = None
    price_nature: PriceNature
    price_type: PriceType
    pricing_basis: PricingBasis
    promotion_label: str | None = Field(default=None, max_length=128)
    availability: Availability
    unit_price: Decimal | None = None
    unit_price_unit: str | None = Field(default=None, max_length=16)
    fee_status: FeeStatus
    quality_status: QualityStatus
    rejection_code: str | None = Field(default=None, max_length=64)
    displayed_price_text: str | None = Field(default=None, max_length=255)
    source_hash: str = Field(min_length=64, max_length=64)
    observed_at: datetime = Field(default_factory=utc_now_naive)

    @field_validator("currency")
    @classmethod
    def currency_must_be_cny(cls, value: str) -> str:
        normalized = value.upper()
        if normalized != "CNY":
            raise ValueError("catalog prices must use CNY")
        return normalized

    @field_validator("source_hash")
    @classmethod
    def source_hash_must_be_hex(cls, value: str) -> str:
        normalized = value.lower()
        if any(character not in "0123456789abcdef" for character in normalized):
            raise ValueError("source_hash must be a SHA-256 hexadecimal string")
        return normalized

    @field_validator("observed_at")
    @classmethod
    def observed_at_must_be_utc_naive(cls, value: datetime) -> datetime:
        if value.tzinfo is not None:
            value = value.astimezone(UTC).replace(tzinfo=None)
        return value.replace(microsecond=(value.microsecond // 1000) * 1000)

    @field_validator("region_code")
    @classmethod
    def normalize_region_code(cls, value: str) -> str:
        normalized = value.strip().upper()
        if not normalized:
            raise ValueError("region_code cannot be blank")
        return normalized

    @field_validator("unit_price_unit")
    @classmethod
    def normalize_unit_price_unit(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = value.strip().upper()
        if not normalized:
            raise ValueError("unit_price_unit cannot be blank")
        return normalized

    @model_validator(mode="after")
    def validate_semantics(self) -> CatalogPriceObservation:
        for field_name in ("original_price", "current_price", "unit_price"):
            value = getattr(self, field_name)
            if value is not None and value <= 0:
                raise ValueError(f"{field_name} must be greater than zero")

        if self.original_price is None and self.original_price_type is not OriginalPriceType.NONE:
            raise ValueError("original_price_type must be NONE when original_price is null")
        if self.original_price is not None and self.original_price_type is OriginalPriceType.NONE:
            raise ValueError("original_price_type must describe a non-null original_price")
        if (self.unit_price is None) != (self.unit_price_unit is None):
            raise ValueError("unit_price and unit_price_unit must both be null or non-null")
        if self.region_scope is RegionScope.MULTI:
            raise ValueError("one price observation cannot use a MULTI region")
        if self.region_scope is RegionScope.NATIONAL and self.region_code != "CN":
            raise ValueError("NATIONAL observations must use region_code CN")
        if self.quality_status is QualityStatus.ACCEPTED:
            if self.rejection_code is not None:
                raise ValueError("accepted observations cannot have a rejection_code")
            self._validate_current_eligibility()
        elif self.rejection_code is None:
            raise ValueError("non-accepted observations require a rejection_code")
        return self

    def _validate_current_eligibility(self) -> None:
        if self.price_type is PriceType.AVAILABILITY_ONLY:
            if self.price_nature is not PriceNature.RETAIL_OFFER:
                raise ValueError("availability-only facts require RETAIL_OFFER")
            if self.region_scope is RegionScope.UNKNOWN:
                raise ValueError("accepted observations require a known region")
            if self.availability not in {
                Availability.OFF_SHELF,
                Availability.OUT_OF_STOCK,
                Availability.COMING_SOON,
            }:
                raise ValueError("availability-only facts require an explicit non-selling state")
            if any(
                value is not None
                for value in (
                    self.current_price,
                    self.original_price,
                    self.unit_price,
                    self.unit_price_unit,
                )
            ):
                raise ValueError("availability-only facts cannot contain amounts or unit prices")
            if (
                self.pricing_basis is not PricingBasis.UNKNOWN
                or self.fee_status is not FeeStatus.NOT_APPLICABLE
                or self.promotion_label is not None
            ):
                raise ValueError("availability-only facts cannot carry pricing or promotion terms")
            return
        if self.current_price is None:
            raise ValueError("accepted observations require current_price")
        if self.region_scope is RegionScope.UNKNOWN:
            raise ValueError("accepted observations require a known region")
        if self.price_nature is PriceNature.UNKNOWN:
            raise ValueError("accepted observations require a known price_nature")
        if self.pricing_basis not in {PricingBasis.PACKAGE_TOTAL, PricingBasis.UNIT_QUOTED}:
            raise ValueError("accepted observations require an exact pricing_basis")
        if self.pricing_basis is PricingBasis.UNIT_QUOTED and self.unit_price is None:
            raise ValueError("UNIT_QUOTED observations require a normalized unit_price")

        if self.price_nature in {PriceNature.RETAIL_OFFER, PriceNature.WHOLESALE_OFFER}:
            if self.price_type is not PriceType.DIRECT_UNCONDITIONAL:
                raise ValueError("accepted offer prices must be direct and unconditional")
            if self.fee_status not in {
                FeeStatus.ITEM_ONLY,
                FeeStatus.SEPARATE_FEES_EXCLUDED,
            }:
                raise ValueError("accepted offer prices require separable item-only fees")
            return

        if self.price_nature in {
            PriceNature.RETAIL_AVERAGE,
            PriceNature.WHOLESALE_AVERAGE,
            PriceNature.MARKET_AVERAGE,
        }:
            if self.price_type is not PriceType.PUBLISHED_VALUE:
                raise ValueError("accepted market averages must be published values")
            if self.fee_status is not FeeStatus.NOT_APPLICABLE:
                raise ValueError("market averages require NOT_APPLICABLE fee status")

    @property
    def eligible_for_current(self) -> bool:
        return self.quality_status is QualityStatus.ACCEPTED

    def build_observation_key(self, *, adapter_version: str, policy_version: str) -> str:
        if not adapter_version.strip() or not policy_version.strip():
            raise ValueError("adapter_version and policy_version cannot be blank")
        price_payload = "|".join(
            (
                self.currency,
                _decimal_token(self.original_price),
                self.original_price_type.value,
                _decimal_token(self.current_price),
                self.price_nature.value,
                self.price_type.value,
                self.pricing_basis.value,
                self.promotion_label or "",
                self.availability.value,
                _decimal_token(self.unit_price),
                self.unit_price_unit or "",
                self.fee_status.value,
                self.quality_status.value,
                self.rejection_code or "",
                self.displayed_price_text or "",
                str(self.supersedes_observation_id or ""),
            )
        )
        price_payload_hash = sha256(price_payload.encode()).hexdigest()
        identity = "|".join(
            (
                str(self.source_listing_id),
                str(self.listing_revision_id),
                self.region_scope.value,
                self.region_code,
                self.observed_at.isoformat(timespec="milliseconds"),
                self.source_hash,
                price_payload_hash,
                adapter_version.strip(),
                policy_version.strip(),
            )
        )
        return sha256(identity.encode()).hexdigest()
