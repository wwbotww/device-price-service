from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager

from sqlalchemy import Engine, create_engine
from sqlalchemy.orm import Session, sessionmaker

from device_price_service.config import Settings, get_settings


def create_database_engine(settings: Settings | None = None) -> Engine:
    resolved = settings or get_settings()
    return create_engine(
        resolved.sqlalchemy_url,
        pool_pre_ping=True,
        pool_recycle=1800,
    )


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
