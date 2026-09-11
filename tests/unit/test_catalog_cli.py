from __future__ import annotations

import base64
import json
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import pytest
from typer.testing import CliRunner

import device_price_service.cli as cli
from device_price_service.config import Settings
from device_price_service.crawlers.base import AdapterContext
from device_price_service.crawlers.catalog import CatalogConnector, CatalogConnectorRegistry
from device_price_service.crawlers.mofcom_fresh import (
    MOFCOM_FRESH_BASE_URL,
    MofcomFreshWholesaleConnector,
)
from device_price_service.crawlers.shanghai_fresh import ShanghaiFreshRetailConnector
from device_price_service.domain.catalog_crawl import (
    CatalogCollectionRequest,
    DiscoveredCatalogDataset,
    DiscoveredCatalogListing,
    DiscoveredCatalogProduct,
    ParsedCatalogListing,
    ParsedCatalogProduct,
    ParsedCatalogRow,
    SourceMerchant,
    SourcePriceCandidate,
)
from device_price_service.domain.catalog_enums import (
    Availability,
    CollectionFetchMethod,
    FeeStatus,
    PriceNature,
    PriceType,
    PricingBasis,
    RegionScope,
    SellerType,
    VerificationStatus,
)
from device_price_service.domain.crawl import FetchResult
from device_price_service.domain.enums import FetchMethod, RunStatus
from device_price_service.normalization.devices import DeviceSpecification, device_listing_key
from device_price_service.services.catalog_crawl_pipeline import CatalogConfigurationError


class FixtureProductConnector(CatalogConnector):
    channel_code = "APPLE_CN_WEB"
    connector_code = "apple-cn"
    version = "fixture-product"
    brand_code = "APPLE"
    allowed_domains = ("www.apple.com.cn",)
    default_category_codes = ("PHONE",)
    fetch_method = CollectionFetchMethod.HTTP

    def __init__(self) -> None:
        self.discovered = [
            DiscoveredCatalogProduct(
                external_product_id="phone",
                category_code="PHONE",
                url="https://www.apple.com.cn/shop/buy-iphone/phone",
            )
        ]
        self.prices = (PriceType.DIRECT_UNCONDITIONAL,)
        self.specification_known = True
        self.fetch_count = 0
        self.parse_count = 0
        self.status_code = 200
        self.final_url: str | None = None

    async def discover_products(
        self,
        context: AdapterContext,
        request: CatalogCollectionRequest,
    ) -> list[DiscoveredCatalogProduct]:
        return self.discovered

    async def fetch_product(
        self,
        context: AdapterContext,
        item: DiscoveredCatalogProduct,
    ) -> FetchResult:
        self.fetch_count += 1
        return FetchResult(
            request_url=item.url,
            final_url=self.final_url or item.url,
            status_code=self.status_code,
            headers={"content-type": "application/json"},
            body=b"{}",
            fetched_at=datetime(2026, 9, 11),
            duration_ms=1,
            fetch_method=FetchMethod.HTTP,
        )

    def parse_product(
        self,
        item: DiscoveredCatalogProduct,
        result: FetchResult,
    ) -> ParsedCatalogProduct:
        self.parse_count += 1
        rows: list[ParsedCatalogRow] = []
        for index, price_type in enumerate(self.prices):
            specification = DeviceSpecification(capacity="256GB", color=f"color-{index}")
            sku_id = f"SKU-{index}"
            rows.append(
                ParsedCatalogRow(
                    item=DiscoveredCatalogListing(
                        listing_key=device_listing_key(
                            product_id=item.external_product_id,
                            sku_id=sku_id,
                            specification=specification,
                        ),
                        url=item.url,
                        category_code="PHONE",
                        external_product_id=item.external_product_id,
                        external_sku_id=sku_id,
                        price_nature=PriceNature.RETAIL_OFFER,
                        merchant=SourceMerchant(
                            merchant_key="apple-official",
                            name="Apple 官方商城",
                            seller_type=SellerType.BRAND_OFFICIAL,
                            verification_status=VerificationStatus.VERIFIED,
                        ),
                    ),
                    parsed=ParsedCatalogListing(
                        source_title="Fixture phone",
                        source_attributes={"device_specification": specification.model_dump()}
                        if self.specification_known
                        else {},
                        price_candidates=[
                            SourcePriceCandidate(
                                current_price=Decimal("5999"),
                                price_type=price_type,
                                pricing_basis=PricingBasis.PACKAGE_TOTAL,
                                availability=Availability.ON_SALE,
                                fee_status=FeeStatus.ITEM_ONLY,
                            )
                        ],
                    ),
                )
            )
        return ParsedCatalogProduct(
            external_product_id=item.external_product_id,
            category_code="PHONE",
            brand_code="APPLE",
            name="Fixture phone",
            rows=rows,
        )


class OfflineHttp:
    closed = False

    def __init__(self, settings: Settings) -> None:
        pass

    async def aclose(self) -> None:
        self.closed = True


@pytest.fixture
def offline_cli(monkeypatch: pytest.MonkeyPatch) -> FixtureProductConnector:
    settings = Settings(_env_file=None, live_crawl_enabled=True)
    monkeypatch.setattr(cli, "get_settings", lambda: settings)
    connector = FixtureProductConnector()
    registry = CatalogConnectorRegistry()
    registry.register(connector)
    monkeypatch.setattr(cli, "build_catalog_registry", lambda: registry)
    monkeypatch.setattr(cli, "HttpFetcher", OfflineHttp)
    monkeypatch.setattr(cli, "BrowserFetcher", lambda settings: object())

    def forbidden(*args: object, **kwargs: object) -> None:
        pytest.fail("database/runtime must not be created by smoke or argument validation")

    monkeypatch.setattr(cli, "create_database_engine", forbidden)
    monkeypatch.setattr(cli, "build_catalog_runtime", forbidden)
    return connector


def test_product_default_scope_comes_from_connector(offline_cli: FixtureProductConnector) -> None:
    requests = cli._catalog_requests("apple_cn_web", None)
    assert len(requests) == 1
    assert requests[0].region_scope is RegionScope.NATIONAL
    assert requests[0].region_code == "CN"
    assert requests[0].category_codes == ("PHONE",)


@pytest.mark.parametrize(
    ("prices", "known_specification", "expected_status", "expected_code"),
    [
        ((PriceType.DIRECT_UNCONDITIONAL,), True, "SUCCEEDED", 0),
        ((PriceType.MEMBER,), True, "FAILED", 1),
        ((PriceType.DIRECT_UNCONDITIONAL, PriceType.MEMBER), True, "PARTIAL", 1),
        ((PriceType.DIRECT_UNCONDITIONAL,), False, "FAILED", 1),
    ],
)
def test_product_smoke_uses_shared_quality_rules_without_database(
    offline_cli: FixtureProductConnector,
    prices: tuple[PriceType, ...],
    known_specification: bool,
    expected_status: str,
    expected_code: int,
) -> None:
    offline_cli.prices = prices
    offline_cli.specification_known = known_specification
    result = CliRunner().invoke(cli.app, ["catalog", "smoke", "--channel", "APPLE_CN_WEB"])
    assert result.exit_code == expected_code, result.output
    assert f'"status": "{expected_status}"' in result.stdout
    assert offline_cli.fetch_count == 1
    assert offline_cli.parse_count == 1


def test_product_smoke_caps_product_fetches(offline_cli: FixtureProductConnector) -> None:
    offline_cli.discovered.append(
        offline_cli.discovered[0].model_copy(
            update={"external_product_id": "phone-2"},
        )
    )
    result = CliRunner().invoke(
        cli.app,
        [
            "catalog",
            "smoke",
            "--channel",
            "APPLE_CN_WEB",
            "--max-products",
            "1",
        ],
    )
    assert result.exit_code == 0, result.output
    assert '"discovered_count": 2' in result.stdout
    assert '"tested_count": 1' in result.stdout
    assert offline_cli.fetch_count == 1


def test_product_smoke_empty_discovery_is_failed(offline_cli: FixtureProductConnector) -> None:
    offline_cli.discovered = []
    result = CliRunner().invoke(cli.app, ["catalog", "smoke", "--channel", "APPLE_CN_WEB"])
    assert result.exit_code == 1
    assert '"status": "FAILED"' in result.stdout
    assert "no products" in result.stdout
    assert '"tested_count": 0' in result.stdout
    assert offline_cli.fetch_count == 0


def test_product_smoke_duplicate_discovery_is_failed_before_fetch(
    offline_cli: FixtureProductConnector,
) -> None:
    offline_cli.discovered.append(offline_cli.discovered[0])
    result = CliRunner().invoke(cli.app, ["catalog", "smoke", "--channel", "APPLE_CN_WEB"])
    assert result.exit_code == 1
    assert "duplicate product identities" in result.stdout
    assert '"tested_count": 0' in result.stdout
    assert offline_cli.fetch_count == 0


def test_smoke_extra_conditional_candidate_does_not_hide_valid_direct_price(
    offline_cli: FixtureProductConnector,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    parse_product = offline_cli.parse_product

    def parse(item: DiscoveredCatalogProduct, result: FetchResult) -> ParsedCatalogProduct:
        product = parse_product(item, result)
        candidates = product.rows[0].parsed.price_candidates
        candidates.append(candidates[0].model_copy(update={"price_type": PriceType.MEMBER}))
        return product

    monkeypatch.setattr(offline_cli, "parse_product", parse)
    result = CliRunner().invoke(cli.app, ["catalog", "smoke", "--channel", "APPLE_CN_WEB"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["status"] == "SUCCEEDED"
    assert payload["samples"][0]["accepted_count"] == 1
    assert payload["samples"][0]["rejected_count"] == 1


@pytest.mark.parametrize("invalid_response", ["http", "redirect"])
def test_product_smoke_rejects_fetch_before_parsing(
    offline_cli: FixtureProductConnector,
    invalid_response: str,
) -> None:
    if invalid_response == "http":
        offline_cli.status_code = 403
    else:
        offline_cli.final_url = "https://third-party.example/phone"
    result = CliRunner().invoke(cli.app, ["catalog", "smoke", "--channel", "APPLE_CN_WEB"])
    assert result.exit_code == 1
    assert '"status": "FAILED"' in result.stdout
    assert offline_cli.parse_count == 0


@pytest.mark.parametrize("command", ["crawl", "smoke"])
def test_product_arguments_are_rejected_before_database_setup(
    offline_cli: FixtureProductConnector,
    command: str,
) -> None:
    result = CliRunner().invoke(
        cli.app,
        [
            "catalog",
            command,
            "--channel",
            "APPLE_CN_WEB",
            "--commodity",
            "ALL",
        ],
    )
    assert result.exit_code == 2
    assert "only supported by the MOFCOM" in result.stderr


def test_unknown_catalog_channel_is_parameter_error_before_database_setup(
    offline_cli: FixtureProductConnector,
) -> None:
    result = CliRunner().invoke(cli.app, ["catalog", "crawl", "--channel", "UNKNOWN"])
    assert result.exit_code == 2
    assert "not registered" in result.stderr


@dataclass(frozen=True)
class FixtureOutcome:
    status: RunStatus
    failed_count: int = 0
    accepted_count: int = 0


@pytest.mark.parametrize("status", [RunStatus.SUCCEEDED, RunStatus.PARTIAL, RunStatus.FAILED])
def test_catalog_crawl_exit_uses_status_not_failed_count(
    offline_cli: FixtureProductConnector,
    monkeypatch: pytest.MonkeyPatch,
    status: RunStatus,
) -> None:
    closed = []

    async def run(connector: CatalogConnector, request: CatalogCollectionRequest) -> FixtureOutcome:
        assert connector is offline_cli
        assert request.region_code == "CN"
        return FixtureOutcome(status=status)

    async def close() -> None:
        closed.append(True)

    runtime = SimpleNamespace(pipeline=SimpleNamespace(run=run), aclose=close)
    monkeypatch.setattr(cli, "build_catalog_runtime", lambda settings: runtime)
    result = CliRunner().invoke(cli.app, ["catalog", "crawl", "--channel", "APPLE_CN_WEB"])
    assert result.exit_code == (0 if status is RunStatus.SUCCEEDED else 1), result.output
    assert f'"status": "{status.value}"' in result.stdout
    assert closed == [True]


def test_catalog_crawl_configuration_gate_exits_two_and_closes_runtime(
    offline_cli: FixtureProductConnector,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    closed = []

    async def run(connector: CatalogConnector, request: CatalogCollectionRequest) -> FixtureOutcome:
        raise CatalogConfigurationError("source is disabled; seed-devices --enable is required")

    async def close() -> None:
        closed.append(True)

    runtime = SimpleNamespace(pipeline=SimpleNamespace(run=run), aclose=close)
    monkeypatch.setattr(cli, "build_catalog_runtime", lambda settings: runtime)
    result = CliRunner().invoke(cli.app, ["catalog", "crawl", "--channel", "APPLE_CN_WEB"])
    assert result.exit_code == 2
    assert "source is disabled" in result.stderr
    assert closed == [True]


@pytest.mark.parametrize("command", ["crawl", "smoke"])
def test_migrated_brand_legacy_command_is_removed(
    offline_cli: FixtureProductConnector,
    command: str,
) -> None:
    result = CliRunner().invoke(cli.app, [command, "--brand", "APPLE"])
    assert result.exit_code == 2
    assert "No such command" in result.stderr


@pytest.mark.parametrize("status_code", [200, 403])
def test_government_smoke_uses_fixture_and_static_quality_without_database(
    offline_cli: FixtureProductConnector,
    monkeypatch: pytest.MonkeyPatch,
    status_code: int,
) -> None:
    connector = ShanghaiFreshRetailConnector()
    registry = CatalogConnectorRegistry()
    registry.register(connector)
    monkeypatch.setattr(cli, "build_catalog_registry", lambda: registry)
    dataset = DiscoveredCatalogDataset(
        dataset_key="shanghai-fresh-retail:2026-08-19",
        url="https://fgw.sh.gov.cn/cmsres/sanitized/shanghai-fresh.xls",
        source_page_url="https://fgw.sh.gov.cn/fgw_jgjgdt/20260819/fixture.html",
        source_observed_at=datetime(2026, 8, 18, 16),
        metadata={"source_date": "2026-08-19", "time_precision": "DAY"},
    )
    fixture = Path(__file__).parents[1] / "fixtures/catalog_fresh/shanghai_daily_20260819.xls.b64"

    async def discover(
        context: AdapterContext,
        request: CatalogCollectionRequest,
    ) -> DiscoveredCatalogDataset:
        return dataset

    async def fetch(context: AdapterContext, item: DiscoveredCatalogDataset) -> FetchResult:
        return FetchResult(
            request_url=dataset.url,
            final_url=dataset.url,
            status_code=status_code,
            headers={"content-type": "application/vnd.ms-excel"},
            body=base64.b64decode(fixture.read_text()),
            fetched_at=datetime(2026, 9, 11),
            duration_ms=1,
            fetch_method=FetchMethod.HTTP,
        )

    monkeypatch.setattr(connector, "discover_dataset", discover)
    monkeypatch.setattr(connector, "fetch_dataset", fetch)
    result = CliRunner().invoke(cli.app, ["catalog", "smoke"])
    assert result.exit_code == (0 if status_code == 200 else 1), result.output
    payload = json.loads(result.stdout)
    if status_code == 200:
        assert payload["status"] == "SUCCEEDED"
        assert payload["parsed_count"] == payload["accepted_count"] == 8
        assert all(price["quality_status"] == "ACCEPTED" for price in payload["prices"])
    else:
        assert payload["status"] == "FAILED"
        assert "HTTP 403" in payload["error"]


def test_mofcom_smoke_continues_commodities_after_failure(
    offline_cli: FixtureProductConnector,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    connector = MofcomFreshWholesaleConnector()
    registry = CatalogConnectorRegistry()
    registry.register(connector)
    monkeypatch.setattr(cli, "build_catalog_registry", lambda: registry)
    source_url = f"{MOFCOM_FRESH_BASE_URL}?commdityid=170130"
    dataset = DiscoveredCatalogDataset(
        dataset_key="mofcom-bj:170130:2026-08-23",
        url=source_url,
        source_page_url=source_url,
        source_observed_at=datetime(2026, 8, 22, 16),
        metadata={
            "source_date": "2026-08-23",
            "time_precision": "DAY",
            "commodity_code": "CUCUMBER",
            "commodity_id": "170130",
            "commodity_name": "黄瓜",
        },
    )
    fixture = Path(__file__).parents[1] / "fixtures/catalog_fresh/mofcom_cucumber_20260823.html"

    async def discover(
        context: AdapterContext,
        request: CatalogCollectionRequest,
    ) -> DiscoveredCatalogDataset:
        if request.source_item_codes != ("CUCUMBER",):
            raise ValueError("fixture source unavailable")
        return dataset

    async def fetch(context: AdapterContext, item: DiscoveredCatalogDataset) -> FetchResult:
        return FetchResult(
            request_url=source_url,
            final_url=source_url,
            status_code=200,
            headers={"content-type": "text/html"},
            body=fixture.read_bytes(),
            fetched_at=datetime(2026, 9, 11),
            duration_ms=1,
            fetch_method=FetchMethod.HTTP,
        )

    monkeypatch.setattr(connector, "discover_dataset", discover)
    monkeypatch.setattr(connector, "fetch_dataset", fetch)
    result = CliRunner().invoke(
        cli.app,
        [
            "catalog",
            "smoke",
            "--channel",
            "MOFCOM_FRESH_WHOLESALE",
            "--commodity",
            "ALL",
        ],
    )
    assert result.exit_code == 1, result.output
    payload = json.loads(result.stdout)
    assert payload["status"] == "PARTIAL"
    assert payload["dataset_count"] == 15
    assert sum(sample["status"] == "SUCCEEDED" for sample in payload["datasets"]) == 1
