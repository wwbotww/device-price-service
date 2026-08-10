import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import Engine, inspect

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

    command.downgrade(config, "base")
    assert EXPECTED_TABLES.isdisjoint(inspect(mysql_engine).get_table_names())
