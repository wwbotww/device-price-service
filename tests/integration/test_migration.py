import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import Engine, inspect, text
from sqlalchemy.exc import DBAPIError

from device_price_service.cli import EXPECTED_TABLES, V1_TABLES, V2_TABLES
from device_price_service.db.base import Base

pytestmark = pytest.mark.integration


def test_initial_migration_upgrades_and_downgrades(mysql_engine: Engine) -> None:
    Base.metadata.drop_all(mysql_engine)
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


def test_v2_upgrade_and_downgrade_preserve_v1_schema_and_rows(mysql_engine: Engine) -> None:
    Base.metadata.drop_all(mysql_engine)
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
        for table_name in V1_TABLES
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
        for table_name in V1_TABLES
    }
    assert after_upgrade_columns == before_columns

    command.downgrade(config, "6f2a91d4c8b7")
    assert V2_TABLES.isdisjoint(inspect(mysql_engine).get_table_names())
    assert set(inspect(mysql_engine).get_table_names()) >= V1_TABLES
    with mysql_engine.connect() as connection:
        assert connection.scalar(text("SELECT COUNT(*) FROM brand WHERE code = 'KEEP'")) == 1

    command.downgrade(config, "base")
