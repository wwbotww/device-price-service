import gzip
from datetime import datetime
from pathlib import Path

import pytest

from device_price_service.domain.crawl import FetchResult
from device_price_service.domain.enums import FetchMethod
from device_price_service.services.artifact_store import (
    ArtifactError,
    ArtifactIntegrityError,
    RawArtifactStore,
)


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
    first = store.save(source_code="APPLE_CN_WEB", crawl_run_id=7, result=_result())
    second = store.save(source_code="APPLE_CN_WEB", crawl_run_id=7, result=_result())

    assert first == second
    assert store.load(first.relative_path, expected_hash=first.source_hash) == _result().body


def test_artifact_store_blocks_path_traversal(tmp_path: Path) -> None:
    store = RawArtifactStore(tmp_path / "raw")
    with pytest.raises(ArtifactError, match="escapes"):
        store.load("../../secret")


def test_read_only_artifact_access_does_not_create_storage_directories(tmp_path: Path) -> None:
    root = tmp_path / "not-created"
    store = RawArtifactStore(root)
    with pytest.raises(ArtifactError, match="does not exist"):
        store.load("missing.html.gz")
    assert not root.exists()


@pytest.mark.parametrize("body", [b"not gzip", gzip.compress(b"evidence")[:-5]])
def test_corrupt_compressed_evidence_reports_integrity_error(tmp_path: Path, body: bytes) -> None:
    (tmp_path / "corrupt.gz").write_bytes(body)
    with pytest.raises(ArtifactIntegrityError, match="gzip is corrupt"):
        RawArtifactStore(tmp_path).load("corrupt.gz")


def test_artifact_store_preserves_xls_evidence_type(tmp_path: Path) -> None:
    result = FetchResult(
        request_url="https://example.test/prices.xls",
        final_url="https://example.test/prices.xls",
        status_code=200,
        headers={"content-type": "application/vnd.ms-excel"},
        body=bytes.fromhex("D0CF11E0A1B11AE1"),
        fetched_at=datetime(2026, 8, 25, 8),
        duration_ms=4,
        fetch_method=FetchMethod.HTTP,
    )

    artifact = RawArtifactStore(tmp_path).save(
        source_code="SH_FGW_FRESH_RETAIL",
        crawl_run_id=8,
        result=result,
    )

    assert artifact.relative_path.endswith(".xls.gz")
