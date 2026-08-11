from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager

from sqlalchemy import Engine, create_engine
from sqlalchemy.orm import Session, sessionmaker

from device_price_service.config import Settings, get_settings
from device_price_service.db.mysql_compat import install_mysql_connection_guards


def create_database_engine(settings: Settings | None = None) -> Engine:
    resolved = settings or get_settings()
    engine = create_engine(
        resolved.sqlalchemy_url,
        pool_pre_ping=True,
        pool_recycle=1800,
    )
    install_mysql_connection_guards(engine)
    return engine


def create_session_factory(engine: Engine) -> sessionmaker[Session]:
    return sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


@contextmanager
def session_scope(factory: sessionmaker[Session]) -> Iterator[Session]:
    session = factory()
    try:
        with session.begin():
            yield session
    finally:
        session.close()
