import pytest
from sqlalchemy import Engine

from device_price_service.services.locks import LockNotAcquiredError, mysql_named_lock

pytestmark = pytest.mark.integration


def test_mysql_named_lock_prevents_overlapping_channel_run(mysql_engine: Engine) -> None:
    with (
        mysql_named_lock(mysql_engine, "device-price:test-channel"),
        pytest.raises(LockNotAcquiredError),
        mysql_named_lock(mysql_engine, "device-price:test-channel"),
    ):
        raise AssertionError("second lock must never be acquired")
