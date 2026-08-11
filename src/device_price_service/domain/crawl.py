from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from hashlib import sha256
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from device_price_service.domain.enums import (
    Availability,
    FetchMethod,
    OriginalPriceType,
)
from device_price_service.domain.models import utc_now_naive


@dataclass(frozen=True, slots=True)
class FetchResult:
    request_url: str
    final_url: str
    status_code: int
    headers: dict[str, str]
    body: bytes
    fetched_at: datetime
    duration_ms: int
    fetch_method: FetchMethod

    @property
    def source_hash(self) -> str:
        return sha256(self.body).hexdigest()

    @property
    def content_type(self) -> str:
        return self.headers.get("content-type", "").split(";", maxsplit=1)[0].strip().lower()


@dataclass(frozen=True, slots=True)
class BrowserVariantDimension:
    name: str
    container_selector: str
    heading_text: str | tuple[str, ...]
    option_selector: str = "li"
    excluded_values: tuple[str, ...] = ()
    optional: bool = False


@dataclass(frozen=True, slots=True)
class BrowserFixedOption:
    container_selector: str
    value: str
    option_selector: str = "li"


@dataclass(frozen=True, slots=True)
class BrowserSnapshotPlan:
    ready_selector: str
    snapshot_selector: str
    dimensions: tuple[BrowserVariantDimension, ...]
    fixed_options: tuple[BrowserFixedOption, ...] = ()
    settle_ms: int = 300
    max_snapshots: int = 64


class DiscoveredProduct(BaseModel):
    model_config = ConfigDict(frozen=True)

    official_product_id: str = Field(min_length=1, max_length=128)
    url: str = Field(min_length=1, max_length=1024)
    category_code: str | None = Field(default=None, max_length=32)
    metadata: dict[str, Any] = Field(default_factory=dict)


class ParsedProduct(BaseModel):
    model_config = ConfigDict(frozen=True)

    source_url: str = Field(min_length=1, max_length=1024)
    payload: dict[str, Any]


class NormalizedOffer(BaseModel):
    official_offer_id: str | None = Field(default=None, max_length=128)
    source_url: str = Field(min_length=1, max_length=1024)
    currency: str = "CNY"
    original_price: Decimal | None = None
    original_price_type: OriginalPriceType = OriginalPriceType.NONE
    current_price: Decimal | None = None
    availability: Availability

    @field_validator("currency")
    @classmethod
    def currency_must_be_cny(cls, value: str) -> str:
        normalized = value.upper()
        if normalized != "CNY":
            raise ValueError("V1 only accepts CNY prices")
        return normalized

    @model_validator(mode="after")
    def validate_price_semantics(self) -> NormalizedOffer:
        for field_name in ("original_price", "current_price"):
            value = getattr(self, field_name)
            if value is not None and value <= 0:
                raise ValueError(f"{field_name} must be greater than zero")
        if self.original_price is None and self.original_price_type is not OriginalPriceType.NONE:
            raise ValueError("original_price_type must be NONE when original_price is null")
        if self.original_price is not None and self.original_price_type is OriginalPriceType.NONE:
            raise ValueError("original_price_type must describe a non-null original_price")
        return self


class NormalizedSku(BaseModel):
    official_sku_id: str | None = Field(default=None, max_length=128)
    name: str = Field(min_length=1, max_length=255)
    color: str | None = Field(default=None, max_length=128)
    capacity: str | None = Field(default=None, max_length=64)
    memory: str | None = Field(default=None, max_length=64)
    connectivity: str | None = Field(default=None, max_length=64)
    size: str | None = Field(default=None, max_length=64)
    attributes: dict[str, Any] = Field(default_factory=dict)
    spec_fingerprint: str = Field(min_length=64, max_length=64)
    status: str = "ACTIVE"
    offers: list[NormalizedOffer] = Field(min_length=1)

    @field_validator("spec_fingerprint")
    @classmethod
    def fingerprint_must_be_hex(cls, value: str) -> str:
        normalized = value.lower()
        if any(character not in "0123456789abcdef" for character in normalized):
            raise ValueError("spec_fingerprint must be a SHA-256 hexadecimal string")
        return normalized


class NormalizedProduct(BaseModel):
    brand_code: str = Field(min_length=1, max_length=32)
    channel_code: str = Field(min_length=1, max_length=64)
    category_code: str = Field(min_length=1, max_length=32)
    official_product_id: str = Field(min_length=1, max_length=128)
    name: str = Field(min_length=1, max_length=255)
    series_name: str | None = Field(default=None, max_length=128)
    model_number: str | None = Field(default=None, max_length=128)
    official_url: str = Field(min_length=1, max_length=1024)
    lifecycle_status: str = "ACTIVE"
    skus: list[NormalizedSku] = Field(min_length=1)


@dataclass(frozen=True, slots=True)
class ArtifactReference:
    relative_path: str
    source_hash: str
    size_bytes: int


@dataclass(frozen=True, slots=True)
class CrawlOutcome:
    crawl_run_id: int
    discovered_count: int
    success_count: int
    skipped_count: int
    failed_count: int


def replay_fetch_result(
    *,
    request_url: str,
    final_url: str,
    status_code: int,
    body: bytes,
    fetched_at: datetime | None = None,
) -> FetchResult:
    return FetchResult(
        request_url=request_url,
        final_url=final_url,
        status_code=status_code,
        headers={},
        body=body,
        fetched_at=fetched_at or utc_now_naive(),
        duration_ms=0,
        fetch_method=FetchMethod.REPLAY,
    )
