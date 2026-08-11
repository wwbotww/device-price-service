import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import Engine, inspect, text
from sqlalchemy.exc import DBAPIError

from device_price_service.cli import EXPECTED_TABLES
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
        assert trigger_count == (14 if version < (8, 0, 16) else 0)
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

    command.downgrade(config, "base")
    assert EXPECTED_TABLES.isdisjoint(inspect(mysql_engine).get_table_names())
