from typer.testing import CliRunner

from device_price_service.cli import app
from device_price_service.config import get_settings


def test_cli_lists_phase_4_adapters_without_database() -> None:
    get_settings.cache_clear()
    result = CliRunner().invoke(app, ["adapters"])

    assert result.exit_code == 0
    assert "APPLE\tAPPLE_CN_WEB\tapple-cn-bootstrap-v2" in result.stdout
    assert "HUAWEI\tHUAWEI_CN_WEB\thuawei-cn-next-v2" in result.stdout
    assert "XIAOMI\tXIAOMI_CN_WEB\txiaomi-cn-rendered-v2" in result.stdout
    assert "OPPO\tOPPO_CN_WEB\toppo-cn-oapi-v2" in result.stdout
    assert "VIVO\tVIVO_CN_WEB\tvivo-cn-api-v2" in result.stdout


def test_cli_refuses_live_crawl_by_default() -> None:
    get_settings.cache_clear()
    result = CliRunner().invoke(app, ["crawl", "--brand", "APPLE"])

    assert result.exit_code == 2
    assert "live crawl is disabled" in result.stderr
