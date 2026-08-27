from typer.testing import CliRunner

from device_price_service.cli import _catalog_requests, app
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


def test_cli_lists_government_catalog_source_without_database() -> None:
    get_settings.cache_clear()
    result = CliRunner().invoke(app, ["catalog", "sources"])

    assert result.exit_code == 0
    assert (
        "SH_FGW_FRESH_RETAIL\tshanghai-fresh-retail\tshanghai-fresh-retail-1"
        in result.stdout
    )
    assert (
        "MOFCOM_FRESH_WHOLESALE\tmofcom-fresh-wholesale\t"
        "mofcom-fresh-wholesale-1" in result.stdout
    )


def test_cli_builds_explicit_mofcom_requests_for_all_supported_commodities() -> None:
    requests = _catalog_requests("MOFCOM_FRESH_WHOLESALE", None)

    assert len(requests) == 15
    assert {request.source_item_codes[0] for request in requests} == {
        "NAPA_CABBAGE",
        "CUCUMBER",
        "CARROT",
        "WHITE_RADISH",
        "TOMATO",
        "POTATO",
        "ONION",
        "GARLIC",
        "GINGER",
        "EGGPLANT",
        "BROCCOLI",
        "CHICKEN_EGG",
        "PORK_HIND",
        "BEEF_LEG",
        "DRESSED_CHICKEN",
    }


def test_cli_refuses_government_smoke_by_default() -> None:
    get_settings.cache_clear()
    result = CliRunner().invoke(app, ["catalog", "smoke"])

    assert result.exit_code == 2
    assert "live crawl is disabled" in result.stderr
