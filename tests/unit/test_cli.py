import pytest
from typer.testing import CliRunner

import device_price_service.cli as cli
from device_price_service.cli import _catalog_requests, app
from device_price_service.config import Settings, get_settings


def test_cli_retired_adapter_listing_explains_unified_catalog_entrypoint() -> None:
    get_settings.cache_clear()
    result = CliRunner().invoke(app, ["adapters"])

    assert result.exit_code == 2
    assert "V1 adapters are retired; use catalog sources" in result.stderr


def test_cli_refuses_live_crawl_by_default() -> None:
    get_settings.cache_clear()
    result = CliRunner().invoke(app, ["crawl", "--brand", "APPLE"])

    assert result.exit_code == 2
    assert "live crawl is disabled" in result.stderr


def test_cli_lists_government_and_five_device_catalog_sources_without_database() -> None:
    get_settings.cache_clear()
    result = CliRunner().invoke(app, ["catalog", "sources"])

    assert result.exit_code == 0
    assert "APPLE_CN_WEB\tapple-cn\tapple-cn-catalog-product" in result.stdout
    for brand in ("HUAWEI", "XIAOMI", "OPPO", "VIVO"):
        assert f"{brand}_CN_WEB\t{brand.lower()}-cn\t" in result.stdout
    assert len(result.stdout.strip().splitlines()) == 7
    assert "SH_FGW_FRESH_RETAIL\tshanghai-fresh-retail\tshanghai-fresh-retail-1" in result.stdout
    assert (
        "MOFCOM_FRESH_WHOLESALE\tmofcom-fresh-wholesale\tmofcom-fresh-wholesale-1" in result.stdout
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


def test_device_seed_help_is_available_without_database_or_live_access() -> None:
    get_settings.cache_clear()
    result = CliRunner().invoke(app, ["db", "seed-devices", "--help"])
    assert result.exit_code == 0
    assert "--enable" in result.stdout
    assert "disabled official sources" in result.stdout


@pytest.mark.parametrize("brand", ["APPLE", "HUAWEI", "XIAOMI", "OPPO", "VIVO", "UNKNOWN"])
@pytest.mark.parametrize("command", ["crawl", "smoke"])
def test_all_legacy_collection_commands_refuse_before_database_or_source_access(
    brand: str, command: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        cli, "get_settings", lambda: Settings(_env_file=None, live_crawl_enabled=True)
    )

    def forbidden(*args: object, **kwargs: object) -> None:
        pytest.fail("retired entrypoints must not create a runtime or database connection")

    monkeypatch.setattr(cli, "create_database_engine", forbidden)
    monkeypatch.setattr(cli, "build_catalog_runtime", forbidden)
    result = CliRunner().invoke(app, [command, "--brand", brand])
    assert result.exit_code == 2
    if brand == "UNKNOWN":
        assert "V1 collection is retired" in result.stderr
    else:
        assert "now uses V2" in result.stderr
        assert f"{brand}_CN_WEB" in result.stderr


@pytest.mark.parametrize("arguments", [["replay", "--record-id", "1"], ["scheduler"]])
def test_legacy_replay_and_scheduler_refuse_without_database_access(
    arguments: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    def forbidden(*args: object, **kwargs: object) -> None:
        pytest.fail("retired entrypoints must not access a database or source")

    monkeypatch.setattr(cli, "create_database_engine", forbidden)
    monkeypatch.setattr(cli, "build_catalog_runtime", forbidden)
    result = CliRunner().invoke(app, arguments)
    assert result.exit_code == 2
    assert "is retired" in result.stderr
    assert "phase L" in result.stderr
