from pathlib import Path

import pytest
from pydantic import ValidationError

from device_price_service.config import Settings


def test_settings_builds_mysql_url_and_encodes_credentials() -> None:
    settings = Settings(
        _env_file=None,
        mysql_user="user@example",
        mysql_password="p@ss/word",
        mysql_database="prices",
    )

    assert settings.sqlalchemy_url == (
        "mysql+pymysql://user%40example:p%40ss%2Fword@127.0.0.1:3307/prices?charset=utf8mb4"
    )
    assert settings.raw_storage_path == Path("var/raw")


def test_settings_rejects_reversed_delay_range() -> None:
    with pytest.raises(ValidationError, match="HTTP_MAX_DELAY_SECONDS"):
        Settings(_env_file=None, http_min_delay_seconds=3, http_max_delay_seconds=1)


def test_live_crawl_accepts_anonymous_test_user_agent() -> None:
    settings = Settings(_env_file=None, live_crawl_enabled=True)

    assert settings.crawler_user_agent == "DevicePriceTestBot/1.0"


def test_user_agent_cannot_be_empty() -> None:
    with pytest.raises(ValidationError):
        Settings(_env_file=None, crawler_user_agent="")
