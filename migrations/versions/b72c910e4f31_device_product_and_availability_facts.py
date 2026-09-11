"""Allow product evidence and explicitly price-free availability facts.

Revision ID: b72c910e4f31
Revises: 96524222b3ec
"""

from collections.abc import Sequence
from pathlib import Path
from runpy import run_path

import sqlalchemy as sa
from alembic import op

revision: str = "b72c910e4f31"
down_revision: str | None = "96524222b3ec"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Reuse the frozen predecessor's validations, not mutable application models.
# Only the two affected tables are changed; all cross-table guards are retained.
_PREVIOUS = run_path(
    str(Path(__file__).with_name("96524222b3ec_enforce_v2_mysql57_constraints.py"))
)
_TABLES = ("v2_crawl_record", "v2_price_observation")
_ELIGIBILITY_MESSAGE = "accepted price is not eligible for current projection"
_OLD_PRICE_ELIGIBILITY = (
    "current_price IS NOT NULL AND region_scope <> 'UNKNOWN' "
    "AND price_nature <> 'UNKNOWN' "
    "AND pricing_basis IN ('PACKAGE_TOTAL','UNIT_QUOTED') "
    "AND (pricing_basis <> 'UNIT_QUOTED' OR unit_price IS NOT NULL) "
    "AND ((price_nature IN ('RETAIL_OFFER','WHOLESALE_OFFER') "
    "AND price_type = 'DIRECT_UNCONDITIONAL' "
    "AND fee_status IN ('ITEM_ONLY','SEPARATE_FEES_EXCLUDED')) "
    "OR (price_nature IN ('RETAIL_AVERAGE','WHOLESALE_AVERAGE','MARKET_AVERAGE') "
    "AND price_type = 'PUBLISHED_VALUE' AND fee_status = 'NOT_APPLICABLE'))"
)
_STATE_ELIGIBILITY = (
    "price_type = 'AVAILABILITY_ONLY' "
    "AND price_nature = 'RETAIL_OFFER' AND region_scope <> 'UNKNOWN' "
    "AND availability IN ('OFF_SHELF','OUT_OF_STOCK','COMING_SOON') "
    "AND current_price IS NULL AND original_price IS NULL "
    "AND original_price_type = 'NONE' AND unit_price IS NULL "
    "AND unit_price_unit IS NULL AND pricing_basis = 'UNKNOWN' "
    "AND fee_status = 'NOT_APPLICABLE' AND promotion_label IS NULL"
)


def _eligibility(*, upgrade: bool, prefix: str = "") -> str:
    expression = f"({_OLD_PRICE_ELIGIBILITY})"
    if upgrade:
        expression = f"({_STATE_ELIGIBILITY}) OR {expression}"
    expression = f"quality_status <> 'ACCEPTED' OR ({expression})"
    if prefix:
        # These names belong to this immutable migration, not an ORM registry.
        import re

        columns = (
            "quality_status",
            "current_price",
            "original_price",
            "original_price_type",
            "unit_price",
            "unit_price_unit",
            "region_scope",
            "price_nature",
            "price_type",
            "pricing_basis",
            "availability",
            "fee_status",
            "promotion_label",
        )
        expression = re.sub(
            r"\b(" + "|".join(columns) + r")\b",
            lambda match: prefix + match.group(),
            expression,
        )
    return expression


def _enums(table: str, *, upgrade: bool) -> dict[str, tuple[str, ...]]:
    enums = dict(_PREVIOUS["ENUM_COLUMNS"][table])
    if upgrade:
        column, value = (
            ("entity_type", "PRODUCT")
            if table == "v2_crawl_record"
            else ("price_type", "AVAILABILITY_ONLY")
        )
        enums[column] = (*enums[column], value)
    return enums


def _replace_constraints(*, upgrade: bool) -> None:
    version = op.get_bind().dialect.server_version_info
    if version is None:
        raise RuntimeError("cannot determine MySQL version during device migration")
    if version >= (8, 0, 16):
        for table, column in (
            ("v2_crawl_record", "entity_type"),
            ("v2_price_observation", "price_type"),
        ):
            name = op.f(f"ck_{table}_{column}")
            values = ",".join(f"'{value}'" for value in _enums(table, upgrade=upgrade)[column])
            op.drop_constraint(name, table, type_="check")
            op.create_check_constraint(name, table, f"{column} IN ({values})")
        name = op.f("ck_v2_price_observation_accepted_eligibility")
        op.drop_constraint(name, "v2_price_observation", type_="check")
        op.create_check_constraint(name, "v2_price_observation", _eligibility(upgrade=upgrade))
        return

    for table in _TABLES:
        validations = [
            (
                f"NEW.{column} NOT IN ({','.join(repr(value) for value in values)})",
                f"invalid {table}.{column}",
            )
            for column, values in _enums(table, upgrade=upgrade).items()
        ]
        for condition, message in _PREVIOUS["CUSTOM_VALIDATIONS"].get(table, ()):
            if message == _ELIGIBILITY_MESSAGE:
                condition = f"NOT ({_eligibility(upgrade=upgrade, prefix='NEW.')})"
            validations.append((condition, message))
        statements = " ".join(
            f"IF {condition} THEN SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = '{message}'; END IF;"
            for condition, message in validations
        )
        for operation in ("INSERT", "UPDATE"):
            name = f"trg_{table}_validate_b{operation.lower()}"
            op.execute(sa.text(f"DROP TRIGGER IF EXISTS `{name}`"))
            op.execute(
                sa.text(
                    f"CREATE TRIGGER `{name}` BEFORE {operation} ON `{table}` "
                    f"FOR EACH ROW BEGIN {statements} END"
                )
            )


def upgrade() -> None:
    _replace_constraints(upgrade=True)


def downgrade() -> None:
    # MySQL DDL is not transactional. Refuse before any DDL if facts would become
    # invalid; do not silently delete prices or mislabel product evidence.
    for table, predicate in (
        ("v2_crawl_record", "entity_type = 'PRODUCT'"),
        ("v2_price_observation", "price_type = 'AVAILABILITY_ONLY'"),
    ):
        if op.get_bind().scalar(sa.text(f"SELECT COUNT(*) FROM `{table}` WHERE {predicate}")):
            raise RuntimeError(
                "device migration downgrade requires no PRODUCT or AVAILABILITY_ONLY facts; "
                "restore an approved backup instead of deleting evidence"
            )
    _replace_constraints(upgrade=False)
