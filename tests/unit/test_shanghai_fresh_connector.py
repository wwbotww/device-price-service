from __future__ import annotations

import base64
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path

import pytest

from device_price_service.crawlers.shanghai_fresh import (
    ShanghaiFreshRetailConnector,
    ShanghaiFreshSourceError,
)
from device_price_service.domain.catalog_crawl import DiscoveredCatalogDataset
from device_price_service.domain.catalog_enums import PriceNature
from device_price_service.domain.crawl import FetchResult
from device_price_service.domain.enums import FetchMethod

FIXTURE_ROOT = Path(__file__).parents[1] / "fixtures" / "catalog_fresh"
SOURCE_OBSERVED_AT = datetime(2026, 8, 18, 16)


def _dataset(*, source_date: str = "2026-08-19") -> DiscoveredCatalogDataset:
    observed_at = (
        SOURCE_OBSERVED_AT
        if source_date == "2026-08-19"
        else datetime(2026, 8, 19, 16)
    )
    return DiscoveredCatalogDataset(
        dataset_key=f"shanghai-fresh-retail:{source_date}",
        url="https://fgw.sh.gov.cn/cmsres/sanitized/shanghai-fresh.xls",
        source_page_url="https://fgw.sh.gov.cn/fgw_jgjgdt/20260819/fixture.html",
        source_observed_at=observed_at,
        metadata={"source_date": source_date, "time_precision": "DAY"},
    )


def _xls_bytes() -> bytes:
    encoded = (FIXTURE_ROOT / "shanghai_daily_20260819.xls.b64").read_text()
    return base64.b64decode(encoded)


def _fetch_result(body: bytes | None = None) -> FetchResult:
    return FetchResult(
        request_url="https://fgw.sh.gov.cn/cmsres/sanitized/shanghai-fresh.xls",
        final_url="https://fgw.sh.gov.cn/cmsres/sanitized/shanghai-fresh.xls",
        status_code=200,
        headers={"content-type": "application/vnd.ms-excel"},
        body=body if body is not None else _xls_bytes(),
        fetched_at=datetime(2026, 8, 20, 1),
        duration_ms=8,
        fetch_method=FetchMethod.REPLAY,
    )


def test_shanghai_index_and_article_discovery_are_exact_and_latest() -> None:
    connector = ShanghaiFreshRetailConnector()

    source_date, article_url, title = connector.parse_index(
        (FIXTURE_ROOT / "shanghai_index.html").read_bytes()
    )
    attachment_url = connector.parse_article(
        (FIXTURE_ROOT / "shanghai_article.html").read_bytes(),
        article_url=article_url,
        expected_date=source_date,
    )

    assert source_date == date(2026, 8, 19)
    assert article_url.endswith("/fgw_jgjgdt/20260819/fixture.html")
    assert title.endswith("(2026年08月19日)")
    assert attachment_url == (
        "https://fgw.sh.gov.cn/cmsres/sanitized/shanghai-fresh-20260819.xls"
    )


def test_shanghai_sanitized_xls_maps_eight_published_prices() -> None:
    parsed = ShanghaiFreshRetailConnector().parse_dataset(_dataset(), _fetch_result())

    assert len(parsed.rows) == 8
    prices = {
        row.item.external_product_id: row.parsed.price_candidates[0].current_price
        for row in parsed.rows
    }
    assert prices == {
        "QINGCAI": Decimal("4.22"),
        "JIMAOCAI": Decimal("6.41"),
        "NAPA_CABBAGE": Decimal("2.10"),
        "CUCUMBER": Decimal("4.58"),
        "CARROT": Decimal("2.81"),
        "RED_FUJI_APPLE": Decimal("6.22"),
        "CHICKEN_EGG": Decimal("5.87"),
        "LEAN_PORK": Decimal("17.48"),
    }
    for row in parsed.rows:
        candidate = row.parsed.price_candidates[0]
        assert row.item.price_nature is PriceNature.RETAIL_AVERAGE
        assert candidate.original_price is None
        assert candidate.source_observed_at == SOURCE_OBSERVED_AT
        assert candidate.evidence_hash is not None
        assert len(candidate.evidence_hash) == 64
        assert row.parsed.source_attributes["time_precision"] == "DAY"
    assert len(
        {
            row.parsed.price_candidates[0].evidence_hash
            for row in parsed.rows
        }
    ) == 8


def test_shanghai_parser_rejects_date_disagreement() -> None:
    with pytest.raises(ShanghaiFreshSourceError, match="does not match"):
        ShanghaiFreshRetailConnector().parse_dataset(
            _dataset(source_date="2026-08-20"),
            _fetch_result(),
        )


@pytest.mark.parametrize(
    ("header", "replacement"),
    [
        ("品种", "未知青菜"),
        ("单位", "元/斤"),
    ],
)
def test_shanghai_parser_rejects_missing_or_unknown_mapping(
    header: str,
    replacement: str,
) -> None:
    connector = ShanghaiFreshRetailConnector()
    rows, formats = connector._read_xls(_xls_bytes())
    row_index = next(index for index, row in enumerate(rows) if row[0] == header)
    rows[row_index][2] = replacement

    with pytest.raises(ShanghaiFreshSourceError, match="exactly one Shanghai column"):
        connector.parse_table(_dataset(), rows=rows, price_formats=formats)


def test_shanghai_parser_rejects_malformed_attachment() -> None:
    with pytest.raises(ShanghaiFreshSourceError, match="not an OLE2 XLS"):
        ShanghaiFreshRetailConnector().parse_dataset(
            _dataset(),
            _fetch_result(b"<html>not an xls</html>"),
        )
