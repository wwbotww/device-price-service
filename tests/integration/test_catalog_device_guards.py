from __future__ import annotations

import asyncio
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.parse import urlsplit

import pytest
from sqlalchemy import Engine, select
from sqlalchemy.orm import Session, sessionmaker

from device_price_service.crawlers.apple import AppleCatalogConnector
from device_price_service.crawlers.base import AdapterContext
from device_price_service.crawlers.mofcom_fresh import MofcomFreshWholesaleConnector
from device_price_service.db.catalog_models import (
    CatalogCrawlRecord,
    CatalogCrawlRun,
    CatalogItem,
    CatalogPriceCurrent,
    CatalogPriceObservationRecord,
    SourceChannel,
    SourceListing,
)
from device_price_service.db.catalog_repositories import (
    CatalogCrawlRepository,
    CatalogPriceRepository,
)
from device_price_service.db.catalog_seed import seed_mofcom_fresh_source
from device_price_service.db.device_seed import seed_device_catalog
from device_price_service.domain.catalog_crawl import (
    CatalogCollectionRequest,
    DiscoveredCatalogProduct,
    ParsedCatalogProduct,
    SourcePriceCandidate,
)
from device_price_service.domain.catalog_enums import (
    Availability,
    CollectionTriggerType,
    FeeStatus,
    PriceType,
    PricingBasis,
    RegionScope,
    RunStatus,
    RunType,
)
from device_price_service.domain.crawl import FetchResult
from device_price_service.domain.enums import FetchMethod
from device_price_service.runtime import build_catalog_rule_registry
from device_price_service.services.artifact_store import RawArtifactStore
from device_price_service.services.catalog_crawl_pipeline import CatalogCrawlPipeline
from device_price_service.services.catalog_device_guards import CatalogDeviceGuards

pytestmark = pytest.mark.integration
BASE_TIME = datetime(2026, 9, 11, 8)
BODY = (Path(__file__).parents[1] / "fixtures/apple/product_iphone.html").read_bytes()
PRODUCT = DiscoveredCatalogProduct(
    external_product_id="iphone-fixture-pro",
    category_code="PHONE",
    url="https://www.apple.com.cn/shop/buy-iphone/iphone-fixture-pro",
)
SECOND = PRODUCT.model_copy(
    update={
        "external_product_id": "iphone-fixture-second",
        "url": "https://www.apple.com.cn/shop/buy-iphone/iphone-fixture-second",
    }
)
FULL = CatalogCollectionRequest(
    region_scope=RegionScope.NATIONAL,
    region_code="CN",
    category_codes=AppleCatalogConnector.default_category_codes,
)


def response(hour: int, body: bytes = BODY, *, status: int = 200) -> FetchResult:
    return FetchResult(
        request_url=PRODUCT.url,
        final_url=PRODUCT.url,
        status_code=status,
        headers={"content-type": "text/html"},
        body=body,
        fetched_at=BASE_TIME + timedelta(hours=hour),
        duration_ms=1,
        fetch_method=FetchMethod.HTTP,
    )


class QueueFetcher:
    def __init__(self) -> None:
        self.responses: list[FetchResult | BaseException] = []
        self.calls: list[str] = []

    async def fetch(self, url: str, *, allowed_domains: list[str]) -> FetchResult:
        self.calls.append(url)
        next_result = self.responses.pop(0)
        if isinstance(next_result, BaseException):
            raise next_result
        return replace(
            next_result,
            request_url=url,
            final_url=url if next_result.final_url == PRODUCT.url else next_result.final_url,
            body=next_result.body.replace(
                PRODUCT.external_product_id.encode(), urlsplit(url).path.rsplit("/", 1)[-1].encode()
            ),
        )


class FixedConnector(AppleCatalogConnector):
    def __init__(self, products: list[DiscoveredCatalogProduct]) -> None:
        super().__init__()
        self.products = products
        self.state_only = False

    async def discover_products(
        self, context: AdapterContext, request: CatalogCollectionRequest
    ) -> list[DiscoveredCatalogProduct]:
        return self.products

    def parse_product(
        self, item: DiscoveredCatalogProduct, result: FetchResult
    ) -> ParsedCatalogProduct:
        product = super().parse_product(item, result)
        if not self.state_only:
            return product
        return product.model_copy(
            update={
                "rows": [
                    row.model_copy(
                        update={
                            "parsed": row.parsed.model_copy(
                                update={
                                    "price_candidates": [
                                        SourcePriceCandidate(
                                            price_type=PriceType.AVAILABILITY_ONLY,
                                            pricing_basis=PricingBasis.UNKNOWN,
                                            fee_status=FeeStatus.NOT_APPLICABLE,
                                            availability=Availability.OFF_SHELF,
                                        )
                                    ]
                                }
                            )
                        }
                    )
                    for row in product.rows
                ]
            }
        )


@dataclass
class Harness:
    pipeline: CatalogCrawlPipeline
    fetcher: QueueFetcher
    connector: FixedConnector
    factory: sessionmaker[Session]
    raw_path: Path

    def run(self, *responses: FetchResult | BaseException, request=FULL, **kwargs):
        self.fetcher.responses.extend(responses)
        return asyncio.run(self.pipeline.run(self.connector, request, **kwargs))

    def current(self):
        with self.factory() as session:
            return [
                (
                    listing.external_product_id,
                    fact.current_price,
                    fact.availability,
                    fact.observed_at,
                )
                for listing, fact in session.execute(
                    select(SourceListing, CatalogPriceObservationRecord)
                    .join(
                        CatalogPriceCurrent,
                        CatalogPriceCurrent.source_listing_id == SourceListing.id,
                    )
                    .join(
                        CatalogPriceObservationRecord,
                        CatalogPriceObservationRecord.id
                        == CatalogPriceCurrent.price_observation_id,
                    )
                    .order_by(SourceListing.id)
                )
            ]

    def listings(self):
        with self.factory() as session:
            return [
                (row.external_product_id, row.lifecycle_status, row.consecutive_misses)
                for row in session.scalars(select(SourceListing).order_by(SourceListing.id))
            ]


@pytest.fixture()
def harness(mysql_engine: Engine, migrated_session_factory: sessionmaker[Session], tmp_path: Path):
    with migrated_session_factory.begin() as session:
        seed_device_catalog(session, enable=True)
    fetcher = QueueFetcher()
    raw_path = tmp_path / "raw"
    pipeline = CatalogCrawlPipeline(
        engine=mysql_engine,
        session_factory=migrated_session_factory,
        http_fetcher=fetcher,
        browser_fetcher=fetcher,
        artifact_store=RawArtifactStore(raw_path),
        category_rules=build_catalog_rule_registry(),
        missing_confirmation_runs=2,
    )
    return Harness(pipeline, fetcher, FixedConnector([PRODUCT]), migrated_session_factory, raw_path)


def test_large_change_refetches_whole_product_once_and_retains_both_evidences(harness):
    assert harness.run(response(0)).status is RunStatus.SUCCEEDED
    changed = BODY.replace(b"8,999", b"4,999")
    outcome = harness.run(response(1, changed), response(2, changed))
    assert outcome.status is RunStatus.SUCCEEDED
    assert outcome.fetched_count == 2 and outcome.accepted_count == 3
    assert len(harness.fetcher.calls) == 3  # Not one request per SKU.
    assert str(harness.current()[0][1]) == "4999.00"
    with harness.factory() as session:
        records = session.scalars(
            select(CatalogCrawlRecord)
            .where(CatalogCrawlRecord.crawl_run_id == outcome.crawl_run_id)
            .order_by(CatalogCrawlRecord.id)
        ).all()
        assert len(records) == 2
        assert records[0].validation_status == "PENDING"
        assert records[1].validation_status == "SUCCEEDED"
        assert any(
            part["role"] == "price_change_initial_response" for part in records[1].artifact_manifest
        )
        for record in records:
            assert (
                RawArtifactStore(harness.raw_path).load(
                    record.raw_path, expected_hash=record.raw_hash
                )
                == changed
            )
        assert all(
            row.observed_at == BASE_TIME + timedelta(hours=2)
            for row in session.scalars(
                select(CatalogPriceObservationRecord).where(
                    CatalogPriceObservationRecord.crawl_record_id == records[1].id
                )
            )
        )


@pytest.mark.parametrize("confirmation", ["price", "spec", "status", "old_time", "http", "network"])
def test_unconfirmed_large_change_never_replaces_current(harness, confirmation):
    harness.run(response(0))
    before = harness.current()
    changed = BODY.replace(b"8,999", b"4,999")
    second = response(2, changed)
    if confirmation == "price":
        second = response(2, changed.replace(b"4,999", b"4,599"))
    elif confirmation == "spec":
        second = response(2, changed.replace(b"256<", b"128<"))
    elif confirmation == "status":
        second = response(
            2, changed.replace(b'<div class="item">', '<div class="item">暂时缺货'.encode())
        )
    elif confirmation == "old_time":
        second = response(1, changed)
    elif confirmation == "http":
        second = response(2, b"Forbidden", status=403)
    elif confirmation == "network":
        second = OSError("fixture timeout")
    outcome = harness.run(response(1, changed), second)
    assert outcome.status is RunStatus.FAILED
    assert harness.current() == before
    assert outcome.accepted_count == 0
    assert (
        outcome.review_count == 3
        if confirmation not in {"http", "network"}
        else outcome.failed_count == 1
    )


def test_replayed_large_change_never_fetches_confirmation(harness):
    harness.run(response(0))
    before = harness.current()
    outcome = harness.run(
        replace(response(1, BODY.replace(b"8,999", b"4,999")), fetch_method=FetchMethod.REPLAY)
    )
    assert outcome.status is RunStatus.FAILED
    assert outcome.review_count == 3 and outcome.fetched_count == 1
    assert harness.current() == before


def test_last_priced_fact_is_baseline_even_after_availability_only(harness):
    harness.run(response(0))
    harness.connector.state_only = True
    assert harness.run(response(1)).status is RunStatus.SUCCEEDED
    assert all(price is None and state == "OFF_SHELF" for _, price, state, _ in harness.current())
    assert all(state == "INACTIVE" for _, state, _ in harness.listings())
    harness.connector.state_only = False
    changed = BODY.replace(b"8,999", b"4,999")
    outcome = harness.run(response(2, changed), response(3, changed))
    assert outcome.status is RunStatus.SUCCEEDED and outcome.fetched_count == 2
    assert all(state == "ACTIVE" for _, state, _ in harness.listings())


def seed_two_products(harness):
    harness.connector.products = [PRODUCT, SECOND]
    assert harness.run(response(0), response(0)).status is RunStatus.SUCCEEDED
    harness.connector.products = [PRODUCT]


def test_missing_product_needs_multiple_complete_runs_and_detail_evidence(harness):
    seed_two_products(harness)
    assert harness.run(response(1)).status is RunStatus.SUCCEEDED
    assert [
        misses for product, _, misses in harness.listings() if product == SECOND.external_product_id
    ] == [1] * 3
    assert len(harness.fetcher.calls) == 3
    outcome = harness.run(response(2), response(2, b"gone", status=404))
    assert outcome.status is RunStatus.SUCCEEDED
    assert outcome.fetched_count == 2 and outcome.accepted_count == 6
    assert [
        (price, state)
        for product, price, state, _ in harness.current()
        if product == SECOND.external_product_id
    ] == [(None, "OFF_SHELF")] * 3
    assert [
        (state, misses)
        for product, state, misses in harness.listings()
        if product == SECOND.external_product_id
    ] == [("INACTIVE", 2)] * 3
    before = harness.current()
    with harness.factory.begin() as session:
        assert all(item.status == "ACTIVE" for item in session.scalars(select(CatalogItem)))
        for listing_id in session.scalars(select(SourceListing.id)):
            CatalogPriceRepository(session).rebuild_current(source_listing_id=listing_id)
    assert harness.current() == before
    harness.connector.products = [PRODUCT, SECOND]
    assert harness.run(response(3), response(3)).status is RunStatus.SUCCEEDED
    assert all(state == "ACTIVE" and misses == 0 for _, state, misses in harness.listings())


@pytest.mark.parametrize(
    "failure", ["403", "no_response", "redirect", "network", "sku_endpoint", "old_404"]
)
def test_missing_confirmation_failure_keeps_last_trusted_prices(harness, failure):
    seed_two_products(harness)
    harness.run(response(1))
    previous = [row for row in harness.current() if row[0] == SECOND.external_product_id]
    result = response(2, b"gone", status=404)
    if failure == "403":
        result = response(2, b"Forbidden", status=403)
    elif failure == "no_response":
        result = response(2, b"No HTTP response", status=0)
    elif failure == "redirect":
        result = replace(result, final_url="https://www.apple.com.cn/shop/login")
    elif failure == "network":
        result = OSError("fixture timeout")
    elif failure == "sku_endpoint":
        harness.connector.product_not_found_is_definitive = False
    elif failure == "old_404":
        result = response(0, b"gone", status=404)
    outcome = harness.run(response(2), result)
    assert outcome.status is RunStatus.PARTIAL and outcome.failed_count == 1
    assert [row for row in harness.current() if row[0] == SECOND.external_product_id] == previous
    assert all(state == "ACTIVE" for _, state, _ in harness.listings())


@pytest.mark.parametrize("scope", ["category", "replay", "old", "query", "partial"])
def test_non_full_or_untrusted_runs_do_not_increment_missing(harness, scope):
    seed_two_products(harness)
    kwargs = {}
    result = response(1)
    if scope == "category":
        kwargs["request"] = FULL.model_copy(update={"category_codes": ("PHONE",)})
    elif scope == "replay":
        result = replace(result, fetch_method=FetchMethod.REPLAY)
    elif scope == "old":
        result = response(0)
    elif scope == "query":
        kwargs["trigger_type"] = CollectionTriggerType.QUERY_RECRAWL
    elif scope == "partial":
        result = response(1, b"Forbidden", status=403)
    harness.run(result, **kwargs)
    assert all(misses == 0 for _, _, misses in harness.listings())


def test_discovery_floor_counts_products_and_rejects_before_price_writes(harness):
    seed_two_products(harness)
    # Six existing SKUs are two products, so discovering one product passes the default 50% floor.
    assert harness.run(response(1)).status is RunStatus.SUCCEEDED
    before = harness.current(), harness.listings()
    harness.pipeline.discovery_count_floor_ratio = 1
    outcome = harness.run()
    assert outcome.status is RunStatus.FAILED and outcome.fetched_count == 0
    assert (harness.current(), harness.listings()) == before
    with harness.factory() as session:
        run = session.get(CatalogCrawlRun, outcome.crawl_run_id)
        assert run.error_summary == {"DISCOVERY_COUNT_BELOW_FLOOR": 1}


def test_missing_sku_marks_partial_without_retiring_or_clearing_it(harness):
    harness.run(response(0))
    # Simulate a parser returning a legitimate subset; do not alter its remaining prices.
    parse = harness.connector.parse_product
    harness.connector.parse_product = lambda item, result: (
        product := parse(item, result)
    ).model_copy(update={"rows": product.rows[:2]})
    outcome = harness.run(response(1))
    assert outcome.status is RunStatus.PARTIAL and outcome.accepted_count == 2
    assert harness.current()[2][3] == BASE_TIME
    assert all(state == "ACTIVE" and misses == 0 for _, state, misses in harness.listings())
    with harness.factory() as session:
        record = session.scalar(
            select(CatalogCrawlRecord).where(
                CatalogCrawlRecord.crawl_run_id == outcome.crawl_run_id
            )
        )
        assert record.error_code == "SKU_COVERAGE_INCOMPLETE"
        assert record.validation_status == "PENDING"


def test_stale_recovery_only_changes_same_source_region_and_category_scope(harness):
    ids = []
    with harness.factory.begin() as session:
        apple = session.scalar(select(SourceChannel).where(SourceChannel.code == "APPLE_CN_WEB"))
        xiaomi = session.scalar(select(SourceChannel).where(SourceChannel.code == "XIAOMI_CN_WEB"))
        for channel, categories, region, code, age in [
            (apple.id, list(reversed(FULL.category_codes)), RegionScope.NATIONAL, "CN", 4),
            (apple.id, ["PHONE"], RegionScope.NATIONAL, "CN", 4),
            (xiaomi.id, list(FULL.category_codes), RegionScope.NATIONAL, "CN", 4),
            (apple.id, list(FULL.category_codes), RegionScope.PROVINCE, "310000", 4),
            (apple.id, list(FULL.category_codes), RegionScope.NATIONAL, "CN", 0),
        ]:
            run = CatalogCrawlRepository(session).start_run(
                source_channel_id=channel,
                region_scope=region,
                region_code=code,
                category_scope=categories,
                run_type=RunType.FULL,
                trigger_type=CollectionTriggerType.MANUAL,
                adapter_version="fixture",
                policy_version="fixture",
                started_at=datetime.now(UTC).replace(tzinfo=None) - timedelta(hours=age),
            )
            ids.append(run.id)
    harness.run(response(0))
    with harness.factory() as session:
        runs = [session.get(CatalogCrawlRun, identifier) for identifier in ids]
        assert runs[0].status == "FAILED" and runs[0].error_summary == {
            "STALE_RUNNING_RECOVERED": 1
        }
        assert all(run.status == "RUNNING" for run in runs[1:])


def test_cancellation_finishes_batch_and_keeps_current(harness):
    harness.run(response(0))
    before = harness.current()
    with pytest.raises(asyncio.CancelledError):
        harness.run(asyncio.CancelledError())
    assert harness.current() == before
    with harness.factory() as session:
        run = session.scalar(select(CatalogCrawlRun).order_by(CatalogCrawlRun.id.desc()))
        assert run.status == "CANCELLED" and run.finished_at is not None


def test_missing_confirmation_uses_latest_trusted_context_not_revision_creation(harness):
    harness.run(response(0))
    harness.connector.products = [
        PRODUCT.model_copy(
            update={
                "metadata": {"detail_context": "new"},
                "url": PRODUCT.url + "?view=updated",
            }
        )
    ]
    harness.run(response(1))
    # Changing only discovery context does not change specification or create a revision.
    with harness.factory() as session:
        channel = session.scalar(select(SourceChannel).where(SourceChannel.code == "APPLE_CN_WEB"))
        channel_id = channel.id
    product = CatalogDeviceGuards(harness.factory).snapshot(channel_id)[PRODUCT.external_product_id]
    assert product.discovery.metadata == {"detail_context": "new"}
    assert product.discovery.url == PRODUCT.url + "?view=updated"
    # A later failed fetch must not replace the usable context.
    harness.connector.products = [
        PRODUCT.model_copy(update={"metadata": {"detail_context": "untrusted"}})
    ]
    harness.run(response(2, b"Forbidden", status=403))
    product = CatalogDeviceGuards(harness.factory).snapshot(channel_id)[PRODUCT.external_product_id]
    assert product.discovery.metadata == {"detail_context": "new"}
    assert product.discovery.url == PRODUCT.url + "?view=updated"


def test_same_millisecond_is_not_a_new_discovery_for_missing_counts(harness):
    seed_two_products(harness)
    harness.run(replace(response(0), fetched_at=BASE_TIME + timedelta(microseconds=500)))
    assert all(misses == 0 for _, _, misses in harness.listings())
    assert len(harness.fetcher.calls) == 3


def test_device_stale_recovery_does_not_touch_government_commodity_runs(harness):
    connector = MofcomFreshWholesaleConnector()
    request = CatalogCollectionRequest(
        region_scope=RegionScope.MULTI,
        region_code="*",
        category_codes=("FRESH_MONITORED_COMMODITY",),
        source_item_codes=("BEEF_LEG",),
    )
    with harness.factory.begin() as session:
        channel = seed_mofcom_fresh_source(session, enable=True)
        channel_id = channel.id
        old = CatalogCrawlRepository(session).start_run(
            source_channel_id=channel_id,
            region_scope=request.region_scope,
            region_code=request.region_code,
            category_scope=list(request.category_codes),
            run_type=RunType.FULL,
            trigger_type=CollectionTriggerType.MANUAL,
            adapter_version=connector.version,
            policy_version=harness.pipeline.price_policy.version,
            started_at=datetime.now(UTC).replace(tzinfo=None) - timedelta(hours=4),
        )
        old_id = old.id
    harness.pipeline._start_run(connector, request, CollectionTriggerType.MANUAL, channel_id)
    with harness.factory() as session:
        assert session.get(CatalogCrawlRun, old_id).status == "RUNNING"
