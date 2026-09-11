import runpy
from pathlib import Path

import pytest
from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.config import Config
from alembic.migration import MigrationContext
from schema_support import LEGACY_TABLE_NAMES, drop_test_tables
from sqlalchemy import Engine, inspect, text
from sqlalchemy.exc import DBAPIError

from device_price_service.cli import V2_TABLES
from device_price_service.db.base import Base

EXPECTED_TABLES = LEGACY_TABLE_NAMES | V2_TABLES

pytestmark = pytest.mark.integration


def test_initial_migration_upgrades_and_downgrades(mysql_engine: Engine) -> None:
    drop_test_tables(mysql_engine)
    config = Config("alembic.ini")
    config.set_main_option("sqlalchemy.url", mysql_engine.url.render_as_string(hide_password=False))
    command.stamp(config, "base", purge=True)

    command.upgrade(config, "head")
    assert set(inspect(mysql_engine).get_table_names()) >= EXPECTED_TABLES
    with mysql_engine.connect() as connection:
        assert connection.scalar(text("SELECT @@session.time_zone")) == "+00:00"
        version = mysql_engine.dialect.server_version_info or ()
        trigger_count = connection.scalar(
            text(
                "SELECT COUNT(*) FROM information_schema.triggers "
                "WHERE trigger_schema = DATABASE() AND trigger_name LIKE 'trg_%_validate_b%'"
            )
        )
        assert trigger_count == (40 if version < (8, 0, 16) else 0)
        connection.execute(
            text("INSERT INTO brand (code, name_zh, name_en) VALUES ('TEST', '测试', 'Test')")
        )
        brand_id = connection.scalar(text("SELECT id FROM brand WHERE code = 'TEST'"))
        connection.execute(text("INSERT INTO category (code, name_zh) VALUES ('TEST', '测试')"))
        category_id = connection.scalar(text("SELECT id FROM category WHERE code = 'TEST'"))
        with pytest.raises(DBAPIError):
            connection.execute(
                text(
                    "INSERT INTO product "
                    "(brand_id, category_id, official_product_id, name, official_url, "
                    "lifecycle_status, first_seen_at, last_seen_at) VALUES "
                    "(:brand_id, :category_id, 'invalid', 'Invalid', "
                    "'https://example.invalid', 'BROKEN', UTC_TIMESTAMP(3), UTC_TIMESTAMP(3))"
                ),
                {"brand_id": brand_id, "category_id": category_id},
            )
        connection.rollback()
        with pytest.raises(DBAPIError):
            connection.execute(
                text(
                    "INSERT INTO v2_source_channel "
                    "(code, name, source_type, business_mode, access_mode, base_url, "
                    "allowed_domains, region_mode, currency, connector_code) VALUES "
                    "('BROKEN', 'Broken', 'INVALID', 'SELF_OPERATED', 'HTTP', "
                    "'https://example.invalid', '[]', 'NATIONAL', 'CNY', 'broken')"
                )
            )
        connection.rollback()

    command.downgrade(config, "base")
    assert EXPECTED_TABLES.isdisjoint(inspect(mysql_engine).get_table_names())


def test_autogenerate_does_not_propose_deleting_frozen_demo_tables(
    mysql_engine: Engine, migrated_session_factory
) -> None:
    assert set(inspect(mysql_engine).get_table_names()) >= LEGACY_TABLE_NAMES
    assert set(Base.metadata.tables) == V2_TABLES
    scope = runpy.run_path(str(Path(__file__).parents[2] / "migrations" / "schema_scope.py"))
    with mysql_engine.connect() as connection:
        context = MigrationContext.configure(
            connection,
            opts={"include_name": scope["include_name"], "compare_type": True},
        )
        differences = compare_metadata(context, Base.metadata)
    legacy_table_changes = [
        difference
        for difference in differences
        if isinstance(difference, tuple)
        and difference[0] in {"add_table", "remove_table"}
        and difference[1].name in LEGACY_TABLE_NAMES
    ]
    assert legacy_table_changes == []


def test_v2_upgrade_and_downgrade_preserve_v1_schema_and_rows(mysql_engine: Engine) -> None:
    drop_test_tables(mysql_engine)
    config = Config("alembic.ini")
    config.set_main_option("sqlalchemy.url", mysql_engine.url.render_as_string(hide_password=False))
    command.stamp(config, "base", purge=True)
    command.upgrade(config, "6f2a91d4c8b7")

    inspector = inspect(mysql_engine)
    before_columns = {
        table_name: tuple(
            (column["name"], str(column["type"]), column["nullable"], str(column["default"]))
            for column in inspector.get_columns(table_name)
        )
        for table_name in LEGACY_TABLE_NAMES
    }
    with mysql_engine.begin() as connection:
        connection.execute(
            text("INSERT INTO brand (code, name_zh, name_en) VALUES ('KEEP', '保留', 'Keep')")
        )

    command.upgrade(config, "head")
    assert set(inspect(mysql_engine).get_table_names()) >= V2_TABLES
    with mysql_engine.connect() as connection:
        assert connection.scalar(text("SELECT COUNT(*) FROM brand WHERE code = 'KEEP'")) == 1
    after_upgrade_columns = {
        table_name: tuple(
            (column["name"], str(column["type"]), column["nullable"], str(column["default"]))
            for column in inspect(mysql_engine).get_columns(table_name)
        )
        for table_name in LEGACY_TABLE_NAMES
    }
    assert after_upgrade_columns == before_columns

    command.downgrade(config, "6f2a91d4c8b7")
    assert V2_TABLES.isdisjoint(inspect(mysql_engine).get_table_names())
    assert set(inspect(mysql_engine).get_table_names()) >= LEGACY_TABLE_NAMES
    with mysql_engine.connect() as connection:
        assert connection.scalar(text("SELECT COUNT(*) FROM brand WHERE code = 'KEEP'")) == 1

    command.downgrade(config, "base")
