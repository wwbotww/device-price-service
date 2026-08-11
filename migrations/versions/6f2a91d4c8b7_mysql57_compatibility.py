"""enforce validation constraints on MySQL versions that ignore CHECK

Revision ID: 6f2a91d4c8b7
Revises: 874227e1c153
Create Date: 2026-08-11 10:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "6f2a91d4c8b7"
down_revision: str | None = "874227e1c153"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

CHECK_ENFORCEMENT_VERSION = (8, 0, 16)

VALIDATIONS: dict[str, tuple[tuple[str, str], ...]] = {
    "product": (
        (
            "NEW.lifecycle_status NOT IN ('ACTIVE','INACTIVE','UNKNOWN')",
            "invalid product lifecycle_status",
        ),
    ),
    "sales_channel": (
        ("NEW.region_code <> 'CN'", "sales_channel region_code must be CN"),
        ("NEW.currency <> 'CNY'", "sales_channel currency must be CNY"),
        (
            "NEW.seller_type <> 'OFFICIAL_DIRECT'",
            "sales_channel seller_type must be OFFICIAL_DIRECT",
        ),
    ),
    "crawl_run": (
        (
            "NEW.run_type NOT IN ('DISCOVERY','PRICE','FULL','REPLAY')",
            "invalid crawl_run run_type",
        ),
        (
            "NEW.trigger_type NOT IN ('SCHEDULED','MANUAL')",
            "invalid crawl_run trigger_type",
        ),
        (
            "NEW.status NOT IN ('RUNNING','SUCCEEDED','PARTIAL','FAILED','CANCELLED')",
            "invalid crawl_run status",
        ),
    ),
    "sku": (("NEW.status NOT IN ('ACTIVE','INACTIVE','UNKNOWN')", "invalid sku status"),),
    "official_offer": (
        (
            "NEW.availability NOT IN "
            "('ON_SALE','OUT_OF_STOCK','RESERVATION','PRE_SALE','COMING_SOON',"
            "'OFF_SHELF','UNKNOWN')",
            "invalid official_offer availability",
        ),
        ("NEW.consecutive_misses < 0", "consecutive_misses cannot be negative"),
    ),
    "price_current": (
        ("NEW.currency <> 'CNY'", "price_current currency must be CNY"),
        ("NEW.original_price IS NOT NULL AND NEW.original_price <= 0", "invalid original_price"),
        ("NEW.current_price IS NOT NULL AND NEW.current_price <= 0", "invalid current_price"),
        (
            "NEW.original_price_type NOT IN ('CROSSED_OUT','MSRP','EXPLICIT_ORIGINAL','NONE')",
            "invalid original_price_type",
        ),
        (
            "(NEW.original_price IS NULL AND NEW.original_price_type <> 'NONE') OR "
            "(NEW.original_price IS NOT NULL AND NEW.original_price_type = 'NONE')",
            "invalid original_price semantics",
        ),
    ),
    "price_history": (
        ("NEW.currency <> 'CNY'", "price_history currency must be CNY"),
        ("NEW.original_price IS NOT NULL AND NEW.original_price <= 0", "invalid original_price"),
        ("NEW.current_price IS NOT NULL AND NEW.current_price <= 0", "invalid current_price"),
        ("NEW.valid_to IS NOT NULL AND NEW.valid_to < NEW.valid_from", "invalid valid range"),
        ("NEW.last_observed_at < NEW.valid_from", "invalid last_observed_at"),
        (
            "NEW.original_price_type NOT IN ('CROSSED_OUT','MSRP','EXPLICIT_ORIGINAL','NONE')",
            "invalid original_price_type",
        ),
        (
            "(NEW.original_price IS NULL AND NEW.original_price_type <> 'NONE') OR "
            "(NEW.original_price IS NOT NULL AND NEW.original_price_type = 'NONE')",
            "invalid original_price semantics",
        ),
    ),
}


def _trigger_name(table_name: str, operation: str) -> str:
    return f"trg_{table_name}_validate_b{operation.lower()}"


def _create_trigger(table_name: str, operation: str) -> None:
    statements = " ".join(
        f"IF {condition} THEN SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = '{message}'; END IF;"
        for condition, message in VALIDATIONS[table_name]
    )
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
    for table_name in VALIDATIONS:
        _create_trigger(table_name, "INSERT")
        _create_trigger(table_name, "UPDATE")


def downgrade() -> None:
    for table_name in reversed(VALIDATIONS):
        op.execute(sa.text(f"DROP TRIGGER IF EXISTS `{_trigger_name(table_name, 'INSERT')}`"))
        op.execute(sa.text(f"DROP TRIGGER IF EXISTS `{_trigger_name(table_name, 'UPDATE')}`"))
