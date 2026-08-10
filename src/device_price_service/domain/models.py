from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from device_price_service.domain.enums import Availability, OriginalPriceType


def utc_now_naive() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


class PriceObservation(BaseModel):
    model_config = ConfigDict(frozen=True)

    offer_id: int = Field(gt=0)
    crawl_run_id: int = Field(gt=0)
    currency: str = "CNY"
    original_price: Decimal | None = None
    original_price_type: OriginalPriceType = OriginalPriceType.NONE
    current_price: Decimal | None = None
    availability: Availability
    observed_at: datetime = Field(default_factory=utc_now_naive)
    source_hash: str = Field(min_length=64, max_length=64)

    @field_validator("currency")
    @classmethod
    def currency_must_be_cny(cls, value: str) -> str:
        normalized = value.upper()
        if normalized != "CNY":
            raise ValueError("V1 only accepts CNY prices")
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
            return value.astimezone(UTC).replace(tzinfo=None)
        return value

    @model_validator(mode="after")
    def validate_price_semantics(self) -> PriceObservation:
        for field_name in ("original_price", "current_price"):
            value = getattr(self, field_name)
            if value is not None and value <= 0:
                raise ValueError(f"{field_name} must be greater than zero")

        if self.original_price is None and self.original_price_type is not OriginalPriceType.NONE:
            raise ValueError("original_price_type must be NONE when original_price is null")
        if self.original_price is not None and self.original_price_type is OriginalPriceType.NONE:
            raise ValueError("original_price_type must describe a non-null original_price")
        return self
