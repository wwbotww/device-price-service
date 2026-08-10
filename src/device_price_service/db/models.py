from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    CheckConstraint,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    SmallInteger,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.mysql import DATETIME
from sqlalchemy.orm import Mapped, mapped_column

from device_price_service.db.base import Base


def created_at_column() -> Mapped[datetime]:
    return mapped_column(
        DATETIME(fsp=3),
        nullable=False,
        server_default=text("CURRENT_TIMESTAMP(3)"),
    )


def updated_at_column() -> Mapped[datetime]:
    return mapped_column(
        DATETIME(fsp=3),
        nullable=False,
        server_default=text("CURRENT_TIMESTAMP(3)"),
        onupdate=func.current_timestamp(),
    )


class Brand(Base):
    __tablename__ = "brand"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    code: Mapped[str] = mapped_column(String(32), nullable=False, unique=True)
    name_zh: Mapped[str] = mapped_column(String(64), nullable=False)
    name_en: Mapped[str] = mapped_column(String(64), nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("1"))
    created_at: Mapped[datetime] = created_at_column()
    updated_at: Mapped[datetime] = updated_at_column()


class Category(Base):
    __tablename__ = "category"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    parent_id: Mapped[int | None] = mapped_column(
        BigInteger,
        ForeignKey("category.id", ondelete="RESTRICT"),
        nullable=True,
    )
    code: Mapped[str] = mapped_column(String(32), nullable=False, unique=True)
    name_zh: Mapped[str] = mapped_column(String(64), nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("1"))
    created_at: Mapped[datetime] = created_at_column()
    updated_at: Mapped[datetime] = updated_at_column()


class Product(Base):
    __tablename__ = "product"
    __table_args__ = (
        UniqueConstraint(
            "brand_id", "official_product_id", name="uq_product_brand_official_product"
        ),
        CheckConstraint(
            "lifecycle_status IN ('ACTIVE','INACTIVE','UNKNOWN')",
            name="lifecycle_status",
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    brand_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("brand.id", ondelete="RESTRICT"),
        nullable=False,
    )
    category_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("category.id", ondelete="RESTRICT"),
        nullable=False,
    )
    official_product_id: Mapped[str] = mapped_column(String(128), nullable=False)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    series_name: Mapped[str | None] = mapped_column(String(128), nullable=True)
    model_number: Mapped[str | None] = mapped_column(String(128), nullable=True)
    official_url: Mapped[str] = mapped_column(String(1024), nullable=False)
    lifecycle_status: Mapped[str] = mapped_column(
        String(32), nullable=False, server_default=text("'UNKNOWN'")
    )
    first_seen_at: Mapped[datetime] = mapped_column(DATETIME(fsp=3), nullable=False)
    last_seen_at: Mapped[datetime] = mapped_column(DATETIME(fsp=3), nullable=False)
    created_at: Mapped[datetime] = created_at_column()
    updated_at: Mapped[datetime] = updated_at_column()


class Sku(Base):
    __tablename__ = "sku"
    __table_args__ = (
        UniqueConstraint("product_id", "official_sku_id", name="uq_sku_product_official_sku"),
        UniqueConstraint("product_id", "spec_fingerprint", name="uq_sku_product_spec_fingerprint"),
        CheckConstraint("status IN ('ACTIVE','INACTIVE','UNKNOWN')", name="status"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    product_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("product.id", ondelete="RESTRICT"),
        nullable=False,
    )
    official_sku_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    color: Mapped[str | None] = mapped_column(String(128), nullable=True)
    capacity: Mapped[str | None] = mapped_column(String(64), nullable=True)
    memory: Mapped[str | None] = mapped_column(String(64), nullable=True)
    connectivity: Mapped[str | None] = mapped_column(String(64), nullable=True)
    size: Mapped[str | None] = mapped_column(String(64), nullable=True)
    attributes: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    spec_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(
        String(32), nullable=False, server_default=text("'UNKNOWN'")
    )
    first_seen_at: Mapped[datetime] = mapped_column(DATETIME(fsp=3), nullable=False)
    last_seen_at: Mapped[datetime] = mapped_column(DATETIME(fsp=3), nullable=False)
    created_at: Mapped[datetime] = created_at_column()
    updated_at: Mapped[datetime] = updated_at_column()


class SalesChannel(Base):
    __tablename__ = "sales_channel"
    __table_args__ = (
        CheckConstraint("region_code = 'CN'", name="region_code"),
        CheckConstraint("currency = 'CNY'", name="currency"),
        CheckConstraint("seller_type = 'OFFICIAL_DIRECT'", name="seller_type"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    brand_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("brand.id", ondelete="RESTRICT"),
        nullable=False,
    )
    code: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    base_url: Mapped[str] = mapped_column(String(512), nullable=False)
    allowed_domains: Mapped[list[str]] = mapped_column(JSON, nullable=False)
    region_code: Mapped[str] = mapped_column(String(2), nullable=False, server_default=text("'CN'"))
    currency: Mapped[str] = mapped_column(String(3), nullable=False, server_default=text("'CNY'"))
    seller_type: Mapped[str] = mapped_column(
        String(32), nullable=False, server_default=text("'OFFICIAL_DIRECT'")
    )
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("1"))
    created_at: Mapped[datetime] = created_at_column()
    updated_at: Mapped[datetime] = updated_at_column()


class CrawlRun(Base):
    __tablename__ = "crawl_run"
    __table_args__ = (
        CheckConstraint("run_type IN ('DISCOVERY','PRICE','FULL','REPLAY')", name="run_type"),
        CheckConstraint("trigger_type IN ('SCHEDULED','MANUAL')", name="trigger_type"),
        CheckConstraint(
            "status IN ('RUNNING','SUCCEEDED','PARTIAL','FAILED','CANCELLED')",
            name="status",
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    channel_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("sales_channel.id", ondelete="RESTRICT"),
        nullable=False,
    )
    run_type: Mapped[str] = mapped_column(String(32), nullable=False)
    trigger_type: Mapped[str] = mapped_column(String(32), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    adapter_version: Mapped[str] = mapped_column(String(64), nullable=False)
    started_at: Mapped[datetime] = mapped_column(DATETIME(fsp=3), nullable=False)
    finished_at: Mapped[datetime | None] = mapped_column(DATETIME(fsp=3), nullable=True)
    discovered_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    success_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    skipped_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    failed_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    error_summary: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = created_at_column()


class OfficialOffer(Base):
    __tablename__ = "official_offer"
    __table_args__ = (
        UniqueConstraint("sku_id", "channel_id", name="uq_offer_sku_channel"),
        UniqueConstraint("channel_id", "official_offer_id", name="uq_offer_channel_official_offer"),
        CheckConstraint(
            "availability IN "
            "('ON_SALE','OUT_OF_STOCK','RESERVATION','PRE_SALE','COMING_SOON',"
            "'OFF_SHELF','UNKNOWN')",
            name="availability",
        ),
        CheckConstraint("consecutive_misses >= 0", name="consecutive_misses"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    sku_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("sku.id", ondelete="RESTRICT"),
        nullable=False,
    )
    channel_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("sales_channel.id", ondelete="RESTRICT"),
        nullable=False,
    )
    official_offer_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    source_url: Mapped[str] = mapped_column(String(1024), nullable=False)
    availability: Mapped[str] = mapped_column(
        String(32), nullable=False, server_default=text("'UNKNOWN'")
    )
    consecutive_misses: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("0")
    )
    first_seen_at: Mapped[datetime] = mapped_column(DATETIME(fsp=3), nullable=False)
    last_seen_at: Mapped[datetime] = mapped_column(DATETIME(fsp=3), nullable=False)
    created_at: Mapped[datetime] = created_at_column()
    updated_at: Mapped[datetime] = updated_at_column()


class PriceCurrent(Base):
    __tablename__ = "price_current"
    __table_args__ = (
        CheckConstraint("currency = 'CNY'", name="currency"),
        CheckConstraint("original_price IS NULL OR original_price > 0", name="original_price"),
        CheckConstraint("current_price IS NULL OR current_price > 0", name="current_price"),
        CheckConstraint(
            "original_price_type IN ('CROSSED_OUT','MSRP','EXPLICIT_ORIGINAL','NONE')",
            name="original_price_type",
        ),
        CheckConstraint(
            "(original_price IS NULL AND original_price_type = 'NONE') OR "
            "(original_price IS NOT NULL AND original_price_type <> 'NONE')",
            name="original_price_semantics",
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    offer_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("official_offer.id", ondelete="RESTRICT"),
        nullable=False,
        unique=True,
    )
    currency: Mapped[str] = mapped_column(String(3), nullable=False, server_default=text("'CNY'"))
    original_price: Mapped[Decimal | None] = mapped_column(Numeric(12, 2), nullable=True)
    original_price_type: Mapped[str] = mapped_column(
        String(32), nullable=False, server_default=text("'NONE'")
    )
    current_price: Mapped[Decimal | None] = mapped_column(Numeric(12, 2), nullable=True)
    observed_at: Mapped[datetime] = mapped_column(DATETIME(fsp=3), nullable=False)
    source_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    crawl_run_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("crawl_run.id", ondelete="RESTRICT"),
        nullable=False,
    )
    created_at: Mapped[datetime] = created_at_column()
    updated_at: Mapped[datetime] = updated_at_column()


class PriceHistory(Base):
    __tablename__ = "price_history"
    __table_args__ = (
        CheckConstraint("currency = 'CNY'", name="currency"),
        CheckConstraint("original_price IS NULL OR original_price > 0", name="original_price"),
        CheckConstraint("current_price IS NULL OR current_price > 0", name="current_price"),
        CheckConstraint("valid_to IS NULL OR valid_to >= valid_from", name="valid_range"),
        CheckConstraint("last_observed_at >= valid_from", name="last_observed_at"),
        CheckConstraint(
            "original_price_type IN ('CROSSED_OUT','MSRP','EXPLICIT_ORIGINAL','NONE')",
            name="original_price_type",
        ),
        CheckConstraint(
            "(original_price IS NULL AND original_price_type = 'NONE') OR "
            "(original_price IS NOT NULL AND original_price_type <> 'NONE')",
            name="original_price_semantics",
        ),
        Index("ix_price_history_offer_valid_from", "offer_id", "valid_from"),
        Index("ix_price_history_offer_valid_to", "offer_id", "valid_to"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    offer_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("official_offer.id", ondelete="RESTRICT"),
        nullable=False,
    )
    currency: Mapped[str] = mapped_column(String(3), nullable=False, server_default=text("'CNY'"))
    original_price: Mapped[Decimal | None] = mapped_column(Numeric(12, 2), nullable=True)
    original_price_type: Mapped[str] = mapped_column(String(32), nullable=False)
    current_price: Mapped[Decimal | None] = mapped_column(Numeric(12, 2), nullable=True)
    availability: Mapped[str] = mapped_column(String(32), nullable=False)
    valid_from: Mapped[datetime] = mapped_column(DATETIME(fsp=3), nullable=False)
    last_observed_at: Mapped[datetime] = mapped_column(DATETIME(fsp=3), nullable=False)
    valid_to: Mapped[datetime | None] = mapped_column(DATETIME(fsp=3), nullable=True)
    source_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    crawl_run_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("crawl_run.id", ondelete="RESTRICT"),
        nullable=False,
    )
    created_at: Mapped[datetime] = created_at_column()


class CrawlRecord(Base):
    __tablename__ = "crawl_record"
    __table_args__ = (
        Index("ix_crawl_record_run_parse_status", "crawl_run_id", "parse_status"),
        Index("ix_crawl_record_entity", "entity_type", "entity_key"),
        Index("ix_crawl_record_fetched_at", "fetched_at"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    crawl_run_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("crawl_run.id", ondelete="RESTRICT"),
        nullable=False,
    )
    entity_type: Mapped[str] = mapped_column(String(32), nullable=False)
    entity_key: Mapped[str] = mapped_column(String(255), nullable=False)
    request_url: Mapped[str] = mapped_column(String(1024), nullable=False)
    final_url: Mapped[str] = mapped_column(String(1024), nullable=False)
    fetch_method: Mapped[str] = mapped_column(String(32), nullable=False)
    http_status: Mapped[int | None] = mapped_column(SmallInteger, nullable=True)
    fetch_status: Mapped[str] = mapped_column(String(32), nullable=False)
    parse_status: Mapped[str] = mapped_column(String(32), nullable=False)
    error_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    raw_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    raw_path: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    duration_ms: Mapped[int] = mapped_column(Integer, nullable=False)
    fetched_at: Mapped[datetime] = mapped_column(DATETIME(fsp=3), nullable=False)
    created_at: Mapped[datetime] = created_at_column()
