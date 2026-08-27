from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from typing import Any

from sqlalchemy import (
    JSON,
    Boolean,
    CheckConstraint,
    ForeignKey,
    Index,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects import mysql
from sqlalchemy.orm import Mapped, mapped_column

from device_price_service.db.base import Base
from device_price_service.db.models import created_at_column, updated_at_column
from device_price_service.domain.catalog_enums import (
    AccessMode,
    Availability,
    BusinessMode,
    CatalogEntityType,
    CollectionFetchMethod,
    CollectionTriggerType,
    ConditionCode,
    FeeStatus,
    ItemStatus,
    ItemType,
    LifecycleStatus,
    MatchMethod,
    MatchStatus,
    MeasureType,
    OperationStatus,
    OriginalPriceType,
    PriceNature,
    PriceType,
    PricingBasis,
    QualityStatus,
    RecordOrigin,
    RegionMode,
    RegionScope,
    RunStatus,
    RunType,
    SellerType,
    SourceType,
    VerificationStatus,
)

BIGINT = mysql.BIGINT(unsigned=True)
INTEGER = mysql.INTEGER(unsigned=True)
SMALLINT = mysql.SMALLINT(unsigned=True)
TINYINT = mysql.TINYINT(unsigned=True)
DATETIME = mysql.DATETIME(fsp=3)


def _enum_check(column_name: str, enum_type: type[StrEnum]) -> str:
    values = ",".join(f"'{member.value}'" for member in enum_type)
    return f"{column_name} IN ({values})"


class CatalogBrand(Base):
    __tablename__ = "v2_brand"
    __table_args__ = (CheckConstraint(_enum_check("status", LifecycleStatus), name="status"),)

    id: Mapped[int] = mapped_column(BIGINT, primary_key=True, autoincrement=True)
    code: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    name_zh: Mapped[str] = mapped_column(String(128), nullable=False)
    name_en: Mapped[str | None] = mapped_column(String(128), nullable=True)
    status: Mapped[str] = mapped_column(String(16), nullable=False, server_default=text("'ACTIVE'"))
    created_at: Mapped[datetime] = created_at_column()
    updated_at: Mapped[datetime] = updated_at_column()


class TaxonomyCategory(Base):
    __tablename__ = "v2_category"
    __table_args__ = (
        CheckConstraint("level >= 0", name="level"),
        CheckConstraint(
            f"default_measure_type IS NULL OR {_enum_check('default_measure_type', MeasureType)}",
            name="default_measure_type",
        ),
        Index("ix_v2_category_parent_id", "parent_id"),
        Index("ix_v2_category_path", "path"),
        Index("ix_v2_category_enabled", "enabled"),
    )

    id: Mapped[int] = mapped_column(BIGINT, primary_key=True, autoincrement=True)
    parent_id: Mapped[int | None] = mapped_column(
        BIGINT,
        ForeignKey("v2_category.id", ondelete="RESTRICT"),
        nullable=True,
    )
    code: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    name_zh: Mapped[str] = mapped_column(String(128), nullable=False)
    name_en: Mapped[str | None] = mapped_column(String(128), nullable=True)
    level: Mapped[int] = mapped_column(TINYINT, nullable=False)
    path: Mapped[str] = mapped_column(String(512), nullable=False)
    is_leaf: Mapped[bool] = mapped_column(Boolean, nullable=False)
    default_measure_type: Mapped[str | None] = mapped_column(String(16), nullable=True)
    attribute_profile_code: Mapped[str] = mapped_column(String(64), nullable=False)
    attribute_profile_version: Mapped[str] = mapped_column(String(32), nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("1"))
    created_at: Mapped[datetime] = created_at_column()
    updated_at: Mapped[datetime] = updated_at_column()


class CatalogItem(Base):
    __tablename__ = "v2_catalog_item"
    __table_args__ = (
        CheckConstraint(_enum_check("item_type", ItemType), name="item_type"),
        CheckConstraint(_enum_check("record_origin", RecordOrigin), name="record_origin"),
        CheckConstraint(_enum_check("status", ItemStatus), name="status"),
        Index("ix_v2_catalog_item_category_status", "category_id", "status"),
        Index("ix_v2_catalog_item_brand_id", "brand_id"),
        Index("ix_v2_catalog_item_model_number", "model_number"),
    )

    id: Mapped[int] = mapped_column(BIGINT, primary_key=True, autoincrement=True)
    category_id: Mapped[int] = mapped_column(
        BIGINT,
        ForeignKey("v2_category.id", ondelete="RESTRICT"),
        nullable=False,
    )
    brand_id: Mapped[int | None] = mapped_column(
        BIGINT,
        ForeignKey("v2_brand.id", ondelete="RESTRICT"),
        nullable=True,
    )
    canonical_key: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    item_type: Mapped[str] = mapped_column(String(24), nullable=False)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    series_name: Mapped[str | None] = mapped_column(String(128), nullable=True)
    model_number: Mapped[str | None] = mapped_column(String(128), nullable=True)
    base_attributes: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    record_origin: Mapped[str] = mapped_column(String(16), nullable=False)
    status: Mapped[str] = mapped_column(String(24), nullable=False)
    created_at: Mapped[datetime] = created_at_column()
    updated_at: Mapped[datetime] = updated_at_column()


class ItemVariant(Base):
    __tablename__ = "v2_item_variant"
    __table_args__ = (
        UniqueConstraint("catalog_item_id", "variant_key", name="uq_v2_variant_item_key"),
        CheckConstraint(_enum_check("condition_code", ConditionCode), name="condition_code"),
        CheckConstraint(_enum_check("measure_type", MeasureType), name="measure_type"),
        CheckConstraint(_enum_check("status", ItemStatus), name="status"),
        CheckConstraint(
            "quantity_value IS NULL OR quantity_value > 0",
            name="quantity_value",
        ),
        CheckConstraint("quantity_min IS NULL OR quantity_min > 0", name="quantity_min"),
        CheckConstraint("quantity_max IS NULL OR quantity_max > 0", name="quantity_max"),
        CheckConstraint(
            "(quantity_min IS NULL AND quantity_max IS NULL) OR "
            "(quantity_min IS NOT NULL AND quantity_max IS NOT NULL)",
            name="quantity_range_pair",
        ),
        CheckConstraint(
            "quantity_value IS NULL OR (quantity_min IS NULL AND quantity_max IS NULL)",
            name="quantity_exact_or_range",
        ),
        CheckConstraint(
            "quantity_min IS NULL OR quantity_max >= quantity_min",
            name="quantity_range_order",
        ),
        CheckConstraint("package_count IS NULL OR package_count > 0", name="package_count"),
        Index("ix_v2_item_variant_gtin", "gtin"),
        Index("ix_v2_item_variant_mpn", "manufacturer_part_number"),
        Index("ix_v2_item_variant_identity", "identity_fingerprint"),
    )

    id: Mapped[int] = mapped_column(BIGINT, primary_key=True, autoincrement=True)
    catalog_item_id: Mapped[int] = mapped_column(
        BIGINT,
        ForeignKey("v2_catalog_item.id", ondelete="RESTRICT"),
        nullable=False,
    )
    supersedes_variant_id: Mapped[int | None] = mapped_column(
        BIGINT,
        ForeignKey("v2_item_variant.id", ondelete="RESTRICT"),
        nullable=True,
    )
    variant_key: Mapped[str] = mapped_column(String(64), nullable=False)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    gtin: Mapped[str | None] = mapped_column(String(32), nullable=True)
    manufacturer_part_number: Mapped[str | None] = mapped_column(String(128), nullable=True)
    condition_code: Mapped[str] = mapped_column(String(24), nullable=False)
    measure_type: Mapped[str] = mapped_column(String(16), nullable=False)
    quantity_value: Mapped[Decimal | None] = mapped_column(Numeric(20, 6), nullable=True)
    quantity_min: Mapped[Decimal | None] = mapped_column(Numeric(20, 6), nullable=True)
    quantity_max: Mapped[Decimal | None] = mapped_column(Numeric(20, 6), nullable=True)
    base_unit: Mapped[str] = mapped_column(String(16), nullable=False)
    package_count: Mapped[int | None] = mapped_column(INTEGER, nullable=True)
    attributes: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    identity_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(24), nullable=False)
    created_at: Mapped[datetime] = created_at_column()
    updated_at: Mapped[datetime] = updated_at_column()


class SourceChannel(Base):
    __tablename__ = "v2_source_channel"
    __table_args__ = (
        CheckConstraint(_enum_check("source_type", SourceType), name="source_type"),
        CheckConstraint(_enum_check("business_mode", BusinessMode), name="business_mode"),
        CheckConstraint(_enum_check("access_mode", AccessMode), name="access_mode"),
        CheckConstraint(_enum_check("region_mode", RegionMode), name="region_mode"),
        CheckConstraint("currency = 'CNY'", name="currency"),
    )

    id: Mapped[int] = mapped_column(BIGINT, primary_key=True, autoincrement=True)
    code: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    source_type: Mapped[str] = mapped_column(String(32), nullable=False)
    business_mode: Mapped[str] = mapped_column(String(24), nullable=False)
    access_mode: Mapped[str] = mapped_column(String(16), nullable=False)
    base_url: Mapped[str] = mapped_column(String(512), nullable=False)
    allowed_domains: Mapped[list[str]] = mapped_column(JSON, nullable=False)
    region_mode: Mapped[str] = mapped_column(String(16), nullable=False)
    currency: Mapped[str] = mapped_column(String(3), nullable=False, server_default=text("'CNY'"))
    connector_code: Mapped[str] = mapped_column(String(64), nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("1"))
    created_at: Mapped[datetime] = created_at_column()
    updated_at: Mapped[datetime] = updated_at_column()


class Merchant(Base):
    __tablename__ = "v2_merchant"
    __table_args__ = (
        UniqueConstraint(
            "source_channel_id",
            "merchant_key_hash",
            name="uq_v2_merchant_channel_key",
        ),
        CheckConstraint(_enum_check("seller_type", SellerType), name="seller_type"),
        CheckConstraint(
            _enum_check("verification_status", VerificationStatus),
            name="verification_status",
        ),
        CheckConstraint(_enum_check("status", LifecycleStatus), name="status"),
        CheckConstraint("last_seen_at >= first_seen_at", name="observation_range"),
        Index("ix_v2_merchant_channel_external", "source_channel_id", "external_merchant_id"),
        Index("ix_v2_merchant_seller_type", "seller_type"),
    )

    id: Mapped[int] = mapped_column(BIGINT, primary_key=True, autoincrement=True)
    source_channel_id: Mapped[int] = mapped_column(
        BIGINT,
        ForeignKey("v2_source_channel.id", ondelete="RESTRICT"),
        nullable=False,
    )
    merchant_key_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    external_merchant_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    seller_type: Mapped[str] = mapped_column(String(32), nullable=False)
    verification_status: Mapped[str] = mapped_column(String(24), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    first_seen_at: Mapped[datetime] = mapped_column(DATETIME, nullable=False)
    last_seen_at: Mapped[datetime] = mapped_column(DATETIME, nullable=False)
    created_at: Mapped[datetime] = created_at_column()
    updated_at: Mapped[datetime] = updated_at_column()


class SourceListing(Base):
    __tablename__ = "v2_source_listing"
    __table_args__ = (
        UniqueConstraint(
            "source_channel_id",
            "merchant_id",
            "listing_key_hash",
            name="uq_v2_listing_channel_merchant_key",
        ),
        CheckConstraint(_enum_check("price_nature", PriceNature), name="price_nature"),
        CheckConstraint(
            _enum_check("lifecycle_status", LifecycleStatus),
            name="lifecycle_status",
        ),
        CheckConstraint("consecutive_misses >= 0", name="consecutive_misses"),
        CheckConstraint("last_seen_at >= first_seen_at", name="observation_range"),
        Index(
            "ix_v2_listing_channel_external_product",
            "source_channel_id",
            "external_product_id",
        ),
        Index(
            "ix_v2_listing_channel_external_sku",
            "source_channel_id",
            "external_sku_id",
        ),
        Index("ix_v2_listing_url_hash", "url_hash"),
        Index("ix_v2_listing_lifecycle_seen", "lifecycle_status", "last_seen_at"),
    )

    id: Mapped[int] = mapped_column(BIGINT, primary_key=True, autoincrement=True)
    source_channel_id: Mapped[int] = mapped_column(
        BIGINT,
        ForeignKey("v2_source_channel.id", ondelete="RESTRICT"),
        nullable=False,
    )
    merchant_id: Mapped[int] = mapped_column(
        BIGINT,
        ForeignKey("v2_merchant.id", ondelete="RESTRICT"),
        nullable=False,
    )
    listing_key: Mapped[str] = mapped_column(String(512), nullable=False)
    listing_key_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    external_product_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    external_sku_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    price_nature: Mapped[str] = mapped_column(String(32), nullable=False)
    canonical_url: Mapped[str] = mapped_column(String(1024), nullable=False)
    url_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    current_revision_id: Mapped[int | None] = mapped_column(
        BIGINT,
        ForeignKey(
            "v2_listing_revision.id",
            name="fk_v2_source_listing_current_revision",
            ondelete="RESTRICT",
            use_alter=True,
        ),
        nullable=True,
    )
    lifecycle_status: Mapped[str] = mapped_column(
        String(24), nullable=False, server_default=text("'UNKNOWN'")
    )
    consecutive_misses: Mapped[int] = mapped_column(
        INTEGER, nullable=False, server_default=text("0")
    )
    first_seen_at: Mapped[datetime] = mapped_column(DATETIME, nullable=False)
    last_seen_at: Mapped[datetime] = mapped_column(DATETIME, nullable=False)
    created_at: Mapped[datetime] = created_at_column()
    updated_at: Mapped[datetime] = updated_at_column()


class CatalogCrawlRun(Base):
    __tablename__ = "v2_crawl_run"
    __table_args__ = (
        CheckConstraint(_enum_check("region_scope", RegionScope), name="region_scope"),
        CheckConstraint(_enum_check("run_type", RunType), name="run_type"),
        CheckConstraint(
            _enum_check("trigger_type", CollectionTriggerType),
            name="trigger_type",
        ),
        CheckConstraint(_enum_check("status", RunStatus), name="status"),
        CheckConstraint(
            "finished_at IS NULL OR finished_at >= started_at",
            name="time_range",
        ),
        CheckConstraint(
            "(region_scope <> 'NATIONAL' OR region_code = 'CN') AND "
            "(region_scope <> 'MULTI' OR region_code = '*')",
            name="region_semantics",
        ),
        CheckConstraint(
            "discovered_count >= 0 AND fetched_count >= 0 AND accepted_count >= 0 "
            "AND review_count >= 0 AND rejected_count >= 0 AND failed_count >= 0",
            name="counts",
        ),
        Index("ix_v2_crawl_run_channel_started", "source_channel_id", "started_at"),
        Index("ix_v2_crawl_run_status_started", "status", "started_at"),
        Index("ix_v2_crawl_run_region_started", "region_code", "started_at"),
    )

    id: Mapped[int] = mapped_column(BIGINT, primary_key=True, autoincrement=True)
    source_channel_id: Mapped[int] = mapped_column(
        BIGINT,
        ForeignKey("v2_source_channel.id", ondelete="RESTRICT"),
        nullable=False,
    )
    region_scope: Mapped[str] = mapped_column(String(24), nullable=False)
    region_code: Mapped[str] = mapped_column(String(32), nullable=False)
    category_scope: Mapped[list[str]] = mapped_column(JSON, nullable=False)
    run_type: Mapped[str] = mapped_column(String(24), nullable=False)
    trigger_type: Mapped[str] = mapped_column(String(24), nullable=False)
    status: Mapped[str] = mapped_column(String(24), nullable=False)
    adapter_version: Mapped[str] = mapped_column(String(64), nullable=False)
    policy_version: Mapped[str] = mapped_column(String(64), nullable=False)
    started_at: Mapped[datetime] = mapped_column(DATETIME, nullable=False)
    finished_at: Mapped[datetime | None] = mapped_column(DATETIME, nullable=True)
    discovered_count: Mapped[int] = mapped_column(INTEGER, nullable=False, server_default=text("0"))
    fetched_count: Mapped[int] = mapped_column(INTEGER, nullable=False, server_default=text("0"))
    accepted_count: Mapped[int] = mapped_column(INTEGER, nullable=False, server_default=text("0"))
    review_count: Mapped[int] = mapped_column(INTEGER, nullable=False, server_default=text("0"))
    rejected_count: Mapped[int] = mapped_column(INTEGER, nullable=False, server_default=text("0"))
    failed_count: Mapped[int] = mapped_column(INTEGER, nullable=False, server_default=text("0"))
    error_summary: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = created_at_column()


class CatalogCrawlRecord(Base):
    __tablename__ = "v2_crawl_record"
    __table_args__ = (
        CheckConstraint(_enum_check("entity_type", CatalogEntityType), name="entity_type"),
        CheckConstraint(_enum_check("fetch_method", CollectionFetchMethod), name="fetch_method"),
        CheckConstraint(_enum_check("fetch_status", OperationStatus), name="fetch_status"),
        CheckConstraint(_enum_check("parse_status", OperationStatus), name="parse_status"),
        CheckConstraint(
            _enum_check("validation_status", OperationStatus),
            name="validation_status",
        ),
        CheckConstraint("raw_size_bytes IS NULL OR raw_size_bytes >= 0", name="raw_size"),
        CheckConstraint("duration_ms >= 0", name="duration"),
        CheckConstraint(
            "http_status IS NULL OR (http_status >= 100 AND http_status <= 599)",
            name="http_status",
        ),
        Index("ix_v2_crawl_record_run_parse", "crawl_run_id", "parse_status"),
        Index("ix_v2_crawl_record_listing_fetched", "source_listing_id", "fetched_at"),
        Index("ix_v2_crawl_record_entity", "entity_type", "entity_key"),
        Index("ix_v2_crawl_record_fetched_at", "fetched_at"),
        Index("ix_v2_crawl_record_raw_hash", "raw_hash"),
    )

    id: Mapped[int] = mapped_column(BIGINT, primary_key=True, autoincrement=True)
    crawl_run_id: Mapped[int] = mapped_column(
        BIGINT,
        ForeignKey("v2_crawl_run.id", ondelete="RESTRICT"),
        nullable=False,
    )
    source_listing_id: Mapped[int | None] = mapped_column(
        BIGINT,
        ForeignKey("v2_source_listing.id", ondelete="RESTRICT"),
        nullable=True,
    )
    replayed_from_record_id: Mapped[int | None] = mapped_column(
        BIGINT,
        ForeignKey("v2_crawl_record.id", ondelete="RESTRICT"),
        nullable=True,
    )
    entity_type: Mapped[str] = mapped_column(String(32), nullable=False)
    entity_key: Mapped[str] = mapped_column(String(255), nullable=False)
    request_url: Mapped[str] = mapped_column(String(1024), nullable=False)
    final_url: Mapped[str] = mapped_column(String(1024), nullable=False)
    fetch_method: Mapped[str] = mapped_column(String(16), nullable=False)
    http_status: Mapped[int | None] = mapped_column(SMALLINT, nullable=True)
    fetch_status: Mapped[str] = mapped_column(String(24), nullable=False)
    parse_status: Mapped[str] = mapped_column(String(24), nullable=False)
    validation_status: Mapped[str] = mapped_column(String(24), nullable=False)
    error_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    raw_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    raw_path: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    artifact_manifest: Mapped[list[dict[str, Any]]] = mapped_column(JSON, nullable=False)
    content_type: Mapped[str | None] = mapped_column(String(128), nullable=True)
    raw_size_bytes: Mapped[int | None] = mapped_column(BIGINT, nullable=True)
    duration_ms: Mapped[int] = mapped_column(INTEGER, nullable=False)
    fetched_at: Mapped[datetime] = mapped_column(DATETIME, nullable=False)
    created_at: Mapped[datetime] = created_at_column()


class ListingRevision(Base):
    __tablename__ = "v2_listing_revision"
    __table_args__ = (
        UniqueConstraint(
            "source_listing_id",
            "revision_no",
            name="uq_v2_revision_listing_number",
        ),
        UniqueConstraint(
            "source_listing_id",
            "identity_fingerprint",
            name="uq_v2_revision_listing_identity",
        ),
        CheckConstraint(_enum_check("condition_code", ConditionCode), name="condition_code"),
        CheckConstraint(_enum_check("measure_type", MeasureType), name="measure_type"),
        CheckConstraint(_enum_check("quality_status", QualityStatus), name="quality_status"),
        CheckConstraint("revision_no > 0", name="revision_no"),
        CheckConstraint("quantity_value IS NULL OR quantity_value > 0", name="quantity_value"),
        CheckConstraint("quantity_min IS NULL OR quantity_min > 0", name="quantity_min"),
        CheckConstraint("quantity_max IS NULL OR quantity_max > 0", name="quantity_max"),
        CheckConstraint(
            "(quantity_min IS NULL AND quantity_max IS NULL) OR "
            "(quantity_min IS NOT NULL AND quantity_max IS NOT NULL)",
            name="quantity_range_pair",
        ),
        CheckConstraint(
            "quantity_value IS NULL OR (quantity_min IS NULL AND quantity_max IS NULL)",
            name="quantity_exact_or_range",
        ),
        CheckConstraint(
            "quantity_min IS NULL OR quantity_max >= quantity_min",
            name="quantity_range_order",
        ),
        CheckConstraint(
            "(quantity_value IS NULL AND quantity_min IS NULL AND quantity_max IS NULL "
            "AND quantity_unit IS NULL) OR "
            "((quantity_value IS NOT NULL OR quantity_min IS NOT NULL) "
            "AND quantity_unit IS NOT NULL)",
            name="quantity_unit_semantics",
        ),
        CheckConstraint(
            "base_quantity_value IS NULL OR base_quantity_value > 0",
            name="base_quantity_value",
        ),
        CheckConstraint(
            "base_quantity_min IS NULL OR base_quantity_min > 0",
            name="base_quantity_min",
        ),
        CheckConstraint(
            "base_quantity_max IS NULL OR base_quantity_max > 0",
            name="base_quantity_max",
        ),
        CheckConstraint(
            "(base_quantity_min IS NULL AND base_quantity_max IS NULL) OR "
            "(base_quantity_min IS NOT NULL AND base_quantity_max IS NOT NULL)",
            name="base_quantity_range_pair",
        ),
        CheckConstraint(
            "base_quantity_value IS NULL OR "
            "(base_quantity_min IS NULL AND base_quantity_max IS NULL)",
            name="base_quantity_exact_or_range",
        ),
        CheckConstraint(
            "base_quantity_min IS NULL OR base_quantity_max >= base_quantity_min",
            name="base_quantity_range_order",
        ),
        CheckConstraint(
            "(base_quantity_value IS NULL AND base_quantity_min IS NULL "
            "AND base_quantity_max IS NULL AND base_unit IS NULL) OR "
            "((base_quantity_value IS NOT NULL OR base_quantity_min IS NOT NULL) "
            "AND base_unit IS NOT NULL)",
            name="base_unit_semantics",
        ),
        CheckConstraint("package_count IS NULL OR package_count > 0", name="package_count"),
        CheckConstraint(
            "(quality_status = 'ACCEPTED' AND rejection_code IS NULL) OR "
            "(quality_status <> 'ACCEPTED' AND rejection_code IS NOT NULL)",
            name="quality_semantics",
        ),
        CheckConstraint("last_observed_at >= first_observed_at", name="observation_range"),
        Index("ix_v2_revision_listing_observed", "source_listing_id", "last_observed_at"),
    )

    id: Mapped[int] = mapped_column(BIGINT, primary_key=True, autoincrement=True)
    source_listing_id: Mapped[int] = mapped_column(
        BIGINT,
        ForeignKey("v2_source_listing.id", ondelete="RESTRICT"),
        nullable=False,
    )
    first_crawl_record_id: Mapped[int] = mapped_column(
        BIGINT,
        ForeignKey("v2_crawl_record.id", ondelete="RESTRICT"),
        nullable=False,
    )
    revision_no: Mapped[int] = mapped_column(INTEGER, nullable=False)
    source_title: Mapped[str] = mapped_column(String(512), nullable=False)
    source_category_path: Mapped[str | None] = mapped_column(String(512), nullable=True)
    source_attributes: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    normalized_attributes: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    condition_code: Mapped[str] = mapped_column(String(24), nullable=False)
    measure_type: Mapped[str] = mapped_column(String(16), nullable=False)
    quantity_value: Mapped[Decimal | None] = mapped_column(Numeric(20, 6), nullable=True)
    quantity_min: Mapped[Decimal | None] = mapped_column(Numeric(20, 6), nullable=True)
    quantity_max: Mapped[Decimal | None] = mapped_column(Numeric(20, 6), nullable=True)
    quantity_unit: Mapped[str | None] = mapped_column(String(16), nullable=True)
    base_quantity_value: Mapped[Decimal | None] = mapped_column(Numeric(20, 6), nullable=True)
    base_quantity_min: Mapped[Decimal | None] = mapped_column(Numeric(20, 6), nullable=True)
    base_quantity_max: Mapped[Decimal | None] = mapped_column(Numeric(20, 6), nullable=True)
    base_unit: Mapped[str | None] = mapped_column(String(16), nullable=True)
    package_count: Mapped[int | None] = mapped_column(INTEGER, nullable=True)
    identity_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    normalizer_version: Mapped[str] = mapped_column(String(64), nullable=False)
    quality_status: Mapped[str] = mapped_column(String(24), nullable=False)
    rejection_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    reviewed_by: Mapped[str | None] = mapped_column(String(64), nullable=True)
    reviewed_at: Mapped[datetime | None] = mapped_column(DATETIME, nullable=True)
    first_observed_at: Mapped[datetime] = mapped_column(DATETIME, nullable=False)
    last_observed_at: Mapped[datetime] = mapped_column(DATETIME, nullable=False)
    created_at: Mapped[datetime] = created_at_column()


class ListingMatch(Base):
    __tablename__ = "v2_listing_match"
    __table_args__ = (
        UniqueConstraint(
            "listing_revision_id",
            "item_variant_id",
            "matcher_version",
            name="uq_v2_match_revision_variant_version",
        ),
        CheckConstraint(_enum_check("match_status", MatchStatus), name="match_status"),
        CheckConstraint(_enum_check("match_method", MatchMethod), name="match_method"),
        CheckConstraint("confidence >= 0 AND confidence <= 1", name="confidence"),
        CheckConstraint(
            "effective_to IS NULL OR effective_to >= effective_from",
            name="effective_range",
        ),
        Index("ix_v2_match_variant_status", "item_variant_id", "match_status"),
        Index(
            "ix_v2_match_revision_status_effective",
            "listing_revision_id",
            "match_status",
            "effective_to",
        ),
    )

    id: Mapped[int] = mapped_column(BIGINT, primary_key=True, autoincrement=True)
    listing_revision_id: Mapped[int] = mapped_column(
        BIGINT,
        ForeignKey("v2_listing_revision.id", ondelete="RESTRICT"),
        nullable=False,
    )
    item_variant_id: Mapped[int] = mapped_column(
        BIGINT,
        ForeignKey("v2_item_variant.id", ondelete="RESTRICT"),
        nullable=False,
    )
    match_status: Mapped[str] = mapped_column(String(24), nullable=False)
    match_method: Mapped[str] = mapped_column(String(24), nullable=False)
    confidence: Mapped[Decimal] = mapped_column(Numeric(5, 4), nullable=False)
    matched_fields: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    mismatch_fields: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    matcher_version: Mapped[str] = mapped_column(String(64), nullable=False)
    effective_from: Mapped[datetime] = mapped_column(DATETIME, nullable=False)
    effective_to: Mapped[datetime | None] = mapped_column(DATETIME, nullable=True)
    reviewed_by: Mapped[str | None] = mapped_column(String(64), nullable=True)
    reviewed_at: Mapped[datetime | None] = mapped_column(DATETIME, nullable=True)
    created_at: Mapped[datetime] = created_at_column()


class CatalogPriceObservationRecord(Base):
    __tablename__ = "v2_price_observation"
    __table_args__ = (
        CheckConstraint(_enum_check("region_scope", RegionScope), name="region_scope"),
        CheckConstraint("region_scope <> 'MULTI'", name="single_region"),
        CheckConstraint("currency = 'CNY'", name="currency"),
        CheckConstraint(
            _enum_check("original_price_type", OriginalPriceType),
            name="original_price_type",
        ),
        CheckConstraint(_enum_check("price_nature", PriceNature), name="price_nature"),
        CheckConstraint(_enum_check("price_type", PriceType), name="price_type"),
        CheckConstraint(_enum_check("pricing_basis", PricingBasis), name="pricing_basis"),
        CheckConstraint(_enum_check("availability", Availability), name="availability"),
        CheckConstraint(_enum_check("fee_status", FeeStatus), name="fee_status"),
        CheckConstraint(_enum_check("quality_status", QualityStatus), name="quality_status"),
        CheckConstraint("original_price IS NULL OR original_price > 0", name="original_price"),
        CheckConstraint("current_price IS NULL OR current_price > 0", name="current_price"),
        CheckConstraint("unit_price IS NULL OR unit_price > 0", name="unit_price"),
        CheckConstraint(
            "(original_price IS NULL AND original_price_type = 'NONE') OR "
            "(original_price IS NOT NULL AND original_price_type <> 'NONE')",
            name="original_price_semantics",
        ),
        CheckConstraint(
            "(unit_price IS NULL AND unit_price_unit IS NULL) OR "
            "(unit_price IS NOT NULL AND unit_price_unit IS NOT NULL)",
            name="unit_price_semantics",
        ),
        CheckConstraint(
            "(quality_status = 'ACCEPTED' AND rejection_code IS NULL) OR "
            "(quality_status <> 'ACCEPTED' AND rejection_code IS NOT NULL)",
            name="quality_semantics",
        ),
        CheckConstraint(
            "quality_status <> 'ACCEPTED' OR "
            "(current_price IS NOT NULL AND region_scope <> 'UNKNOWN' "
            "AND price_nature <> 'UNKNOWN' "
            "AND pricing_basis IN ('PACKAGE_TOTAL','UNIT_QUOTED') "
            "AND (pricing_basis <> 'UNIT_QUOTED' OR unit_price IS NOT NULL) "
            "AND ((price_nature IN ('RETAIL_OFFER','WHOLESALE_OFFER') "
            "AND price_type = 'DIRECT_UNCONDITIONAL' "
            "AND fee_status IN ('ITEM_ONLY','SEPARATE_FEES_EXCLUDED')) "
            "OR (price_nature IN ('RETAIL_AVERAGE','WHOLESALE_AVERAGE','MARKET_AVERAGE') "
            "AND price_type = 'PUBLISHED_VALUE' "
            "AND fee_status = 'NOT_APPLICABLE')))",
            name="accepted_eligibility",
        ),
        CheckConstraint(
            "region_scope <> 'NATIONAL' OR region_code = 'CN'",
            name="national_region",
        ),
        Index(
            "ix_v2_price_listing_region_observed",
            "source_listing_id",
            "region_scope",
            "region_code",
            "observed_at",
        ),
        Index("ix_v2_price_revision_observed", "listing_revision_id", "observed_at"),
        Index("ix_v2_price_quality_observed", "quality_status", "observed_at"),
        Index("ix_v2_price_crawl_record", "crawl_record_id"),
        Index("ix_v2_price_supersedes", "supersedes_observation_id"),
        Index("ix_v2_price_observed_at", "observed_at"),
    )

    id: Mapped[int] = mapped_column(BIGINT, primary_key=True, autoincrement=True)
    observation_key: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    supersedes_observation_id: Mapped[int | None] = mapped_column(
        BIGINT,
        ForeignKey(
            "v2_price_observation.id",
            name="fk_v2_price_observation_supersedes",
            ondelete="RESTRICT",
        ),
        nullable=True,
    )
    source_listing_id: Mapped[int] = mapped_column(
        BIGINT,
        ForeignKey("v2_source_listing.id", ondelete="RESTRICT"),
        nullable=False,
    )
    listing_revision_id: Mapped[int] = mapped_column(
        BIGINT,
        ForeignKey("v2_listing_revision.id", ondelete="RESTRICT"),
        nullable=False,
    )
    crawl_record_id: Mapped[int] = mapped_column(
        BIGINT,
        ForeignKey("v2_crawl_record.id", ondelete="RESTRICT"),
        nullable=False,
    )
    region_scope: Mapped[str] = mapped_column(String(24), nullable=False)
    region_code: Mapped[str] = mapped_column(String(32), nullable=False)
    currency: Mapped[str] = mapped_column(String(3), nullable=False, server_default=text("'CNY'"))
    original_price: Mapped[Decimal | None] = mapped_column(Numeric(18, 2), nullable=True)
    original_price_type: Mapped[str] = mapped_column(String(32), nullable=False)
    current_price: Mapped[Decimal | None] = mapped_column(Numeric(18, 2), nullable=True)
    price_nature: Mapped[str] = mapped_column(String(32), nullable=False)
    price_type: Mapped[str] = mapped_column(String(32), nullable=False)
    pricing_basis: Mapped[str] = mapped_column(String(24), nullable=False)
    promotion_label: Mapped[str | None] = mapped_column(String(128), nullable=True)
    availability: Mapped[str] = mapped_column(String(24), nullable=False)
    unit_price: Mapped[Decimal | None] = mapped_column(Numeric(20, 6), nullable=True)
    unit_price_unit: Mapped[str | None] = mapped_column(String(16), nullable=True)
    fee_status: Mapped[str] = mapped_column(String(32), nullable=False)
    quality_status: Mapped[str] = mapped_column(String(24), nullable=False)
    rejection_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    reviewed_by: Mapped[str | None] = mapped_column(String(64), nullable=True)
    reviewed_at: Mapped[datetime | None] = mapped_column(DATETIME, nullable=True)
    displayed_price_text: Mapped[str | None] = mapped_column(String(255), nullable=True)
    source_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    observed_at: Mapped[datetime] = mapped_column(DATETIME, nullable=False)
    created_at: Mapped[datetime] = created_at_column()


class CatalogPriceCurrent(Base):
    __tablename__ = "v2_price_current"
    __table_args__ = (
        UniqueConstraint(
            "source_listing_id",
            "region_scope",
            "region_code",
            name="uq_v2_current_listing_region",
        ),
        CheckConstraint(_enum_check("region_scope", RegionScope), name="region_scope"),
        CheckConstraint("region_scope NOT IN ('UNKNOWN','MULTI')", name="known_region"),
        CheckConstraint(
            "region_scope <> 'NATIONAL' OR region_code = 'CN'",
            name="national_region",
        ),
        Index(
            "ix_v2_current_revision_region",
            "listing_revision_id",
            "region_scope",
            "region_code",
        ),
        Index("ix_v2_current_region_observed", "region_scope", "region_code", "observed_at"),
    )

    id: Mapped[int] = mapped_column(BIGINT, primary_key=True, autoincrement=True)
    source_listing_id: Mapped[int] = mapped_column(
        BIGINT,
        ForeignKey("v2_source_listing.id", ondelete="RESTRICT"),
        nullable=False,
    )
    listing_revision_id: Mapped[int] = mapped_column(
        BIGINT,
        ForeignKey("v2_listing_revision.id", ondelete="RESTRICT"),
        nullable=False,
    )
    region_scope: Mapped[str] = mapped_column(String(24), nullable=False)
    region_code: Mapped[str] = mapped_column(String(32), nullable=False)
    price_observation_id: Mapped[int] = mapped_column(
        BIGINT,
        ForeignKey("v2_price_observation.id", ondelete="RESTRICT"),
        nullable=False,
        unique=True,
    )
    observed_at: Mapped[datetime] = mapped_column(DATETIME, nullable=False)
    created_at: Mapped[datetime] = created_at_column()
    updated_at: Mapped[datetime] = updated_at_column()
