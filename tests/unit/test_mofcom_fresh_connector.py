from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path

import pytest

from device_price_service.crawlers.base import AdapterContext
from device_price_service.crawlers.mofcom_fresh import (
    MOFCOM_FRESH_BASE_URL,
    MOFCOM_SUPPORTED_COMMODITIES,
    MofcomCommodityMapping,
    MofcomFreshSourceError,
    MofcomFreshWholesaleConnector,
)
from device_price_service.domain.catalog_crawl import (
    CatalogCollectionRequest,
    DiscoveredCatalogDataset,
)
from device_price_service.domain.catalog_enums import PriceNature, RegionScope
from device_price_service.domain.crawl import FetchResult
from device_price_service.domain.enums import FetchMethod

FIXTURE_ROOT = Path(__file__).parents[1] / "fixtures" / "catalog_fresh"
PAGE_URL = f"{MOFCOM_FRESH_BASE_URL}?commdityid=170130"
SOURCE_OBSERVED_AT = datetime(2026, 8, 22, 16)


def _fixture(name: str = "mofcom_cucumber_20260823.html") -> bytes:
    return (FIXTURE_ROOT / name).read_bytes()


def _dataset(*, source_date: str = "2026-08-23") -> DiscoveredCatalogDataset:
    observed_at = (
        SOURCE_OBSERVED_AT
        if source_date == "2026-08-23"
        else datetime(2026, 8, 23, 16)
    )
    return DiscoveredCatalogDataset(
        dataset_key=f"mofcom-bj:170130:{source_date}",
        url=PAGE_URL,
        source_page_url=PAGE_URL,
        source_observed_at=observed_at,
        metadata={
            "source_date": source_date,
            "time_precision": "DAY",
            "commodity_code": "CUCUMBER",
            "commodity_id": "170130",
            "commodity_name": "黄瓜",
        },
    )


def _fetch_result(body: bytes | None = None) -> FetchResult:
    return FetchResult(
        request_url=PAGE_URL,
        final_url=PAGE_URL,
        status_code=200,
        headers={"content-type": "text/html; charset=UTF-8"},
        body=body if body is not None else _fixture(),
        fetched_at=datetime(2026, 8, 24, 1),
        duration_ms=8,
        fetch_method=FetchMethod.REPLAY,
    )


def _request(code: str = "CUCUMBER") -> CatalogCollectionRequest:
    return CatalogCollectionRequest(
        region_scope=RegionScope.MULTI,
        region_code="*",
        category_codes=("FRESH_MONITORED_COMMODITY",),
        source_item_codes=(code,),
    )


class _FixtureFetcher:
    async def fetch(
        self,
        url: str,
        *,
        allowed_domains: list[str],
        tls_profile: str = "DEFAULT",
    ) -> FetchResult:
        assert url == PAGE_URL
        assert allowed_domains == ["cif.mofcom.gov.cn"]
        assert tls_profile == "TLS12_COMPAT"
        return _fetch_result()


def test_mofcom_discovery_uses_exact_commodity_and_source_date() -> None:
    connector = MofcomFreshWholesaleConnector()
    fetcher = _FixtureFetcher()
    dataset = asyncio.run(
        connector.discover_dataset(
            AdapterContext(
                http=fetcher,  # type: ignore[arg-type]
                browser=fetcher,  # type: ignore[arg-type]
                allowed_domains=["cif.mofcom.gov.cn"],
            ),
            _request(),
        )
    )

    assert dataset.dataset_key == "mofcom-bj:170130:2026-08-23"
    assert dataset.source_observed_at == SOURCE_OBSERVED_AT
    assert dataset.metadata["commodity_code"] == "CUCUMBER"
    assert dataset.metadata["discovery_hash"] == _fetch_result().source_hash


def test_mofcom_supported_commodities_are_explicit_and_unique() -> None:
    assert [mapping.commodity_code for mapping in MOFCOM_SUPPORTED_COMMODITIES] == [
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
    ]
    assert len({mapping.commodity_id for mapping in MOFCOM_SUPPORTED_COMMODITIES}) == 15
    assert len({mapping.source_name for mapping in MOFCOM_SUPPORTED_COMMODITIES}) == 15


@pytest.mark.parametrize(
    "mapping",
    MOFCOM_SUPPORTED_COMMODITIES,
    ids=lambda mapping: mapping.commodity_code,
)
def test_each_mofcom_mapping_matches_the_exact_page_contract(
    mapping: MofcomCommodityMapping,
) -> None:
    configured = mapping
    page_url = (
        f"{MOFCOM_FRESH_BASE_URL}?commdityid={configured.commodity_id}"
    )
    body = (
        _fixture()
        .decode()
        .replace(
            '<a href="/cif/seach.fhtml?commdityid=170060">大白菜</a>',
            "",
        )
        .replace(
            '<a href="/cif/seach.fhtml?commdityid=170260">胡萝卜</a>',
            "",
        )
        .replace("170130", configured.commodity_id)
        .replace("黄瓜", configured.source_name)
        .encode()
    )
    dataset = DiscoveredCatalogDataset(
        dataset_key=f"mofcom-bj:{configured.commodity_id}:2026-08-23",
        url=page_url,
        source_page_url=page_url,
        source_observed_at=SOURCE_OBSERVED_AT,
        metadata={
            "source_date": "2026-08-23",
            "time_precision": "DAY",
            "commodity_code": configured.commodity_code,
            "commodity_id": configured.commodity_id,
            "commodity_name": configured.source_name,
        },
    )
    result = replace(
        _fetch_result(body),
        request_url=page_url,
        final_url=page_url,
    )

    parsed = MofcomFreshWholesaleConnector().parse_dataset(dataset, result)

    assert {row.item.external_product_id for row in parsed.rows} == {
        configured.commodity_code
    }
    assert all(
        row.parsed.source_attributes["commodity_group"] == configured.source_group
        for row in parsed.rows
    )


def test_mofcom_sanitized_html_maps_market_prices_and_regions() -> None:
    parsed = MofcomFreshWholesaleConnector().parse_dataset(
        _dataset(),
        _fetch_result(),
    )

    assert len(parsed.rows) == 4
    assert sum(len(row.parsed.price_candidates) for row in parsed.rows) == 5
    by_market = {row.item.merchant.external_merchant_id: row for row in parsed.rows}
    assert set(by_market) == {"1001", "2001", "3001", "4001"}
    assert by_market["1001"].item.price_nature is PriceNature.WHOLESALE_AVERAGE
    beijing = by_market["1001"].parsed.price_candidates[0]
    assert beijing.current_price == Decimal("8.50")
    assert beijing.original_price is None
    assert beijing.region is not None
    assert beijing.region.scope is RegionScope.PROVINCE
    assert beijing.region.code == "110000"
    assert beijing.source_observed_at == SOURCE_OBSERVED_AT
    assert beijing.evidence_hash is not None
    assert len(beijing.evidence_hash) == 64
    xinjiang_regions = {
        candidate.region.code
        for candidate in by_market["4001"].parsed.price_candidates
        if candidate.region is not None
    }
    assert xinjiang_regions == {"650000", "XJ_CORPS"}
    assert len(
        {
            candidate.evidence_hash
            for row in parsed.rows
            for candidate in row.parsed.price_candidates
        }
    ) == 5


@pytest.mark.parametrize(
    ("old", "new", "message"),
    [
        ("单位：元/公斤", "单位：元/500克", "commodity or unit"),
        ("北京市", "未知地区", "unsupported MOFCOM region"),
        ("Edate=2026-08-23", "Edate=2026-08-22", "chart identity"),
        ("<td>8.50</td>", "<td>约8.50</td>", "current price"),
    ],
)
def test_mofcom_parser_rejects_unmapped_or_malformed_source_rows(
    old: str,
    new: str,
    message: str,
) -> None:
    body = _fixture().decode().replace(old, new, 1).encode()

    with pytest.raises(MofcomFreshSourceError, match=message):
        MofcomFreshWholesaleConnector().parse_dataset(
            _dataset(),
            _fetch_result(body),
        )


def test_mofcom_parser_rejects_date_change_after_discovery() -> None:
    with pytest.raises(MofcomFreshSourceError, match="changed between"):
        MofcomFreshWholesaleConnector().parse_dataset(
            _dataset(source_date="2026-08-24"),
            _fetch_result(),
        )


def test_mofcom_request_requires_one_supported_source_item() -> None:
    connector = MofcomFreshWholesaleConnector()
    fetcher = _FixtureFetcher()

    with pytest.raises(MofcomFreshSourceError, match="unsupported"):
        asyncio.run(
            connector.discover_dataset(
                AdapterContext(
                    http=fetcher,  # type: ignore[arg-type]
                    browser=fetcher,  # type: ignore[arg-type]
                    allowed_domains=["cif.mofcom.gov.cn"],
                ),
                _request("UNKNOWN_ITEM"),
            )
        )


def test_mofcom_page_identity_is_exact() -> None:
    connector = MofcomFreshWholesaleConnector()
    mapping = connector._mapping_for_request(_request())

    assert connector._parse_page_identity(_fixture(), mapping) == date(2026, 8, 23)
