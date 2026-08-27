from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
from decimal import Decimal
from math import ceil
from typing import NoReturn

import structlog
from sqlalchemy import Engine, select
from sqlalchemy.orm import Session, sessionmaker

from device_price_service.crawlers.base import (
    AdapterContext,
    BrandAdapter,
    BrowserSnapshotFetcher,
    Fetcher,
)
from device_price_service.db.models import CrawlRun
from device_price_service.db.repositories import (
    CatalogRepository,
    CrawlRecordRepository,
    CrawlRunRepository,
    MissingProductCandidate,
    PriceRepository,
)
from device_price_service.domain.crawl import (
    ArtifactReference,
    CrawlOutcome,
    DiscoveredProduct,
    FetchResult,
    NormalizedProduct,
)
from device_price_service.domain.enums import (
    EntityType,
    FetchMethod,
    OperationStatus,
    RunStatus,
    RunType,
    TriggerType,
)
from device_price_service.domain.models import PriceObservation, utc_now_naive
from device_price_service.services.artifact_store import RawArtifactStore
from device_price_service.services.locks import mysql_named_lock
from device_price_service.validation.rules import QualityValidator, ValidationReport


@dataclass(frozen=True, slots=True)
class ChannelRuntime:
    id: int
    allowed_domains: list[str]


class CrawlPipeline:
    def __init__(
        self,
        *,
        engine: Engine,
        session_factory: sessionmaker[Session],
        http_fetcher: Fetcher,
        browser_fetcher: BrowserSnapshotFetcher,
        artifact_store: RawArtifactStore,
        validator: QualityValidator,
        stale_run_after_minutes: int = 120,
        missing_confirmation_runs: int = 3,
        discovery_count_floor_ratio: float = 0.5,
    ) -> None:
        self.engine = engine
        self.session_factory = session_factory
        self.http_fetcher = http_fetcher
        self.browser_fetcher = browser_fetcher
        self.artifact_store = artifact_store
        self.validator = validator
        self.stale_run_after_minutes = stale_run_after_minutes
        self.missing_confirmation_runs = missing_confirmation_runs
        self.discovery_count_floor_ratio = discovery_count_floor_ratio
        self.logger = structlog.get_logger()

    async def run(
        self,
        adapter: BrandAdapter,
        *,
        trigger_type: TriggerType = TriggerType.MANUAL,
    ) -> CrawlOutcome:
        lock_name = f"device-price:{adapter.channel_code}"
        with mysql_named_lock(self.engine, lock_name):
            channel = self._load_channel(adapter.channel_code)
            known_active_products = self._active_product_count(channel.id)
            context = AdapterContext(
                http=self.http_fetcher,
                browser=self.browser_fetcher,
                allowed_domains=channel.allowed_domains,
            )
            run_id = self._start_run(adapter, channel.id, trigger_type)
            try:
                discovered = await adapter.discover(context)
            except Exception:
                self._finish_run(
                    run_id,
                    status=RunStatus.FAILED,
                    discovered=0,
                    success=0,
                    skipped=0,
                    failed=1,
                    error_summary={"DISCOVERY_FAILED": 1},
                )
                self.logger.exception(
                    "crawl_discovery_failed",
                    crawl_run_id=run_id,
                    channel_code=adapter.channel_code,
                )
                raise

            success = 0
            skipped = 0
            failed = 0
            seen_products: set[str] = set()
            discovery_trusted = self._discovery_is_trusted(
                discovered_count=len({item.official_product_id for item in discovered}),
                known_active_products=known_active_products,
            )
            if not discovery_trusted:
                failed += 1
                self.logger.warning(
                    "crawl_discovery_count_guard_failed",
                    crawl_run_id=run_id,
                    channel_code=adapter.channel_code,
                    discovered_count=len(discovered),
                    known_active_products=known_active_products,
                    floor_ratio=self.discovery_count_floor_ratio,
                )

            for item in discovered:
                if item.official_product_id in seen_products:
                    skipped += 1
                    self._record_duplicate(run_id, item)
                    continue
                seen_products.add(item.official_product_id)
                processed = await self._process_item(
                    run_id=run_id,
                    adapter=adapter,
                    context=context,
                    item=item,
                )
                if processed:
                    success += 1
                else:
                    failed += 1

            confirmation_failures = 0
            candidates = self._register_missing_products(channel.id, seen_products)
            for candidate in candidates:
                if not await self._confirm_missing_product(
                    run_id=run_id,
                    adapter=adapter,
                    context=context,
                    channel_id=channel.id,
                    candidate=candidate,
                ):
                    confirmation_failures += 1
            failed += confirmation_failures

            status = self._terminal_status(success=success, failed=failed)
            error_summary: dict[str, int] = {}
            product_failures = failed - confirmation_failures - (0 if discovery_trusted else 1)
            if product_failures:
                error_summary["PRODUCT_FAILED"] = product_failures
            if confirmation_failures:
                error_summary["OFF_SHELF_CONFIRMATION_FAILED"] = confirmation_failures
            if not discovery_trusted:
                error_summary["DISCOVERY_COUNT_GUARD"] = 1
            self._finish_run(
                run_id,
                status=status,
                discovered=len(discovered),
                success=success,
                skipped=skipped,
                failed=failed,
                error_summary=error_summary or None,
            )
            return CrawlOutcome(run_id, len(discovered), success, skipped, failed)

    async def _process_item(
        self,
        *,
        run_id: int,
        adapter: BrandAdapter,
        context: AdapterContext,
        item: DiscoveredProduct,
        prefetched_result: FetchResult | None = None,
    ) -> bool:
        result: FetchResult | None = None
        artifact: ArtifactReference | None = None
        try:
            result = prefetched_result or await adapter.fetch_product(context, item)
            artifact = self.artifact_store.save(
                source_code=adapter.channel_code,
                crawl_run_id=run_id,
                result=result,
            )
            if result.status_code < 200 or result.status_code >= 400:
                self._record_failure(
                    run_id,
                    item,
                    result=result,
                    artifact=artifact,
                    error_code="HTTP_STATUS_ERROR",
                    error_message=f"unexpected HTTP status {result.status_code}",
                    fetch_failed=True,
                )
                return False

            parsed = adapter.parse_product(item, result)
            normalized = adapter.normalize(item, parsed)
            previous_prices = self._load_previous_prices(adapter, item)
            report = self.validator.validate(
                normalized,
                expected_brand_code=adapter.brand_code,
                expected_channel_code=adapter.channel_code,
                allowed_domains=context.allowed_domains,
                previous_prices=previous_prices,
            )
            if not report.is_valid:
                self._record_validation_failure(run_id, item, result, artifact, report)
                return False
            if self._requires_price_confirmation(report):
                self._record_large_change_recheck(run_id, item, result, artifact, report)
                confirmation = await adapter.fetch_product(context, item)
                confirmation_artifact = self.artifact_store.save(
                    source_code=adapter.channel_code,
                    crawl_run_id=run_id,
                    result=confirmation,
                )
                if confirmation.status_code < 200 or confirmation.status_code >= 400:
                    self._record_failure(
                        run_id,
                        item,
                        result=confirmation,
                        artifact=confirmation_artifact,
                        error_code="PRICE_CONFIRMATION_HTTP_ERROR",
                        error_message=(
                            f"unexpected confirmation HTTP status {confirmation.status_code}"
                        ),
                        fetch_failed=True,
                    )
                    return False
                confirmed_parsed = adapter.parse_product(item, confirmation)
                confirmed_normalized = adapter.normalize(item, confirmed_parsed)
                confirmed_report = self.validator.validate(
                    confirmed_normalized,
                    expected_brand_code=adapter.brand_code,
                    expected_channel_code=adapter.channel_code,
                    allowed_domains=context.allowed_domains,
                    previous_prices=previous_prices,
                )
                if not confirmed_report.is_valid:
                    self._record_validation_failure(
                        run_id,
                        item,
                        confirmation,
                        confirmation_artifact,
                        confirmed_report,
                    )
                    return False
                if self._price_signature(normalized) != self._price_signature(confirmed_normalized):
                    self._record_failure(
                        run_id,
                        item,
                        result=confirmation,
                        artifact=confirmation_artifact,
                        error_code="LARGE_PRICE_CHANGE_UNCONFIRMED",
                        error_message="two immediate observations returned different price states",
                        fetch_failed=False,
                    )
                    return False
                result = confirmation
                artifact = confirmation_artifact
                normalized = confirmed_normalized
            self._persist_product(run_id, item, result, artifact, normalized)
            return True
        except Exception as error:
            # Per-product parse failures are already persisted with raw evidence and
            # an error message. Avoid serializing the full parser/browser object graph
            # into routine crawl logs, which can otherwise produce multi-megabyte lines.
            self.logger.error(
                "crawl_product_failed",
                crawl_run_id=run_id,
                entity_key=item.official_product_id,
                error_type=type(error).__name__,
                error_message=str(error),
            )
            self._record_failure(
                run_id,
                item,
                result=result,
                artifact=artifact,
                error_code=type(error).__name__.upper()[:64],
                error_message=str(error),
                fetch_failed=result is None,
            )
            return False

    async def _confirm_missing_product(
        self,
        *,
        run_id: int,
        adapter: BrandAdapter,
        context: AdapterContext,
        channel_id: int,
        candidate: MissingProductCandidate,
    ) -> bool:
        item = DiscoveredProduct(
            official_product_id=candidate.official_product_id,
            url=candidate.url,
            category_code=candidate.category_code,
        )
        try:
            result = await adapter.fetch_product(context, item)
            if result.status_code not in {404, 410}:
                return await self._process_item(
                    run_id=run_id,
                    adapter=adapter,
                    context=context,
                    item=item,
                    prefetched_result=result,
                )
            artifact = self.artifact_store.save(
                source_code=adapter.channel_code,
                crawl_run_id=run_id,
                result=result,
            )
            with self.session_factory.begin() as session:
                CatalogRepository(session).confirm_product_off_shelf(
                    product_id=candidate.product_id,
                    channel_id=channel_id,
                    crawl_run_id=run_id,
                    observed_at=result.fetched_at,
                    source_hash=artifact.source_hash,
                    confirmation_runs=self.missing_confirmation_runs,
                )
                CrawlRecordRepository(session).add(
                    crawl_run_id=run_id,
                    entity_type=EntityType.PRODUCT,
                    entity_key=candidate.official_product_id,
                    request_url=result.request_url,
                    final_url=result.final_url,
                    fetch_method=result.fetch_method,
                    http_status=result.status_code,
                    fetch_status=OperationStatus.SUCCEEDED,
                    parse_status=OperationStatus.SKIPPED,
                    error_code="OFF_SHELF_CONFIRMED",
                    error_message=(
                        f"missing from {self.missing_confirmation_runs} discoveries "
                        f"and detail returned HTTP {result.status_code}"
                    ),
                    raw_hash=artifact.source_hash,
                    raw_path=artifact.relative_path,
                    duration_ms=result.duration_ms,
                    fetched_at=result.fetched_at,
                )
            return True
        except Exception as error:
            self.logger.exception(
                "off_shelf_confirmation_failed",
                crawl_run_id=run_id,
                entity_key=candidate.official_product_id,
            )
            self._record_failure(
                run_id,
                item,
                result=None,
                artifact=None,
                error_code="OFF_SHELF_CONFIRMATION_FAILED",
                error_message=str(error),
                fetch_failed=True,
            )
            return False

    def _persist_product(
        self,
        run_id: int,
        item: DiscoveredProduct,
        result: FetchResult,
        artifact: ArtifactReference,
        normalized: NormalizedProduct,
    ) -> None:
        with self.session_factory.begin() as session:
            catalog = CatalogRepository(session)
            brand = catalog.require_brand(normalized.brand_code)
            category = catalog.require_category(normalized.category_code)
            channel = catalog.require_channel(normalized.channel_code)
            product = catalog.upsert_product(
                brand_id=brand.id,
                category_id=category.id,
                official_product_id=normalized.official_product_id,
                name=normalized.name,
                series_name=normalized.series_name,
                model_number=normalized.model_number,
                official_url=normalized.official_url,
                lifecycle_status=normalized.lifecycle_status,
                observed_at=result.fetched_at,
            )
            for normalized_sku in normalized.skus:
                sku = catalog.upsert_sku(
                    product_id=product.id,
                    official_sku_id=normalized_sku.official_sku_id,
                    name=normalized_sku.name,
                    color=normalized_sku.color,
                    capacity=normalized_sku.capacity,
                    memory=normalized_sku.memory,
                    connectivity=normalized_sku.connectivity,
                    size=normalized_sku.size,
                    attributes=normalized_sku.attributes,
                    spec_fingerprint=normalized_sku.spec_fingerprint,
                    status=normalized_sku.status,
                    observed_at=result.fetched_at,
                )
                for normalized_offer in normalized_sku.offers:
                    offer = catalog.upsert_offer(
                        sku_id=sku.id,
                        channel_id=channel.id,
                        official_offer_id=normalized_offer.official_offer_id,
                        source_url=normalized_offer.source_url,
                        availability=normalized_offer.availability.value,
                        observed_at=result.fetched_at,
                    )
                    PriceRepository(session).record(
                        PriceObservation(
                            offer_id=offer.id,
                            crawl_run_id=run_id,
                            currency=normalized_offer.currency,
                            original_price=normalized_offer.original_price,
                            original_price_type=normalized_offer.original_price_type,
                            current_price=normalized_offer.current_price,
                            availability=normalized_offer.availability,
                            observed_at=result.fetched_at,
                            source_hash=artifact.source_hash,
                        )
                    )
            CrawlRecordRepository(session).add(
                crawl_run_id=run_id,
                entity_type=EntityType.PRODUCT,
                entity_key=item.official_product_id,
                request_url=result.request_url,
                final_url=result.final_url,
                fetch_method=result.fetch_method,
                http_status=result.status_code,
                fetch_status=OperationStatus.SUCCEEDED,
                parse_status=OperationStatus.SUCCEEDED,
                raw_hash=artifact.source_hash,
                raw_path=artifact.relative_path,
                duration_ms=result.duration_ms,
                fetched_at=result.fetched_at,
            )

    def _record_validation_failure(
        self,
        run_id: int,
        item: DiscoveredProduct,
        result: FetchResult,
        artifact: ArtifactReference,
        report: ValidationReport,
    ) -> None:
        errors = [issue for issue in report.issues if issue.severity.value == "ERROR"]
        message = "; ".join(f"{issue.code}@{issue.path}" for issue in errors)
        self._record_failure(
            run_id,
            item,
            result=result,
            artifact=artifact,
            error_code="VALIDATION_FAILED",
            error_message=message,
            fetch_failed=False,
        )

    def _record_large_change_recheck(
        self,
        run_id: int,
        item: DiscoveredProduct,
        result: FetchResult,
        artifact: ArtifactReference,
        report: ValidationReport,
    ) -> None:
        warnings = [issue for issue in report.issues if issue.code == "LARGE_PRICE_CHANGE"]
        with self.session_factory.begin() as session:
            CrawlRecordRepository(session).add(
                crawl_run_id=run_id,
                entity_type=EntityType.PRODUCT,
                entity_key=item.official_product_id,
                request_url=result.request_url,
                final_url=result.final_url,
                fetch_method=result.fetch_method,
                http_status=result.status_code,
                fetch_status=OperationStatus.SUCCEEDED,
                parse_status=OperationStatus.SUCCEEDED,
                error_code="LARGE_PRICE_CHANGE_RECHECK",
                error_message="; ".join(f"{issue.code}@{issue.path}" for issue in warnings),
                raw_hash=artifact.source_hash,
                raw_path=artifact.relative_path,
                duration_ms=result.duration_ms,
                fetched_at=result.fetched_at,
            )

    def _record_failure(
        self,
        run_id: int,
        item: DiscoveredProduct,
        *,
        result: FetchResult | None,
        artifact: ArtifactReference | None,
        error_code: str,
        error_message: str,
        fetch_failed: bool,
    ) -> None:
        with self.session_factory.begin() as session:
            CrawlRecordRepository(session).add(
                crawl_run_id=run_id,
                entity_type=EntityType.PRODUCT,
                entity_key=item.official_product_id,
                request_url=result.request_url if result else item.url,
                final_url=result.final_url if result else item.url,
                fetch_method=result.fetch_method if result else FetchMethod.HTTP,
                http_status=result.status_code if result else None,
                fetch_status=(
                    OperationStatus.FAILED if fetch_failed else OperationStatus.SUCCEEDED
                ),
                parse_status=(OperationStatus.SKIPPED if fetch_failed else OperationStatus.FAILED),
                error_code=error_code,
                error_message=error_message,
                raw_hash=artifact.source_hash if artifact else None,
                raw_path=artifact.relative_path if artifact else None,
                duration_ms=result.duration_ms if result else 0,
                fetched_at=result.fetched_at if result else utc_now_naive(),
            )

    def _record_duplicate(self, run_id: int, item: DiscoveredProduct) -> None:
        with self.session_factory.begin() as session:
            CrawlRecordRepository(session).add(
                crawl_run_id=run_id,
                entity_type=EntityType.PRODUCT,
                entity_key=item.official_product_id,
                request_url=item.url,
                final_url=item.url,
                fetch_method=FetchMethod.REPLAY,
                http_status=None,
                fetch_status=OperationStatus.SKIPPED,
                parse_status=OperationStatus.SKIPPED,
                error_code="DUPLICATE_DISCOVERY",
                error_message="duplicate product id in one discovery run",
                duration_ms=0,
                fetched_at=utc_now_naive(),
            )

    def _load_channel(self, channel_code: str) -> ChannelRuntime:
        with self.session_factory() as session:
            channel = CatalogRepository(session).require_channel(channel_code)
            return ChannelRuntime(channel.id, list(channel.allowed_domains))

    def _active_product_count(self, channel_id: int) -> int:
        with self.session_factory() as session:
            return CatalogRepository(session).active_product_count(channel_id)

    def _load_previous_prices(
        self,
        adapter: BrandAdapter,
        item: DiscoveredProduct,
    ) -> dict[str, Decimal]:
        with self.session_factory() as session:
            return CatalogRepository(session).current_prices_for_product(
                brand_code=adapter.brand_code,
                channel_code=adapter.channel_code,
                official_product_id=item.official_product_id,
            )

    def _register_missing_products(
        self,
        channel_id: int,
        seen_product_ids: set[str],
    ) -> list[MissingProductCandidate]:
        with self.session_factory.begin() as session:
            return CatalogRepository(session).register_missing_products(
                channel_id=channel_id,
                seen_product_ids=seen_product_ids,
                confirmation_runs=self.missing_confirmation_runs,
            )

    def _start_run(
        self,
        adapter: BrandAdapter,
        channel_id: int,
        trigger_type: TriggerType,
    ) -> int:
        with self.session_factory.begin() as session:
            repository = CrawlRunRepository(session)
            now = utc_now_naive()
            repository.fail_stale_runs(
                channel_id=channel_id,
                stale_before=now - timedelta(minutes=self.stale_run_after_minutes),
                finished_at=now,
            )
            run = repository.start(
                channel_id=channel_id,
                run_type=RunType.FULL,
                trigger_type=trigger_type,
                adapter_version=adapter.version,
                started_at=now,
            )
            return run.id

    def _finish_run(
        self,
        run_id: int,
        *,
        status: RunStatus,
        discovered: int,
        success: int,
        skipped: int,
        failed: int,
        error_summary: dict[str, int] | None,
    ) -> None:
        with self.session_factory.begin() as session:
            run = session.scalar(select(CrawlRun).where(CrawlRun.id == run_id).with_for_update())
            if run is None:
                self._missing_run(run_id)
            CrawlRunRepository(session).finish(
                run,
                status=status,
                discovered_count=discovered,
                success_count=success,
                skipped_count=skipped,
                failed_count=failed,
                error_summary=error_summary,
            )

    @staticmethod
    def _terminal_status(*, success: int, failed: int) -> RunStatus:
        if failed == 0:
            return RunStatus.SUCCEEDED
        if success == 0:
            return RunStatus.FAILED
        return RunStatus.PARTIAL

    def _discovery_is_trusted(
        self,
        *,
        discovered_count: int,
        known_active_products: int,
    ) -> bool:
        if known_active_products == 0:
            return True
        return discovered_count >= max(
            1,
            ceil(known_active_products * self.discovery_count_floor_ratio),
        )

    @staticmethod
    def _requires_price_confirmation(report: ValidationReport) -> bool:
        return any(issue.code == "LARGE_PRICE_CHANGE" for issue in report.issues)

    @staticmethod
    def _price_signature(product: NormalizedProduct) -> tuple[tuple[object, ...], ...]:
        return tuple(
            sorted(
                (
                    sku.spec_fingerprint,
                    offer.official_offer_id or "",
                    offer.currency,
                    offer.original_price,
                    offer.original_price_type.value,
                    offer.current_price,
                    offer.availability.value,
                )
                for sku in product.skus
                for offer in sku.offers
            )
        )

    @staticmethod
    def _missing_run(run_id: int) -> NoReturn:
        raise RuntimeError(f"crawl run disappeared during execution: {run_id}")
