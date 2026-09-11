"""Only catalog-owned tables are in scope for future schema autogeneration.

The historical Alembic chain remains the only definition of retired demo tables.
They may still contain data, and neither retirement nor V2 development permits
Alembic to generate DROP TABLE operations for them or unrelated user tables.
"""

from collections.abc import Mapping

LEGACY_TABLE_NAMES = frozenset(
    {
        "brand",
        "category",
        "product",
        "sku",
        "sales_channel",
        "official_offer",
        "crawl_run",
        "crawl_record",
        "price_current",
        "price_history",
    }
)


def include_name(name: str | None, type_: str, parent_names: Mapping[str, str | None]) -> bool:
    """Exclude historical and unrelated tables before Alembic reflects them."""
    return type_ != "table" or (name is not None and name.startswith("v2_"))
