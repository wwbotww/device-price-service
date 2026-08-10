from typer.testing import CliRunner

from device_price_service.cli import app
from device_price_service.config import get_settings


def test_cli_lists_phase_3_adapters_without_database() -> None:
    get_settings.cache_clear()
    result = CliRunner().invoke(app, ["adapters"])

    assert result.exit_code == 0
    assert "APPLE\tAPPLE_CN_WEB\tapple-cn-html-v1" in result.stdout
    assert "XIAOMI\tXIAOMI_CN_WEB\txiaomi-cn-rendered-v1" in result.stdout


def test_cli_refuses_live_crawl_by_default() -> None:
    get_settings.cache_clear()
    result = CliRunner().invoke(app, ["crawl", "--brand", "APPLE"])

    assert result.exit_code == 2
    assert "live crawl is disabled" in result.stderr
