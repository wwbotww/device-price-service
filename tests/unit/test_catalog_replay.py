from __future__ import annotations

import copy
from datetime import datetime
from pathlib import Path

import pytest

from device_price_service.crawlers.apple import AppleCatalogConnector
from device_price_service.crawlers.catalog_builtin import build_catalog_registry
from device_price_service.db.catalog_models import CatalogCrawlRecord, CatalogCrawlRun
from device_price_service.domain.catalog_crawl import (
    CatalogCollectionRequest,
    DiscoveredCatalogProduct,
)
from device_price_service.domain.catalog_enums import RegionScope
from device_price_service.domain.crawl import FetchResult
from device_price_service.domain.enums import FetchMethod
from device_price_service.fetchers.url_policy import UrlPolicy, UrlPolicyError
from device_price_service.normalization.devices import DeviceCategoryRule
from device_price_service.normalization.fresh_food import build_fresh_food_rule_registry
from device_price_service.services.artifact_store import ArtifactIntegrityError, RawArtifactStore
from device_price_service.services.catalog_preparation import prepare_catalog_product
from device_price_service.services.replay_service import ReplayError, ReplayOutcome, ReplayService

WHEN = datetime(2026, 9, 11, 8)
PRODUCT = DiscoveredCatalogProduct(
    external_product_id="iphone-fixture-pro",
    category_code="PHONE",
    url="https://www.apple.com.cn/shop/buy-iphone/iphone-fixture-pro",
)
REQUEST = CatalogCollectionRequest(
    region_scope=RegionScope.NATIONAL, region_code="CN", category_codes=("PHONE",)
)


def _record() -> tuple[CatalogCrawlRecord, CatalogCrawlRun]:
    record = CatalogCrawlRecord(
        id=1,
        entity_type="PRODUCT",
        entity_key=PRODUCT.external_product_id,
        request_url=PRODUCT.url,
        final_url=PRODUCT.url,
        fetched_at=WHEN,
        artifact_manifest=[
            {
                "role": "discovery_context",
                "channel_code": "APPLE_CN_WEB",
                "brand_code": "APPLE",
                "request": REQUEST.model_dump(mode="json"),
                "discovery": PRODUCT.model_dump(mode="json"),
            }
        ],
    )
    run = CatalogCrawlRun(region_scope="NATIONAL", region_code="CN", category_scope=["PHONE"])
    return record, run


@pytest.mark.parametrize("mutation", ["missing", "duplicate", "brand", "source", "id", "scope"])
def test_product_replay_rejects_missing_or_inconsistent_immutable_context(mutation):
    record, run = _record()
    context = record.artifact_manifest[0]
    if mutation == "missing":
        record.artifact_manifest = []
    elif mutation == "duplicate":
        record.artifact_manifest.append(copy.deepcopy(context))
    elif mutation == "brand":
        context["brand_code"] = "HUAWEI"
    elif mutation == "source":
        context["channel_code"] = "HUAWEI_CN_WEB"
    elif mutation == "id":
        context["discovery"]["external_product_id"] = "other-product"
    else:
        context["request"]["category_codes"] = ["TABLET"]
    with pytest.raises(ReplayError):
        ReplayService._restore_context(record, run, AppleCatalogConnector())


def test_product_context_does_not_need_current_listing_or_live_category_enabled():
    record, run = _record()
    request, product, recovered = ReplayService._restore_context(
        record, run, AppleCatalogConnector()
    )
    assert request == REQUEST and product == PRODUCT and not recovered


@pytest.mark.parametrize("manifest", [{}, [1], [{"url": 42}]])
def test_malformed_manifest_has_a_clear_replay_error(tmp_path, manifest):
    record, _ = _record()
    record.artifact_manifest = manifest
    service = ReplayService(
        session_factory=lambda: None,
        artifact_store=RawArtifactStore(tmp_path),
        registry=build_catalog_registry(),
        rule_registry=build_fresh_food_rule_registry(),
    )
    with pytest.raises(ReplayError):
        service._verify_manifest(record, UrlPolicy(["www.apple.com.cn"]))


@pytest.mark.parametrize("mutation", ["hash", "redirect", "time", "missing_hash", "duplicate"])
def test_all_response_evidence_is_checked(tmp_path: Path, mutation):
    store = RawArtifactStore(tmp_path)
    result = FetchResult(PRODUCT.url, PRODUCT.url, 200, {}, b"evidence", WHEN, 1, FetchMethod.HTTP)
    artifact = store.save(source_code="APPLE_CN_WEB", crawl_run_id=1, result=result)
    record, _ = _record()
    record.raw_path, record.raw_hash = artifact.relative_path, artifact.source_hash
    part = {
        "role": "response",
        "url": PRODUCT.url,
        "final_url": PRODUCT.url,
        "fetched_at": WHEN.isoformat(timespec="milliseconds"),
        "source_hash": artifact.source_hash,
        "relative_path": artifact.relative_path,
    }
    record.artifact_manifest.append(part)
    if mutation in {"hash", "redirect", "missing_hash"}:
        part = dict(part, role="price_change_initial_response")
        record.artifact_manifest.append(part)
    if mutation == "hash":
        part["source_hash"] = "f" * 64
    elif mutation == "redirect":
        part["final_url"] = "https://evil.example/price"
    elif mutation == "time":
        part["fetched_at"] = "2020-01-01T00:00:00.000"
    elif mutation == "missing_hash":
        del part["source_hash"]
    else:
        record.artifact_manifest.append(copy.deepcopy(part))
    service = ReplayService(
        session_factory=lambda: None,
        artifact_store=store,
        registry=build_catalog_registry(),
        rule_registry=build_fresh_food_rule_registry(),
    )
    with pytest.raises((ReplayError, ArtifactIntegrityError, UrlPolicyError)):
        service._verify_manifest(record, UrlPolicy(["www.apple.com.cn"]))


def test_unconfirmed_jump_is_not_upgraded_by_static_parse():
    body = (Path(__file__).parents[1] / "fixtures/apple/product_iphone.html").read_bytes()
    result = FetchResult(
        PRODUCT.url,
        PRODUCT.url,
        200,
        {"content-type": "text/html"},
        body,
        WHEN,
        1,
        FetchMethod.REPLAY,
    )
    connector = AppleCatalogConnector()
    product = connector.parse_product(PRODUCT, result)
    prepared = prepare_catalog_product(
        product,
        discovered=PRODUCT,
        expected_brand_code="APPLE",
        request=REQUEST,
        allowed_domains=connector.allowed_domains,
        rule_for_category=lambda _: DeviceCategoryRule(),
    )
    outcome = ReplayOutcome(
        1,
        "APPLE_CN_WEB",
        WHEN,
        connector.version,
        {"error_code": "PRICE_CHANGE_CONFIRMATION_REQUIRED", "validation_status": "PENDING"},
        prepared,
        [],
    ).to_dict()
    assert outcome["status"] == "PARTIAL"
    assert outcome["static_validation"]["status"] == "SUCCEEDED"
    assert outcome["historical_validation"]["validation_status"] == "PENDING"
    assert len(outcome["static_validation"]["rows"]) == 3
    assert all(
        price["observed_at"] == WHEN.isoformat(timespec="milliseconds")
        for row in outcome["static_validation"]["rows"]
        for price in row["prices"]
    )
