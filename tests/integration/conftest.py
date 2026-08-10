from __future__ import annotations

import os
from collections.abc import Iterator

import pytest
from sqlalchemy import Engine
from sqlalchemy.orm import Session, sessionmaker

from device_price_service.config import Settings
from device_price_service.db.base import Base
from device_price_service.db.session import create_database_engine, create_session_factory


@pytest.fixture(scope="session")
def mysql_engine() -> Iterator[Engine]:
    if os.getenv("RUN_MYSQL_INTEGRATION") != "1":
        pytest.skip("set RUN_MYSQL_INTEGRATION=1 to run MySQL integration tests")
    settings = Settings(_env_file=None, mysql_database="device_price_test")
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
