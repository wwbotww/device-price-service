from __future__ import annotations

from datetime import datetime

import pytest
from sqlalchemy import Engine
from sqlalchemy.orm import Session, sessionmaker

from device_price_service.db.repositories import CatalogRepository, CrawlRunRepository
from device_price_service.db.seed import seed_reference_data
from device_price_service.domain.enums import RunType, TriggerType
from device_price_service.services.database_audit import audit_database

pytestmark = pytest.mark.integration


def test_database_audit_is_read_only_and_detects_stale_runs(
    mysql_engine: Engine,
    session_factory: sessionmaker[Session],
) -> None:
    with session_factory.begin() as session:
        seed_reference_data(session)

    with mysql_engine.connect() as connection:
        initial = audit_database(
            connection,
            stale_run_minutes=120,
            stale_price_hours=12,
            require_completed_runs=False,
        )
    assert initial.healthy
    assert initial.warnings["channels_without_completed_run"] == 5

    with session_factory.begin() as session:
        channel = CatalogRepository(session).require_channel("APPLE_CN_WEB")
        CrawlRunRepository(session).start(
            channel_id=channel.id,
            run_type=RunType.FULL,
            trigger_type=TriggerType.MANUAL,
            adapter_version="audit-test",
            started_at=datetime(2020, 1, 1),
        )

    with mysql_engine.connect() as connection:
        stale = audit_database(
            connection,
            stale_run_minutes=120,
            stale_price_hours=12,
            require_completed_runs=False,
        )
    assert not stale.healthy
    assert stale.critical["stale_running_crawl"] == 1
