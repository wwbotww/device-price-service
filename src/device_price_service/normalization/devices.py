"""Explicit device specifications and stable, source-scoped catalog identities."""

from __future__ import annotations

import json
from decimal import Decimal
from hashlib import sha256
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

from device_price_service.domain.catalog_crawl import (
    DiscoveredCatalogListing,
    NormalizedListingIdentity,
    ParsedCatalogListing,
)
from device_price_service.domain.catalog_enums import MeasureType, PriceNature, QualityStatus
from device_price_service.normalization.catalog_rules import CategoryRule
from device_price_service.normalization.specs import (
    build_spec_fingerprint,
    normalize_capacity,
    normalize_text,
)

DEVICE_CATEGORY_CODES = frozenset({"PHONE", "TABLET", "LAPTOP", "DESKTOP", "WATCH"})


class DeviceSpecification(BaseModel):
    """Only proven configuration fields; titles, prices and stock are not identity."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    color: str | None = Field(default=None, max_length=128)
    capacity: str | None = Field(default=None, max_length=64)
    memory: str | None = Field(default=None, max_length=64)
    connectivity: str | None = Field(default=None, max_length=64)
    size: str | None = Field(default=None, max_length=64)
    edition: str | None = Field(default=None, max_length=128)
    manufacturer_part_number: str | None = Field(default=None, max_length=128)
    # Exact textual dimensions such as processor, Huawei style/version and Apple
    # case/band configuration; connectors flatten labelled source dimensions here.
    attributes: dict[str, str] = Field(default_factory=dict)

    @field_validator("color", "connectivity", "size", "edition", "manufacturer_part_number")
    @classmethod
    def normalize_optional_text(cls, value: str | None) -> str | None:
        return normalize_text(value) or None if value is not None else None

    @field_validator("capacity", "memory")
    @classmethod
    def normalize_storage(cls, value: str | None) -> str | None:
        return normalize_capacity(value) or None

    @field_validator("attributes")
    @classmethod
    def normalize_attributes(cls, values: dict[str, str]) -> dict[str, str]:
        normalized: dict[str, str] = {}
        for raw_key, raw_value in values.items():
            key, value = normalize_text(raw_key).lower(), normalize_text(raw_value)
            if not key or not value or key in normalized:
                raise ValueError("device dimensions must have distinct non-empty keys and values")
            if key in {
                "price",
                "current_price",
                "original_price",
                "availability",
                "stock",
                "title",
                "name",
                "observed_at",
                "fetched_at",
                "url",
            }:
                raise ValueError("mutable quote fields cannot be device identity dimensions")
            normalized[key] = value
        return normalized

    @model_validator(mode="after")
    def require_proven_configuration(self) -> DeviceSpecification:
        if not self.identity_attributes():
            raise ValueError("device specification needs at least one proven configuration field")
        return self

    def identity_attributes(self) -> dict[str, Any]:
        fields = self.model_dump(exclude_none=True, exclude={"attributes"})
        if self.attributes:
            fields["attributes"] = self.attributes
        return fields


def device_item_key(*, brand_code: str, channel_code: str, product_id: str) -> str:
    parts = [brand_code.strip().upper(), channel_code.strip().upper(), product_id.strip()]
    if not all(parts):
        raise ValueError("device item identity requires brand, source and official product ID")
    return sha256(json.dumps(parts, ensure_ascii=False, separators=(",", ":")).encode()).hexdigest()


def device_listing_key(
    *,
    product_id: str,
    sku_id: str | None,
    specification: DeviceSpecification,
) -> str:
    if not product_id.strip() or (sku_id is not None and not sku_id.strip()):
        raise ValueError("official product and supplied SKU identities cannot be blank")
    identity = (
        sku_id.strip() if sku_id else build_spec_fingerprint(specification.identity_attributes())
    )
    return json.dumps(
        [product_id.strip(), "sku" if sku_id else "spec", identity],
        ensure_ascii=False,
        separators=(",", ":"),
    )


class DeviceCategoryRule(CategoryRule):
    profile_code = "electronic-device"
    version = "1"

    def normalize(
        self,
        item: DiscoveredCatalogListing,
        parsed: ParsedCatalogListing,
    ) -> NormalizedListingIdentity:
        rejection: str | None = None
        attributes: dict[str, Any] = {}
        if item.category_code not in DEVICE_CATEGORY_CODES:
            rejection = "DEVICE_CATEGORY_UNSUPPORTED"
        elif not item.external_product_id or item.price_nature is not PriceNature.RETAIL_OFFER:
            rejection = "DEVICE_SOURCE_IDENTITY_INVALID"
        else:
            try:
                spec = DeviceSpecification.model_validate(
                    parsed.source_attributes.get("device_specification")
                )
                attributes = spec.identity_attributes()
                expected_key = device_listing_key(
                    product_id=item.external_product_id,
                    sku_id=item.external_sku_id,
                    specification=spec,
                )
                if item.listing_key != expected_key:
                    rejection = "DEVICE_LISTING_KEY_MISMATCH"
            except ValidationError:
                rejection = "DEVICE_SPECIFICATION_UNPROVEN"
        return NormalizedListingIdentity(
            normalized_attributes=attributes,
            measure_type=MeasureType.COUNT,
            quantity_value=Decimal(1),
            quantity_unit="PIECE",
            base_quantity_value=Decimal(1),
            base_unit="PIECE",
            package_count=1,
            quality_status=(QualityStatus.REVIEW_REQUIRED if rejection else QualityStatus.ACCEPTED),
            rejection_code=rejection,
        )
