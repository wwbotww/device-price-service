"""Runtime retirement guarantees, independent of a MySQL server."""

import importlib.util
import runpy
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

import pytest

from device_price_service.db import catalog_models  # noqa: F401
from device_price_service.db.base import Base
from device_price_service.domain.time import utc_now_naive


@pytest.mark.parametrize(
    "module",
    [
        "device_price_service.db.models",
        "device_price_service.db.repositories",
        "device_price_service.db.seed",
        "device_price_service.domain.models",
        "device_price_service.services.crawl_pipeline",
        "device_price_service.services.persistence_service",
        "device_price_service.crawlers.builtin",
        "device_price_service.crawlers.registry",
        "device_price_service.validation.rules",
    ],
)
def test_retired_business_modules_are_not_importable(module: str) -> None:
    assert importlib.util.find_spec(module) is None


def test_runtime_metadata_registers_only_thirteen_catalog_tables() -> None:
    assert len(Base.metadata.tables) == 13
    assert all(name.startswith("v2_") for name in Base.metadata.tables)


def test_fetching_and_domain_imports_do_not_register_business_tables() -> None:
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "from device_price_service.fetchers.http import HttpFetcher; "
            "from device_price_service.fetchers.browser import BrowserFetcher; "
            "from device_price_service.domain.catalog_models import CatalogPriceObservation; "
            "from device_price_service.db.columns import created_at_column; "
            "from device_price_service.db.errors import RepositoryError; "
            "from device_price_service.db.base import Base; "
            "assert not Base.metadata.tables",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr


def test_shared_time_is_naive_utc() -> None:
    before = datetime.now(UTC).replace(tzinfo=None)
    timestamp = utc_now_naive()
    after = datetime.now(UTC).replace(tzinfo=None)
    assert timestamp.tzinfo is None
    assert before <= timestamp <= after


def test_autogenerate_excludes_frozen_legacy_tables_but_keeps_catalog_changes() -> None:
    scope = runpy.run_path(str(Path(__file__).parents[2] / "migrations" / "schema_scope.py"))
    include_name = scope["include_name"]
    for name in scope["LEGACY_TABLE_NAMES"]:
        assert not include_name(name, "table", {})
    for name in ("claims", "company_users", "unrelated_price_current"):
        assert not include_name(name, "table", {})
    for name in Base.metadata.tables:
        assert include_name(name, "table", {})
    assert include_name("v2_new_owned_table", "table", {})
    assert include_name("id", "column", {"table_name": "v2_item"})
