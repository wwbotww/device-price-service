"""enforce V2 constraints on MySQL versions that ignore CHECK

Revision ID: 96524222b3ec
Revises: 0aafbcfc19d5
Create Date: 2026-08-21 10:46:48.586882
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "96524222b3ec"
down_revision: str | None = "0aafbcfc19d5"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

CHECK_ENFORCEMENT_VERSION = (8, 0, 16)
Validation = tuple[str, str]

ENUM_COLUMNS: dict[str, dict[str, tuple[str, ...]]] = {
    "v2_brand": {"status": ("ACTIVE", "INACTIVE", "UNKNOWN")},
    "v2_category": {
        "default_measure_type": ("WEIGHT", "VOLUME", "COUNT", "LENGTH", "AREA", "SET", "OTHER")
    },
    "v2_catalog_item": {
        "item_type": ("MODEL", "COMMODITY", "GENERIC_GOOD"),
        "record_origin": ("MANUAL", "RULE", "IMPORT", "AUTO"),
        "status": ("ACTIVE", "INACTIVE", "REVIEW_REQUIRED", "SUPERSEDED"),
    },
    "v2_item_variant": {
        "condition_code": ("NEW", "USED", "REFURBISHED", "UNKNOWN"),
        "measure_type": ("WEIGHT", "VOLUME", "COUNT", "LENGTH", "AREA", "SET", "OTHER"),
        "status": ("ACTIVE", "INACTIVE", "REVIEW_REQUIRED", "SUPERSEDED"),
    },
    "v2_source_channel": {
        "source_type": (
            "OFFICIAL_MALL",
            "MAJOR_ECOMMERCE",
            "SUPERMARKET",
            "PERSONAL_SITE",
            "PUBLIC_DATA",
        ),
        "business_mode": ("SELF_OPERATED", "MARKETPLACE", "HYBRID", "WHOLESALE"),
        "access_mode": ("API", "HTTP", "BROWSER", "FILE", "MIXED"),
        "region_mode": ("NATIONAL", "REGIONAL", "MIXED"),
    },
    "v2_merchant": {
        "seller_type": (
            "PLATFORM_SELF",
            "BRAND_OFFICIAL",
            "THIRD_PARTY",
            "INDIVIDUAL",
            "PUBLIC_MARKET",
            "UNKNOWN",
        ),
        "verification_status": ("VERIFIED", "UNVERIFIED", "UNKNOWN"),
        "status": ("ACTIVE", "INACTIVE", "UNKNOWN"),
    },
    "v2_source_listing": {
        "price_nature": (
            "RETAIL_OFFER",
            "WHOLESALE_OFFER",
            "RETAIL_AVERAGE",
            "WHOLESALE_AVERAGE",
            "MARKET_AVERAGE",
            "UNKNOWN",
        ),
        "lifecycle_status": ("ACTIVE", "INACTIVE", "UNKNOWN"),
    },
    "v2_crawl_run": {
        "region_scope": (
            "NATIONAL",
            "PROVINCE",
            "CITY",
            "DISTRICT",
            "DELIVERY_ZONE",
            "UNKNOWN",
            "MULTI",
        ),
        "run_type": ("DISCOVERY", "PRICE", "FULL", "REPLAY"),
        "trigger_type": ("SCHEDULED", "MANUAL", "QUERY_RECRAWL"),
        "status": ("RUNNING", "SUCCEEDED", "PARTIAL", "FAILED", "CANCELLED"),
    },
    "v2_crawl_record": {
        "entity_type": ("CATEGORY", "SEARCH", "LISTING", "SKU", "OFFER", "PUBLIC_PRICE"),
        "fetch_method": ("API", "HTTP", "BROWSER", "FILE", "REPLAY"),
        "fetch_status": ("PENDING", "SUCCEEDED", "SKIPPED", "FAILED"),
        "parse_status": ("PENDING", "SUCCEEDED", "SKIPPED", "FAILED"),
        "validation_status": ("PENDING", "SUCCEEDED", "SKIPPED", "FAILED"),
    },
    "v2_listing_revision": {
        "condition_code": ("NEW", "USED", "REFURBISHED", "UNKNOWN"),
        "measure_type": ("WEIGHT", "VOLUME", "COUNT", "LENGTH", "AREA", "SET", "OTHER"),
        "quality_status": ("ACCEPTED", "REVIEW_REQUIRED", "REJECTED"),
    },
    "v2_listing_match": {
        "match_status": ("CANDIDATE", "ACCEPTED", "REJECTED", "REVIEW_REQUIRED", "SUPERSEDED"),
        "match_method": ("GTIN", "MODEL", "RULE", "MANUAL", "ML"),
    },
    "v2_price_observation": {
        "region_scope": (
            "NATIONAL",
            "PROVINCE",
            "CITY",
            "DISTRICT",
            "DELIVERY_ZONE",
            "UNKNOWN",
        ),
        "original_price_type": ("CROSSED_OUT", "MSRP", "EXPLICIT_ORIGINAL", "NONE"),
        "price_nature": (
            "RETAIL_OFFER",
            "WHOLESALE_OFFER",
            "RETAIL_AVERAGE",
            "WHOLESALE_AVERAGE",
            "MARKET_AVERAGE",
            "UNKNOWN",
        ),
        "price_type": (
            "DIRECT_UNCONDITIONAL",
            "PUBLISHED_VALUE",
            "MEMBER",
            "COUPON",
            "SUBSIDY",
            "STARTING",
            "INSTALLMENT",
            "DEPOSIT",
            "BUNDLE",
            "UNKNOWN",
        ),
        "pricing_basis": ("PACKAGE_TOTAL", "UNIT_QUOTED", "VARIABLE_ESTIMATE", "UNKNOWN"),
        "availability": (
            "ON_SALE",
            "OUT_OF_STOCK",
            "RESERVATION",
            "PRE_SALE",
            "COMING_SOON",
            "OFF_SHELF",
            "UNKNOWN",
        ),
        "fee_status": (
            "ITEM_ONLY",
            "SEPARATE_FEES_EXCLUDED",
            "NOT_APPLICABLE",
            "INSEPARABLE",
            "UNKNOWN",
        ),
        "quality_status": ("ACCEPTED", "REVIEW_REQUIRED", "REJECTED"),
    },
    "v2_price_current": {
        "region_scope": ("NATIONAL", "PROVINCE", "CITY", "DISTRICT", "DELIVERY_ZONE")
    },
}

NULLABLE_ENUMS = {("v2_category", "default_measure_type")}

CUSTOM_VALIDATIONS: dict[str, tuple[Validation, ...]] = {
    "v2_category": (("NEW.level < 0", "category level cannot be negative"),),
    "v2_item_variant": (
        (
            "(NEW.quantity_value IS NOT NULL AND NEW.quantity_value <= 0) OR "
            "(NEW.quantity_min IS NOT NULL AND NEW.quantity_min <= 0) OR "
            "(NEW.quantity_max IS NOT NULL AND NEW.quantity_max <= 0)",
            "variant quantities must be positive",
        ),
        (
            "(NEW.quantity_min IS NULL AND NEW.quantity_max IS NOT NULL) OR "
            "(NEW.quantity_min IS NOT NULL AND NEW.quantity_max IS NULL)",
            "variant quantity range must be paired",
        ),
        (
            "NEW.quantity_value IS NOT NULL AND "
            "(NEW.quantity_min IS NOT NULL OR NEW.quantity_max IS NOT NULL)",
            "variant quantity cannot be exact and range",
        ),
        (
            "NEW.quantity_min IS NOT NULL AND NEW.quantity_max < NEW.quantity_min",
            "invalid variant quantity range",
        ),
        (
            "NEW.package_count IS NOT NULL AND NEW.package_count <= 0",
            "variant package count must be positive",
        ),
    ),
    "v2_source_channel": (("NEW.currency <> 'CNY'", "source currency must be CNY"),),
    "v2_merchant": (
        ("NEW.last_seen_at < NEW.first_seen_at", "invalid merchant observation range"),
    ),
    "v2_source_listing": (
        ("NEW.consecutive_misses < 0", "listing misses cannot be negative"),
        ("NEW.last_seen_at < NEW.first_seen_at", "invalid listing observation range"),
        (
            "NOT EXISTS (SELECT 1 FROM v2_merchant m "
            "WHERE m.id = NEW.merchant_id AND m.source_channel_id = NEW.source_channel_id)",
            "listing merchant belongs to another source",
        ),
        (
            "NEW.current_revision_id IS NOT NULL AND NOT EXISTS "
            "(SELECT 1 FROM v2_listing_revision r WHERE r.id = NEW.current_revision_id "
            "AND r.source_listing_id = NEW.id AND r.quality_status = 'ACCEPTED')",
            "listing current revision is not accepted",
        ),
    ),
    "v2_crawl_run": (
        (
            "(NEW.region_scope = 'NATIONAL' AND NEW.region_code <> 'CN') OR "
            "(NEW.region_scope = 'MULTI' AND NEW.region_code <> '*')",
            "invalid crawl run region semantics",
        ),
        (
            "NEW.finished_at IS NOT NULL AND NEW.finished_at < NEW.started_at",
            "invalid crawl run time range",
        ),
        (
            "NEW.discovered_count < 0 OR NEW.fetched_count < 0 OR NEW.accepted_count < 0 "
            "OR NEW.review_count < 0 OR NEW.rejected_count < 0 OR NEW.failed_count < 0",
            "crawl run counts cannot be negative",
        ),
    ),
    "v2_crawl_record": (
        (
            "NEW.http_status IS NOT NULL AND (NEW.http_status < 100 OR NEW.http_status > 599)",
            "invalid crawl record HTTP status",
        ),
        (
            "NEW.raw_size_bytes IS NOT NULL AND NEW.raw_size_bytes < 0",
            "crawl record raw size cannot be negative",
        ),
        ("NEW.duration_ms < 0", "crawl record duration cannot be negative"),
    ),
    "v2_listing_revision": (
        ("NEW.revision_no <= 0", "listing revision number must be positive"),
        (
            "(NEW.quantity_value IS NOT NULL AND NEW.quantity_value <= 0) OR "
            "(NEW.quantity_min IS NOT NULL AND NEW.quantity_min <= 0) OR "
            "(NEW.quantity_max IS NOT NULL AND NEW.quantity_max <= 0) OR "
            "(NEW.base_quantity_value IS NOT NULL AND NEW.base_quantity_value <= 0) OR "
            "(NEW.base_quantity_min IS NOT NULL AND NEW.base_quantity_min <= 0) OR "
            "(NEW.base_quantity_max IS NOT NULL AND NEW.base_quantity_max <= 0)",
            "listing revision quantities must be positive",
        ),
        (
            "(NEW.quantity_min IS NULL AND NEW.quantity_max IS NOT NULL) OR "
            "(NEW.quantity_min IS NOT NULL AND NEW.quantity_max IS NULL) OR "
            "(NEW.base_quantity_min IS NULL AND NEW.base_quantity_max IS NOT NULL) OR "
            "(NEW.base_quantity_min IS NOT NULL AND NEW.base_quantity_max IS NULL)",
            "listing revision quantity ranges must be paired",
        ),
        (
            "(NEW.quantity_value IS NOT NULL AND "
            "(NEW.quantity_min IS NOT NULL OR NEW.quantity_max IS NOT NULL)) OR "
            "(NEW.base_quantity_value IS NOT NULL AND "
            "(NEW.base_quantity_min IS NOT NULL OR NEW.base_quantity_max IS NOT NULL))",
            "listing revision quantity cannot be exact and range",
        ),
        (
            "(NEW.quantity_min IS NOT NULL AND NEW.quantity_max < NEW.quantity_min) OR "
            "(NEW.base_quantity_min IS NOT NULL AND "
            "NEW.base_quantity_max < NEW.base_quantity_min)",
            "invalid listing revision quantity range",
        ),
        (
            "(NEW.quantity_value IS NULL AND NEW.quantity_min IS NULL "
            "AND NEW.quantity_max IS NULL AND NEW.quantity_unit IS NOT NULL) OR "
            "((NEW.quantity_value IS NOT NULL OR NEW.quantity_min IS NOT NULL) "
            "AND NEW.quantity_unit IS NULL) OR "
            "(NEW.base_quantity_value IS NULL AND NEW.base_quantity_min IS NULL "
            "AND NEW.base_quantity_max IS NULL AND NEW.base_unit IS NOT NULL) OR "
            "((NEW.base_quantity_value IS NOT NULL OR NEW.base_quantity_min IS NOT NULL) "
            "AND NEW.base_unit IS NULL)",
            "listing revision quantity units are inconsistent",
        ),
        (
            "NEW.package_count IS NOT NULL AND NEW.package_count <= 0",
            "listing revision package count must be positive",
        ),
        (
            "(NEW.quality_status = 'ACCEPTED' AND NEW.rejection_code IS NOT NULL) OR "
            "(NEW.quality_status <> 'ACCEPTED' AND NEW.rejection_code IS NULL)",
            "invalid listing revision quality semantics",
        ),
        (
            "NEW.last_observed_at < NEW.first_observed_at",
            "invalid listing revision observation range",
        ),
        (
            "EXISTS (SELECT 1 FROM v2_crawl_record cr "
            "WHERE cr.id = NEW.first_crawl_record_id "
            "AND cr.source_listing_id IS NOT NULL "
            "AND cr.source_listing_id <> NEW.source_listing_id)",
            "listing revision evidence belongs to another listing",
        ),
    ),
    "v2_listing_match": (
        (
            "NEW.confidence < 0 OR NEW.confidence > 1",
            "listing match confidence must be between zero and one",
        ),
        (
            "NEW.effective_to IS NOT NULL AND NEW.effective_to < NEW.effective_from",
            "invalid listing match effective range",
        ),
        (
            "NEW.match_status = 'ACCEPTED' AND NEW.effective_to IS NULL AND EXISTS "
            "(SELECT 1 FROM v2_listing_match m "
            "WHERE m.listing_revision_id = NEW.listing_revision_id "
            "AND m.match_status = 'ACCEPTED' AND m.effective_to IS NULL "
            "AND m.id <> NEW.id)",
            "listing revision already has an accepted match",
        ),
    ),
    "v2_price_observation": (
        (
            "NEW.region_scope = 'NATIONAL' AND NEW.region_code <> 'CN'",
            "national price observation must use CN",
        ),
        ("NEW.currency <> 'CNY'", "price observation currency must be CNY"),
        (
            "(NEW.original_price IS NOT NULL AND NEW.original_price <= 0) OR "
            "(NEW.current_price IS NOT NULL AND NEW.current_price <= 0) OR "
            "(NEW.unit_price IS NOT NULL AND NEW.unit_price <= 0)",
            "price amounts must be positive",
        ),
        (
            "(NEW.original_price IS NULL AND NEW.original_price_type <> 'NONE') OR "
            "(NEW.original_price IS NOT NULL AND NEW.original_price_type = 'NONE')",
            "invalid original price semantics",
        ),
        (
            "(NEW.unit_price IS NULL AND NEW.unit_price_unit IS NOT NULL) OR "
            "(NEW.unit_price IS NOT NULL AND NEW.unit_price_unit IS NULL)",
            "invalid unit price semantics",
        ),
        (
            "(NEW.quality_status = 'ACCEPTED' AND NEW.rejection_code IS NOT NULL) OR "
            "(NEW.quality_status <> 'ACCEPTED' AND NEW.rejection_code IS NULL)",
            "invalid price quality semantics",
        ),
        (
            "NEW.quality_status = 'ACCEPTED' AND "
            "(NEW.current_price IS NULL OR NEW.region_scope = 'UNKNOWN' "
            "OR NEW.price_nature = 'UNKNOWN' "
            "OR NEW.pricing_basis NOT IN ('PACKAGE_TOTAL','UNIT_QUOTED') "
            "OR (NEW.pricing_basis = 'UNIT_QUOTED' AND NEW.unit_price IS NULL) "
            "OR (NEW.price_nature IN ('RETAIL_OFFER','WHOLESALE_OFFER') AND "
            "(NEW.price_type <> 'DIRECT_UNCONDITIONAL' OR NEW.fee_status NOT IN "
            "('ITEM_ONLY','SEPARATE_FEES_EXCLUDED'))) "
            "OR (NEW.price_nature IN "
            "('RETAIL_AVERAGE','WHOLESALE_AVERAGE','MARKET_AVERAGE') AND "
            "(NEW.price_type <> 'PUBLISHED_VALUE' "
            "OR NEW.fee_status <> 'NOT_APPLICABLE')))",
            "accepted price is not eligible for current projection",
        ),
        (
            "NOT EXISTS (SELECT 1 FROM v2_listing_revision r "
            "WHERE r.id = NEW.listing_revision_id "
            "AND r.source_listing_id = NEW.source_listing_id)",
            "price revision belongs to another listing",
        ),
        (
            "NOT EXISTS (SELECT 1 FROM v2_source_listing l "
            "WHERE l.id = NEW.source_listing_id AND l.price_nature = NEW.price_nature)",
            "price nature differs from listing",
        ),
        (
            "NOT EXISTS (SELECT 1 FROM v2_crawl_record cr "
            "JOIN v2_crawl_run run ON run.id = cr.crawl_run_id "
            "JOIN v2_source_listing l ON l.id = NEW.source_listing_id "
            "WHERE cr.id = NEW.crawl_record_id "
            "AND run.source_channel_id = l.source_channel_id)",
            "price evidence belongs to another source",
        ),
        (
            "EXISTS (SELECT 1 FROM v2_crawl_record cr "
            "JOIN v2_crawl_run run ON run.id = cr.crawl_run_id "
            "WHERE cr.id = NEW.crawl_record_id AND run.region_scope <> 'MULTI' "
            "AND (run.region_scope <> NEW.region_scope OR run.region_code <> NEW.region_code))",
            "price region differs from crawl run",
        ),
        (
            "NEW.supersedes_observation_id IS NOT NULL AND NOT EXISTS "
            "(SELECT 1 FROM v2_price_observation prior "
            "WHERE prior.id = NEW.supersedes_observation_id "
            "AND prior.source_listing_id = NEW.source_listing_id "
            "AND prior.listing_revision_id = NEW.listing_revision_id "
            "AND prior.region_scope = NEW.region_scope AND prior.region_code = NEW.region_code "
            "AND prior.observed_at = NEW.observed_at)",
            "corrected price does not match old observation identity",
        ),
    ),
    "v2_price_current": (
        (
            "NEW.region_scope = 'NATIONAL' AND NEW.region_code <> 'CN'",
            "national current price must use CN",
        ),
        (
            "NOT EXISTS (SELECT 1 FROM v2_price_observation o "
            "JOIN v2_source_listing l ON l.id = NEW.source_listing_id "
            "WHERE o.id = NEW.price_observation_id "
            "AND o.source_listing_id = NEW.source_listing_id "
            "AND o.listing_revision_id = NEW.listing_revision_id "
            "AND o.region_scope = NEW.region_scope AND o.region_code = NEW.region_code "
            "AND o.observed_at = NEW.observed_at AND o.quality_status = 'ACCEPTED' "
            "AND l.current_revision_id = NEW.listing_revision_id)",
            "current price projection does not match accepted observation",
        ),
    ),
}


def _enum_validations(table_name: str) -> tuple[Validation, ...]:
    validations: list[Validation] = []
    for column_name, values in ENUM_COLUMNS[table_name].items():
        nullable_guard = (
            f"NEW.{column_name} IS NOT NULL AND "
            if (table_name, column_name) in NULLABLE_ENUMS
            else ""
        )
        values_sql = ",".join(f"'{value}'" for value in values)
        validations.append(
            (
                f"{nullable_guard}NEW.{column_name} NOT IN ({values_sql})",
                f"invalid {table_name}.{column_name}",
            )
        )
    return tuple(validations)


def _trigger_name(table_name: str, operation: str) -> str:
    return f"trg_{table_name}_validate_b{operation.lower()}"


def _create_trigger(table_name: str, operation: str) -> None:
    validations = _enum_validations(table_name) + CUSTOM_VALIDATIONS.get(table_name, ())
    statements = " ".join(
        f"IF {condition} THEN SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = '{message}'; END IF;"
        for condition, message in validations
    )
    op.execute(sa.text(f"DROP TRIGGER IF EXISTS `{_trigger_name(table_name, operation)}`"))
    op.execute(
        sa.text(
            f"CREATE TRIGGER `{_trigger_name(table_name, operation)}` "
            f"BEFORE {operation} ON `{table_name}` FOR EACH ROW BEGIN {statements} END"
        )
    )


def _server_version() -> tuple[int, int, int]:
    version = op.get_bind().dialect.server_version_info
    if version is None or len(version) < 3:
        raise RuntimeError("cannot determine MySQL server version during migration")
    return int(version[0]), int(version[1]), int(version[2])


def upgrade() -> None:
    if _server_version() >= CHECK_ENFORCEMENT_VERSION:
        return
    for table_name in ENUM_COLUMNS:
        _create_trigger(table_name, "INSERT")
        _create_trigger(table_name, "UPDATE")


def downgrade() -> None:
    for table_name in reversed(ENUM_COLUMNS):
        op.execute(sa.text(f"DROP TRIGGER IF EXISTS `{_trigger_name(table_name, 'INSERT')}`"))
        op.execute(sa.text(f"DROP TRIGGER IF EXISTS `{_trigger_name(table_name, 'UPDATE')}`"))
