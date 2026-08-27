from __future__ import annotations

import os
from collections.abc import Iterator

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import Engine
from sqlalchemy.orm import Session, sessionmaker

from device_price_service.config import Settings
from device_price_service.db.base import Base
from device_price_service.db.session import create_database_engine, create_session_factory


@pytest.fixture(scope="session")
def mysql_engine() -> Iterator[Engine]:
    if os.getenv("RUN_MYSQL_INTEGRATION") != "1":
        pytest.skip("set RUN_MYSQL_INTEGRATION=1 to run MySQL integration tests")
    settings = Settings(
        _env_file=None,
        mysql_host=os.getenv("TEST_MYSQL_HOST", "127.0.0.1"),
        mysql_port=int(os.getenv("TEST_MYSQL_PORT", "3307")),
        mysql_database=os.getenv("TEST_MYSQL_DATABASE", "device_price_test"),
        mysql_user=os.getenv("TEST_MYSQL_USER", "device_price"),
        mysql_password=os.getenv("TEST_MYSQL_PASSWORD", "device-price-local"),
    )
    engine = create_database_engine(settings)
    yield engine
    engine.dispose()


@pytest.fixture()
def session_factory(mysql_engine: Engine) -> Iterator[sessionmaker[Session]]:
    Base.metadata.drop_all(mysql_engine)
    Base.metadata.create_all(mysql_engine)
    factory = create_session_factory(mysql_engine)
    yield factory
    Base.metadata.drop_all(mysql_engine)


@pytest.fixture()
def migrated_session_factory(mysql_engine: Engine) -> Iterator[sessionmaker[Session]]:
    """Use the actual Alembic schema, including MySQL 5.7 compatibility triggers."""
    Base.metadata.drop_all(mysql_engine)
    config = Config("alembic.ini")
    config.set_main_option("sqlalchemy.url", mysql_engine.url.render_as_string(hide_password=False))
    command.stamp(config, "base", purge=True)
    command.upgrade(config, "head")
    factory = create_session_factory(mysql_engine)
    yield factory
    command.downgrade(config, "base")
