import pytest
from typer.testing import CliRunner

import device_price_service.cli as cli
from device_price_service.cli import _catalog_requests, app
from device_price_service.config import Settings, get_settings


def test_cli_removed_adapter_listing_cannot_access_any_database() -> None:
    get_settings.cache_clear()
    result = CliRunner().invoke(app, ["adapters"])

    assert result.exit_code == 2
    assert "No such command" in result.stderr


def test_cli_refuses_live_crawl_by_default() -> None:
    get_settings.cache_clear()
    result = CliRunner().invoke(app, ["catalog", "crawl", "--channel", "APPLE_CN_WEB"])

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
    assert "No such command" in result.stderr


@pytest.mark.parametrize("arguments", [["replay", "--record-id", "1"], ["db", "seed"]])
def test_legacy_replay_and_seed_are_removed_without_database_access(
    arguments: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    def forbidden(*args: object, **kwargs: object) -> None:
        pytest.fail("retired entrypoints must not access a database or source")

    monkeypatch.setattr(cli, "create_database_engine", forbidden)
    monkeypatch.setattr(cli, "build_catalog_runtime", forbidden)
    result = CliRunner().invoke(app, arguments)
    assert result.exit_code == 2
    assert "No such command" in result.stderr


@pytest.mark.parametrize(
    "arguments",
    [
        ["scheduler"],
        ["scheduler", "--channel", "SH_FGW_FRESH_RETAIL"],
        ["scheduler", "--channel", "UNKNOWN"],
        ["scheduler", "--channel", "APPLE_CN_WEB", "--channel", "apple_cn_web"],
    ],
)
def test_scheduler_requires_explicit_valid_unique_devices_before_runtime(arguments, monkeypatch):
    monkeypatch.setattr(
        cli, "get_settings", lambda: Settings(_env_file=None, live_crawl_enabled=True)
    )
    monkeypatch.setattr(
        cli, "build_catalog_runtime", lambda *_: pytest.fail("invalid schedule constructed runtime")
    )
    result = CliRunner().invoke(app, arguments)
    assert result.exit_code == 2


def test_scheduler_obeys_live_gate(monkeypatch):
    monkeypatch.setattr(
        cli, "get_settings", lambda: Settings(_env_file=None, live_crawl_enabled=False)
    )
    monkeypatch.setattr(
        cli,
        "build_catalog_runtime",
        lambda *_: pytest.fail("disabled schedule constructed runtime"),
    )
    result = CliRunner().invoke(app, ["scheduler", "--channel", "APPLE_CN_WEB"])
    assert result.exit_code == 2 and "live crawl is disabled" in result.stderr


def test_replay_help_requires_no_source_or_database(monkeypatch):
    monkeypatch.setattr(
        cli, "create_database_engine", lambda *_: pytest.fail("help accessed database")
    )
    result = CliRunner().invoke(app, ["catalog", "replay", "--help"])
    assert result.exit_code == 0
    assert "--record-id" in result.stdout and "read-only" in result.stdout
