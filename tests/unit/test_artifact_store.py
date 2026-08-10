from datetime import datetime
from pathlib import Path

import pytest

from device_price_service.domain.crawl import FetchResult
from device_price_service.domain.enums import FetchMethod
from device_price_service.services.artifact_store import ArtifactError, RawArtifactStore


def _result(body: bytes = b"<html>price</html>") -> FetchResult:
    return FetchResult(
        request_url="https://shop.example.cn/p/1",
        final_url="https://shop.example.cn/p/1",
        status_code=200,
        headers={"content-type": "text/html; charset=utf-8"},
        body=body,
        fetched_at=datetime(2026, 8, 8, 8),
        duration_ms=12,
        fetch_method=FetchMethod.HTTP,
    )


def test_artifact_store_round_trip_and_idempotency(tmp_path: Path) -> None:
    store = RawArtifactStore(tmp_path)
    first = store.save(brand_code="APPLE", crawl_run_id=7, result=_result())
    second = store.save(brand_code="APPLE", crawl_run_id=7, result=_result())

    assert first == second
    assert store.load(first.relative_path, expected_hash=first.source_hash) == _result().body


def test_artifact_store_blocks_path_traversal(tmp_path: Path) -> None:
    store = RawArtifactStore(tmp_path / "raw")
    with pytest.raises(ArtifactError, match="escapes"):
        store.load("../../secret")
