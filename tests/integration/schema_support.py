"""Dedicated-test-schema cleanup and reflection of frozen historical tables.

No legacy ORM or write repository is shipped in the application. Coexistence
tests inspect the schema created by the unchanged historical migrations.
"""

import runpy
from pathlib import Path

from sqlalchemy import Engine, MetaData

from device_price_service.db import catalog_models  # noqa: F401
from device_price_service.db.base import Base

LEGACY_TABLE_NAMES: frozenset[str] = runpy.run_path(
    str(Path(__file__).parents[2] / "migrations" / "schema_scope.py")
)["LEGACY_TABLE_NAMES"]


def reflect_legacy_tables(engine: Engine) -> MetaData:
    metadata = MetaData()
    metadata.reflect(bind=engine, only=lambda name, _: name in LEGACY_TABLE_NAMES)
    return metadata


def drop_test_tables(engine: Engine) -> None:
    if engine.url.database != "device_price_test":
        raise ValueError("destructive cleanup requires the dedicated device_price_test schema")
    expected_tables = LEGACY_TABLE_NAMES | Base.metadata.tables.keys()
    metadata = MetaData()
    metadata.reflect(bind=engine, only=lambda name, _: name in expected_tables)
    metadata.drop_all(engine)
