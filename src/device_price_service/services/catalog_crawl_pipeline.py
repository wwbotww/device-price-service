from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from datetime import datetime
from hashlib import sha256

import structlog
from sqlalchemy import Engine, select
from sqlalchemy.orm import Session, sessionmaker

from device_price_service.crawlers.base import AdapterContext, BrowserSnapshotFetcher, Fetcher
from device_price_service.crawlers.catalog import (
    CatalogConnector,
    CatalogDatasetConnector,
    CatalogSourceConnector,
)
from device_price_service.db.catalog_models import (
    ListingRevision,
    SourceChannel,
    SourceListing,
    TaxonomyCategory,
)
from device_price_service.db.catalog_repositories import (
    CatalogCrawlRepository,
    CatalogPriceRepository,
    GeneralCatalogRepository,
)
from device_price_service.domain.catalog_crawl import (
    CatalogCollectionRequest,
    DiscoveredCatalogDataset,
    DiscoveredCatalogListing,
    EvaluatedPriceCandidate,
    NormalizedListingIdentity,
    ParsedCatalogDatasetRow,
    ParsedCatalogListing,
)
from device_price_service.domain.catalog_enums import (
    CatalogEntityType,
    CollectionFetchMethod,
    CollectionTriggerType,
    LifecycleStatus,
    OperationStatus,
    QualityStatus,
    RunStatus,
    RunType,
)
from device_price_service.domain.catalog_models import CatalogPriceObservation
from device_price_service.domain.crawl import ArtifactReference, FetchResult
from device_price_service.domain.models import utc_now_naive
from device_price_service.normalization.catalog_rules import CategoryRule, CategoryRuleRegistry
from device_price_service.services.artifact_store import RawArtifactStore
from device_price_service.services.locks import mysql_named_lock
from device_price_service.validation.catalog_price_policy import CatalogPricePolicy


@dataclass(frozen=True, slots=True)
class CatalogCollectionOutcome:
    crawl_run_id: int
    discovered_count: int
    fetched_count: int
    accepted_count: int
    review_count: int
    rejected_count: int
    failed_count: int


@dataclass(frozen=True, slots=True)
class _ChannelRuntime:
    id: int
    allowed_domains: list[str]


@dataclass(frozen=True, slots=True)
class _ProcessResult:
    fetched: int = 0
    accepted: int = 0
    review: int = 0
    rejected: int = 0
    failed: int = 0
    error_code: str | None = None


@dataclass(frozen=True, slots=True)
class _PreparedDatasetRow:
    row: ParsedCatalogDatasetRow
    identity: NormalizedListingIdentity
    evaluated: list[EvaluatedPriceCandidate]
    normalizer_version: str


class CatalogCrawlPipeline:
    """Brand-independent collection flow for V2 source listings and point prices."""

    def __init__(
        self,
        *,
        engine: Engine,
        session_factory: sessionmaker[Session],
        http_fetcher: Fetcher,
        browser_fetcher: BrowserSnapshotFetcher,
        artifact_store: RawArtifactStore,
        category_rules: CategoryRuleRegistry,
        price_policy: CatalogPricePolicy | None = None,
    ) -> None:
        self.engine = engine
        self.session_factory = session_factory
        self.http_fetcher = http_fetcher
        self.browser_fetcher = browser_fetcher
        self.artifact_store = artifact_store
        self.category_rules = category_rules
        self.price_policy = price_policy or CatalogPricePolicy()
        self.logger = structlog.get_logger()

    async def run(
        self,
        connector: CatalogConnector,
        request: CatalogCollectionRequest,
        *,
        trigger_type: CollectionTriggerType = CollectionTriggerType.MANUAL,
    ) -> CatalogCollectionOutcome:
        lock_name = (
            f"device-price:catalog:{connector.channel_code}:"
            f"{request.region_scope.value}:{request.region_code}"
        )
        with mysql_named_lock(self.engine, lock_name):
            channel = self._load_channel(connector)
            context = AdapterContext(
                http=self.http_fetcher,
                browser=self.browser_fetcher,
                allowed_domains=channel.allowed_domains,
            )
            run_id = self._start_run(connector, request, trigger_type, channel.id)
            try:
                discovered = await connector.discover(context, request)
            except Exception as error:
                self._finish_run(
                    run_id,
                    status=RunStatus.FAILED,
                    discovered=0,
                    fetched=0,
                    accepted=0,
                    review=0,
                    rejected=0,
                    failed=1,
                    errors={"DISCOVERY_FAILED": 1},
                )
                self.logger.exception(
                    "catalog_discovery_failed",
                    crawl_run_id=run_id,
                    channel_code=connector.channel_code,
                    error_type=type(error).__name__,
                )
                raise

            totals = Counter[str]()
            errors = Counter[str]()
            seen: set[tuple[str, str, str]] = set()
            for item in discovered:
                if item.discovery_key in seen:
                    self._record_skipped(
                        run_id=run_id,
                        channel=channel,
                        connector=connector,
                        item=item,
                        error_code="DUPLICATE_DISCOVERY",
                        error_message="duplicate source listing identity in one discovery run",
                    )
                    totals["rejected"] += 1
                    errors["DUPLICATE_DISCOVERY"] += 1
                    continue
                seen.add(item.discovery_key)
                if request.category_codes and item.category_code not in request.category_codes:
                    self._record_skipped(
                        run_id=run_id,
                        channel=channel,
                        connector=connector,
                        item=item,
                        error_code="CATEGORY_OUT_OF_SCOPE",
                        error_message="connector returned a category outside the requested scope",
                    )
                    totals["rejected"] += 1
                    errors["CATEGORY_OUT_OF_SCOPE"] += 1
                    continue

                result = await self._process_item(
                    run_id=run_id,
                    channel=channel,
                    connector=connector,
                    context=context,
                    request=request,
                    item=item,
                )
                totals["fetched"] += result.fetched
                totals["accepted"] += result.accepted
                totals["review"] += result.review
                totals["rejected"] += result.rejected
                totals["failed"] += result.failed
                if result.error_code is not None:
                    errors[result.error_code] += 1

            failed = totals["failed"]
            processed = len(seen)
            if failed == 0:
                status = RunStatus.SUCCEEDED
            elif processed == failed:
                status = RunStatus.FAILED
            else:
                status = RunStatus.PARTIAL
            self._finish_run(
                run_id,
                status=status,
                discovered=len(discovered),
                fetched=totals["fetched"],
                accepted=totals["accepted"],
                review=totals["review"],
                rejected=totals["rejected"],
                failed=failed,
                errors=dict(errors) or None,
            )
            return CatalogCollectionOutcome(
                crawl_run_id=run_id,
                discovered_count=len(discovered),
                fetched_count=totals["fetched"],
                accepted_count=totals["accepted"],
                review_count=totals["review"],
                rejected_count=totals["rejected"],
                failed_count=failed,
            )

    async def run_dataset(
        self,
        connector: CatalogDatasetConnector,
        request: CatalogCollectionRequest,
        *,
        trigger_type: CollectionTriggerType = CollectionTriggerType.MANUAL,
    ) -> CatalogCollectionOutcome:
        """Fetch one shared public document and persist all validated rows atomically."""

        lock_name = (
            f"device-price:catalog:{connector.channel_code}:"
            f"{request.region_scope.value}:{request.region_code}"
        )
        with mysql_named_lock(self.engine, lock_name):
            channel = self._load_channel(connector)
            context = AdapterContext(
                http=self.http_fetcher,
                browser=self.browser_fetcher,
                allowed_domains=channel.allowed_domains,
            )
            run_id = self._start_run(connector, request, trigger_type, channel.id)
            try:
                dataset = await connector.discover_dataset(context, request)
            except Exception as error:
                self._finish_run(
                    run_id,
                    status=RunStatus.FAILED,
                    discovered=0,
                    fetched=0,
                    accepted=0,
                    review=0,
                    rejected=0,
                    failed=1,
                    errors={"DISCOVERY_FAILED": 1},
                )
                self.logger.exception(
                    "catalog_dataset_discovery_failed",
                    crawl_run_id=run_id,
                    channel_code=connector.channel_code,
                    error_type=type(error).__name__,
                )
                raise

            result: FetchResult | None = None
            artifact: ArtifactReference | None = None
            discovered_count = 0
            try:
                result = await connector.fetch_dataset(context, dataset)
                artifact = self.artifact_store.save(
                    source_code=connector.channel_code,
                    crawl_run_id=run_id,
                    result=result,
                )
                if not 200 <= result.status_code < 400:
                    raise RuntimeError(f"unexpected HTTP status {result.status_code}")
                parsed_dataset = connector.parse_dataset(dataset, result)
                discovered_count = len(parsed_dataset.rows)
                prepared = self._prepare_dataset_rows(parsed_dataset.rows, request=request)
                accepted, review, rejected = self._persist_dataset_success(
                    run_id=run_id,
                    channel=channel,
                    connector=connector,
                    dataset=dataset,
                    result=result,
                    artifact=artifact,
                    prepared=prepared,
                )
            except Exception as error:
                error_code = self._exception_code(error)
                self.logger.error(
                    "catalog_dataset_failed",
                    crawl_run_id=run_id,
                    channel_code=connector.channel_code,
                    dataset_key=dataset.dataset_key,
                    error_type=type(error).__name__,
                    error_message=str(error)[:1000],
                )
                self._record_dataset_failure(
                    run_id=run_id,
                    connector=connector,
                    dataset=dataset,
                    result=result,
                    artifact=artifact,
                    error_code=error_code,
                    error_message=str(error)[:2000],
                )
                self._finish_run(
                    run_id,
                    status=RunStatus.FAILED,
                    discovered=discovered_count,
                    fetched=1 if result is not None else 0,
                    accepted=0,
                    review=0,
                    rejected=0,
                    failed=1,
                    errors={error_code: 1},
                )
                return CatalogCollectionOutcome(
                    crawl_run_id=run_id,
                    discovered_count=discovered_count,
                    fetched_count=1 if result is not None else 0,
                    accepted_count=0,
                    review_count=0,
                    rejected_count=0,
                    failed_count=1,
                )

            self._finish_run(
                run_id,
                status=RunStatus.SUCCEEDED,
                discovered=discovered_count,
                fetched=1,
                accepted=accepted,
                review=review,
                rejected=rejected,
                failed=0,
                errors=None,
            )
            return CatalogCollectionOutcome(
                crawl_run_id=run_id,
                discovered_count=discovered_count,
                fetched_count=1,
                accepted_count=accepted,
                review_count=review,
                rejected_count=rejected,
                failed_count=0,
            )

    def _prepare_dataset_rows(
        self,
        rows: list[ParsedCatalogDatasetRow],
        *,
        request: CatalogCollectionRequest,
    ) -> list[_PreparedDatasetRow]:
        prepared: list[_PreparedDatasetRow] = []
        seen: set[tuple[str, str, str]] = set()
        for row in rows:
            item = row.item
            if item.discovery_key in seen:
                raise ValueError("duplicate source listing identity in one public dataset")
            seen.add(item.discovery_key)
            if request.category_codes and item.category_code not in request.category_codes:
                raise ValueError("public dataset returned a category outside the requested scope")
            rule = self._load_category_rule(item.category_code)
            identity = rule.normalize(item, row.parsed)
            evaluated = [
                self.price_policy.evaluate(
                    candidate,
                    price_nature=item.price_nature,
                    default_region=request.default_region,
                    identity=identity,
                )
                for candidate in row.parsed.price_candidates
            ]
            prepared.append(
                _PreparedDatasetRow(
                    row=row,
                    identity=identity,
                    evaluated=evaluated,
                    normalizer_version=f"{rule.profile_code}@{rule.version}",
                )
            )
        return prepared

    def _persist_dataset_success(
        self,
        *,
        run_id: int,
        channel: _ChannelRuntime,
        connector: CatalogDatasetConnector,
        dataset: DiscoveredCatalogDataset,
        result: FetchResult,
        artifact: ArtifactReference,
        prepared: list[_PreparedDatasetRow],
    ) -> tuple[int, int, int]:
        quality_counts: Counter[QualityStatus] = Counter()
        rejection_codes: list[str] = []
        for prepared_item in prepared:
            if prepared_item.identity.quality_status is QualityStatus.ACCEPTED:
                quality_counts.update(
                    candidate.quality_status for candidate in prepared_item.evaluated
                )
                rejection_codes.extend(
                    candidate.rejection_code
                    for candidate in prepared_item.evaluated
                    if candidate.rejection_code is not None
                )
            else:
                quality_counts[prepared_item.identity.quality_status] += 1
                if prepared_item.identity.rejection_code is not None:
                    rejection_codes.append(prepared_item.identity.rejection_code)

        validation_status = self._validation_status(quality_counts)
        error_code = None if quality_counts[QualityStatus.ACCEPTED] else (
            rejection_codes[0] if rejection_codes else "NO_ACCEPTED_PRICE"
        )
        artifact_manifest = [
            {
                "role": "source_page",
                "url": dataset.source_page_url,
                "source_date": dataset.metadata.get("source_date"),
                "source_hash": dataset.metadata.get("article_hash"),
            },
            {
                "role": "index",
                "source_hash": dataset.metadata.get("index_hash"),
            },
        ]

        with self.session_factory.begin() as session:
            crawl = CatalogCrawlRepository(session)
            record = crawl.add_record(
                crawl_run_id=run_id,
                source_listing_id=None,
                entity_type=CatalogEntityType.PUBLIC_PRICE,
                entity_key=self._dataset_entity_key(dataset),
                request_url=result.request_url,
                final_url=result.final_url,
                fetch_method=self._fetch_method(result),
                http_status=result.status_code,
                fetch_status=OperationStatus.SUCCEEDED,
                parse_status=OperationStatus.SUCCEEDED,
                validation_status=validation_status,
                error_code=error_code,
                error_message=None,
                raw_hash=artifact.source_hash,
                raw_path=artifact.relative_path,
                artifact_manifest=artifact_manifest,
                content_type=result.content_type or None,
                raw_size_bytes=artifact.size_bytes,
                duration_ms=result.duration_ms,
                fetched_at=result.fetched_at,
            )
            catalog = GeneralCatalogRepository(session)
            prices = CatalogPriceRepository(session)
            for prepared_row in prepared:
                row = prepared_row.row
                item = row.item
                identity = prepared_row.identity
                revision_observed_at = max(
                    (
                        candidate.source_observed_at or result.fetched_at
                        for candidate in prepared_row.evaluated
                    ),
                    default=dataset.source_observed_at,
                )
                listing = self._ensure_listing(
                    session,
                    channel_id=channel.id,
                    item=item,
                    canonical_url=item.url,
                    observed_at=result.fetched_at,
                )
                revision = catalog.get_or_create_listing_revision(
                    source_listing_id=listing.id,
                    first_crawl_record_id=record.id,
                    source_title=row.parsed.source_title,
                    source_category_path=row.parsed.source_category_path,
                    source_attributes=row.parsed.source_attributes,
                    normalized_attributes=identity.normalized_attributes,
                    condition_code=identity.condition_code,
                    measure_type=identity.measure_type,
                    quantity_value=identity.quantity_value,
                    quantity_min=identity.quantity_min,
                    quantity_max=identity.quantity_max,
                    quantity_unit=identity.quantity_unit,
                    base_quantity_value=identity.base_quantity_value,
                    base_quantity_min=identity.base_quantity_min,
                    base_quantity_max=identity.base_quantity_max,
                    base_unit=identity.base_unit,
                    package_count=identity.package_count,
                    identity_fingerprint=identity.build_fingerprint(),
                    normalizer_version=prepared_row.normalizer_version,
                    quality_status=identity.quality_status,
                    rejection_code=identity.rejection_code,
                    observed_at=revision_observed_at,
                )
                if identity.quality_status is not QualityStatus.ACCEPTED:
                    continue
                if self._may_advance_revision(session, listing=listing, revision=revision):
                    catalog.set_current_revision(
                        source_listing_id=listing.id,
                        revision_id=revision.id,
                    )
                for candidate in prepared_row.evaluated:
                    observation = CatalogPriceObservation(
                        source_listing_id=listing.id,
                        listing_revision_id=revision.id,
                        crawl_record_id=record.id,
                        region_scope=candidate.region.scope,
                        region_code=candidate.region.code,
                        original_price=candidate.original_price,
                        original_price_type=candidate.original_price_type,
                        current_price=candidate.current_price,
                        price_nature=candidate.price_nature,
                        price_type=candidate.price_type,
                        pricing_basis=candidate.pricing_basis,
                        promotion_label=candidate.promotion_label,
                        availability=candidate.availability,
                        unit_price=candidate.unit_price,
                        unit_price_unit=candidate.unit_price_unit,
                        fee_status=candidate.fee_status,
                        quality_status=candidate.quality_status,
                        rejection_code=candidate.rejection_code,
                        displayed_price_text=candidate.displayed_price_text,
                        source_hash=candidate.evidence_hash or artifact.source_hash,
                        observed_at=candidate.source_observed_at or result.fetched_at,
                    )
                    if observation.eligible_for_current:
                        observation = prices.resolve_same_time_correction(observation)
                    prices.record(observation)

        return (
            quality_counts[QualityStatus.ACCEPTED],
            quality_counts[QualityStatus.REVIEW_REQUIRED],
            quality_counts[QualityStatus.REJECTED],
        )

    def _record_dataset_failure(
        self,
        *,
        run_id: int,
        connector: CatalogDatasetConnector,
        dataset: DiscoveredCatalogDataset,
        result: FetchResult | None,
        artifact: ArtifactReference | None,
        error_code: str,
        error_message: str,
    ) -> None:
        fetched_at = result.fetched_at if result is not None else utc_now_naive()
        with self.session_factory.begin() as session:
            CatalogCrawlRepository(session).add_record(
                crawl_run_id=run_id,
                source_listing_id=None,
                entity_type=CatalogEntityType.PUBLIC_PRICE,
                entity_key=self._dataset_entity_key(dataset),
                request_url=result.request_url if result is not None else dataset.url,
                final_url=result.final_url if result is not None else dataset.url,
                fetch_method=self._fetch_method(result) if result else connector.fetch_method,
                http_status=result.status_code if result is not None else None,
                fetch_status=(
                    OperationStatus.SUCCEEDED
                    if result is not None
                    else OperationStatus.FAILED
                ),
                parse_status=(
                    OperationStatus.FAILED
                    if result is not None
                    else OperationStatus.SKIPPED
                ),
                validation_status=OperationStatus.SKIPPED,
                error_code=error_code,
                error_message=error_message,
                raw_hash=artifact.source_hash if artifact is not None else None,
                raw_path=artifact.relative_path if artifact is not None else None,
                artifact_manifest=[
                    {
                        "role": "source_page",
                        "url": dataset.source_page_url,
                        "source_date": dataset.metadata.get("source_date"),
                        "source_hash": dataset.metadata.get("article_hash"),
                    }
                ],
                content_type=(result.content_type or None) if result is not None else None,
                raw_size_bytes=artifact.size_bytes if artifact is not None else None,
                duration_ms=result.duration_ms if result is not None else 0,
                fetched_at=fetched_at,
            )

    async def _process_item(
        self,
        *,
        run_id: int,
        channel: _ChannelRuntime,
        connector: CatalogConnector,
        context: AdapterContext,
        request: CatalogCollectionRequest,
        item: DiscoveredCatalogListing,
    ) -> _ProcessResult:
        result: FetchResult | None = None
        artifact: ArtifactReference | None = None
        try:
            rule = self._load_category_rule(item.category_code)
            result = await connector.fetch_listing(context, item)
            artifact = self.artifact_store.save(
                source_code=connector.channel_code,
                crawl_run_id=run_id,
                result=result,
            )
            if not 200 <= result.status_code < 400:
                self._record_failure(
                    run_id=run_id,
                    channel=channel,
                    connector=connector,
                    item=item,
                    result=result,
                    artifact=artifact,
                    error_code="HTTP_STATUS_ERROR",
                    error_message=f"unexpected HTTP status {result.status_code}",
                    fetch_failed=False,
                )
                return _ProcessResult(fetched=1, failed=1, error_code="HTTP_STATUS_ERROR")

            parsed = connector.parse_listing(item, result)
            identity = rule.normalize(item, parsed)
            evaluated = [
                self.price_policy.evaluate(
                    candidate,
                    price_nature=item.price_nature,
                    default_region=request.default_region,
                    identity=identity,
                )
                for candidate in parsed.price_candidates
            ]
            accepted, review, rejected = self._persist_success(
                run_id=run_id,
                channel=channel,
                connector=connector,
                item=item,
                result=result,
                artifact=artifact,
                parsed=parsed,
                identity=identity,
                evaluated=evaluated,
                normalizer_version=f"{rule.profile_code}@{rule.version}",
            )
            return _ProcessResult(
                fetched=1,
                accepted=accepted,
                review=review,
                rejected=rejected,
            )
        except Exception as error:
            error_code = type(error).__name__.upper()[:64]
            self.logger.error(
                "catalog_listing_failed",
                crawl_run_id=run_id,
                channel_code=connector.channel_code,
                entity_key=self._entity_key(item),
                error_type=type(error).__name__,
                error_message=str(error)[:1000],
            )
            self._record_failure(
                run_id=run_id,
                channel=channel,
                connector=connector,
                item=item,
                result=result,
                artifact=artifact,
                error_code=error_code,
                error_message=str(error)[:2000],
                fetch_failed=result is None,
            )
            return _ProcessResult(
                fetched=1 if result is not None else 0,
                failed=1,
                error_code=error_code,
            )

    def _persist_success(
        self,
        *,
        run_id: int,
        channel: _ChannelRuntime,
        connector: CatalogConnector,
        item: DiscoveredCatalogListing,
        result: FetchResult,
        artifact: ArtifactReference,
        parsed: ParsedCatalogListing,
        identity: NormalizedListingIdentity,
        evaluated: list[EvaluatedPriceCandidate],
        normalizer_version: str,
    ) -> tuple[int, int, int]:
        if len(normalizer_version) > 64:
            raise ValueError("normalizer version cannot exceed 64 characters")
        revision_observed_at = max(
            (
                candidate.source_observed_at or result.fetched_at
                for candidate in evaluated
            ),
            default=result.fetched_at,
        )
        quality_counts = Counter(candidate.quality_status for candidate in evaluated)
        if identity.quality_status is QualityStatus.ACCEPTED:
            validation_status = self._validation_status(quality_counts)
            error_code = self._price_error_code(quality_counts, evaluated)
        elif identity.quality_status is QualityStatus.REVIEW_REQUIRED:
            validation_status = OperationStatus.PENDING
            error_code = identity.rejection_code
        else:
            validation_status = OperationStatus.FAILED
            error_code = identity.rejection_code

        with self.session_factory.begin() as session:
            listing = self._ensure_listing(
                session,
                channel_id=channel.id,
                item=item,
                canonical_url=result.final_url,
                observed_at=result.fetched_at,
            )
            crawl = CatalogCrawlRepository(session)
            record = crawl.add_record(
                crawl_run_id=run_id,
                source_listing_id=listing.id,
                entity_type=CatalogEntityType.LISTING,
                entity_key=self._entity_key(item),
                request_url=result.request_url,
                final_url=result.final_url,
                fetch_method=self._fetch_method(result),
                http_status=result.status_code,
                fetch_status=OperationStatus.SUCCEEDED,
                parse_status=OperationStatus.SUCCEEDED,
                validation_status=validation_status,
                error_code=error_code,
                error_message=None,
                raw_hash=artifact.source_hash,
                raw_path=artifact.relative_path,
                content_type=result.content_type or None,
                raw_size_bytes=artifact.size_bytes,
                duration_ms=result.duration_ms,
                fetched_at=result.fetched_at,
            )
            catalog = GeneralCatalogRepository(session)
            revision = catalog.get_or_create_listing_revision(
                source_listing_id=listing.id,
                first_crawl_record_id=record.id,
                source_title=parsed.source_title,
                source_category_path=parsed.source_category_path,
                source_attributes=parsed.source_attributes,
                normalized_attributes=identity.normalized_attributes,
                condition_code=identity.condition_code,
                measure_type=identity.measure_type,
                quantity_value=identity.quantity_value,
                quantity_min=identity.quantity_min,
                quantity_max=identity.quantity_max,
                quantity_unit=identity.quantity_unit,
                base_quantity_value=identity.base_quantity_value,
                base_quantity_min=identity.base_quantity_min,
                base_quantity_max=identity.base_quantity_max,
                base_unit=identity.base_unit,
                package_count=identity.package_count,
                identity_fingerprint=identity.build_fingerprint(),
                normalizer_version=normalizer_version,
                quality_status=identity.quality_status,
                rejection_code=identity.rejection_code,
                observed_at=revision_observed_at,
            )
            if identity.quality_status is not QualityStatus.ACCEPTED:
                return (
                    0,
                    1 if identity.quality_status is QualityStatus.REVIEW_REQUIRED else 0,
                    1 if identity.quality_status is QualityStatus.REJECTED else 0,
                )

            if self._may_advance_revision(session, listing=listing, revision=revision):
                catalog.set_current_revision(
                    source_listing_id=listing.id,
                    revision_id=revision.id,
                )
            prices = CatalogPriceRepository(session)
            for candidate in evaluated:
                prices.record(
                    CatalogPriceObservation(
                        source_listing_id=listing.id,
                        listing_revision_id=revision.id,
                        crawl_record_id=record.id,
                        region_scope=candidate.region.scope,
                        region_code=candidate.region.code,
                        original_price=candidate.original_price,
                        original_price_type=candidate.original_price_type,
                        current_price=candidate.current_price,
                        price_nature=candidate.price_nature,
                        price_type=candidate.price_type,
                        pricing_basis=candidate.pricing_basis,
                        promotion_label=candidate.promotion_label,
                        availability=candidate.availability,
                        unit_price=candidate.unit_price,
                        unit_price_unit=candidate.unit_price_unit,
                        fee_status=candidate.fee_status,
                        quality_status=candidate.quality_status,
                        rejection_code=candidate.rejection_code,
                        displayed_price_text=candidate.displayed_price_text,
                        source_hash=candidate.evidence_hash or artifact.source_hash,
                        observed_at=candidate.source_observed_at or result.fetched_at,
                    )
                )
        return (
            quality_counts[QualityStatus.ACCEPTED],
            quality_counts[QualityStatus.REVIEW_REQUIRED],
            quality_counts[QualityStatus.REJECTED],
        )

    def _record_failure(
        self,
        *,
        run_id: int,
        channel: _ChannelRuntime,
        connector: CatalogConnector,
        item: DiscoveredCatalogListing,
        result: FetchResult | None,
        artifact: ArtifactReference | None,
        error_code: str,
        error_message: str,
        fetch_failed: bool,
    ) -> None:
        observed_at = result.fetched_at if result is not None else utc_now_naive()
        with self.session_factory.begin() as session:
            listing_id = self._best_effort_listing_id(
                session,
                channel_id=channel.id,
                item=item,
                canonical_url=result.final_url if result is not None else item.url,
                observed_at=observed_at,
            )
            CatalogCrawlRepository(session).add_record(
                crawl_run_id=run_id,
                source_listing_id=listing_id,
                entity_type=CatalogEntityType.LISTING,
                entity_key=self._entity_key(item),
                request_url=result.request_url if result is not None else item.url,
                final_url=result.final_url if result is not None else item.url,
                fetch_method=(self._fetch_method(result) if result else connector.fetch_method),
                http_status=result.status_code if result is not None else None,
                fetch_status=(
                    OperationStatus.FAILED if fetch_failed else OperationStatus.SUCCEEDED
                ),
                parse_status=(OperationStatus.SKIPPED if fetch_failed else OperationStatus.FAILED),
                validation_status=OperationStatus.SKIPPED,
                error_code=error_code,
                error_message=error_message,
                raw_hash=artifact.source_hash if artifact is not None else None,
                raw_path=artifact.relative_path if artifact is not None else None,
                content_type=(result.content_type or None) if result is not None else None,
                raw_size_bytes=artifact.size_bytes if artifact is not None else None,
                duration_ms=result.duration_ms if result is not None else 0,
                fetched_at=observed_at,
            )

    def _record_skipped(
        self,
        *,
        run_id: int,
        channel: _ChannelRuntime,
        connector: CatalogConnector,
        item: DiscoveredCatalogListing,
        error_code: str,
        error_message: str,
    ) -> None:
        observed_at = utc_now_naive()
        with self.session_factory.begin() as session:
            listing_id = self._best_effort_listing_id(
                session,
                channel_id=channel.id,
                item=item,
                canonical_url=item.url,
                observed_at=observed_at,
            )
            CatalogCrawlRepository(session).add_record(
                crawl_run_id=run_id,
                source_listing_id=listing_id,
                entity_type=CatalogEntityType.LISTING,
                entity_key=self._entity_key(item),
                request_url=item.url,
                final_url=item.url,
                fetch_method=connector.fetch_method,
                fetch_status=OperationStatus.SKIPPED,
                parse_status=OperationStatus.SKIPPED,
                validation_status=OperationStatus.FAILED,
                error_code=error_code,
                error_message=error_message,
                duration_ms=0,
                fetched_at=observed_at,
            )

    def _best_effort_listing_id(
        self,
        session: Session,
        *,
        channel_id: int,
        item: DiscoveredCatalogListing,
        canonical_url: str,
        observed_at: datetime,
    ) -> int | None:
        try:
            return self._ensure_listing(
                session,
                channel_id=channel_id,
                item=item,
                canonical_url=canonical_url,
                observed_at=observed_at,
            ).id
        except (ValueError, RuntimeError):
            return None

    @staticmethod
    def _ensure_listing(
        session: Session,
        *,
        channel_id: int,
        item: DiscoveredCatalogListing,
        canonical_url: str,
        observed_at: datetime,
    ) -> SourceListing:
        catalog = GeneralCatalogRepository(session)
        merchant = catalog.get_or_create_merchant(
            source_channel_id=channel_id,
            merchant_key_hash=sha256(item.merchant.merchant_key.encode()).hexdigest(),
            name=item.merchant.name,
            seller_type=item.merchant.seller_type,
            verification_status=item.merchant.verification_status,
            observed_at=observed_at,
            external_merchant_id=item.merchant.external_merchant_id,
        )
        return catalog.get_or_create_source_listing(
            source_channel_id=channel_id,
            merchant_id=merchant.id,
            listing_key=item.listing_key,
            listing_key_hash=sha256(item.listing_key.encode()).hexdigest(),
            price_nature=item.price_nature,
            canonical_url=canonical_url,
            url_hash=sha256(canonical_url.encode()).hexdigest(),
            observed_at=observed_at,
            external_product_id=item.external_product_id,
            external_sku_id=item.external_sku_id,
            lifecycle_status=LifecycleStatus.ACTIVE,
        )

    @staticmethod
    def _may_advance_revision(
        session: Session,
        *,
        listing: SourceListing,
        revision: ListingRevision,
    ) -> bool:
        if listing.current_revision_id in {None, revision.id}:
            return True
        current = session.get(ListingRevision, listing.current_revision_id)
        return current is None or revision.last_observed_at >= current.last_observed_at

    def _load_channel(self, connector: CatalogSourceConnector) -> _ChannelRuntime:
        channel_code = connector.channel_code.strip().upper()
        with self.session_factory() as session:
            channel = session.scalar(
                select(SourceChannel).where(SourceChannel.code == channel_code)
            )
            if channel is None or not channel.enabled:
                raise RuntimeError(f"catalog source channel is not enabled: {channel_code}")
            if channel.connector_code != connector.connector_code:
                raise RuntimeError(
                    f"source channel {channel_code} expects connector "
                    f"{channel.connector_code!r}, got {connector.connector_code!r}"
                )
            return _ChannelRuntime(channel.id, list(channel.allowed_domains))

    def _load_category_rule(self, category_code: str) -> CategoryRule:
        with self.session_factory() as session:
            category = session.scalar(
                select(TaxonomyCategory).where(TaxonomyCategory.code == category_code)
            )
            if category is None or not category.enabled or not category.is_leaf:
                raise RuntimeError(f"catalog category is not an enabled leaf: {category_code}")
            return self.category_rules.get(
                category.attribute_profile_code,
                category.attribute_profile_version,
            )

    def _start_run(
        self,
        connector: CatalogSourceConnector,
        request: CatalogCollectionRequest,
        trigger_type: CollectionTriggerType,
        channel_id: int,
    ) -> int:
        with self.session_factory.begin() as session:
            run = CatalogCrawlRepository(session).start_run(
                source_channel_id=channel_id,
                region_scope=request.region_scope,
                region_code=request.region_code,
                category_scope=list(request.category_codes),
                run_type=RunType.FULL,
                trigger_type=trigger_type,
                adapter_version=connector.version,
                policy_version=self.price_policy.version,
                started_at=utc_now_naive(),
            )
            return run.id

    def _finish_run(
        self,
        run_id: int,
        *,
        status: RunStatus,
        discovered: int,
        fetched: int,
        accepted: int,
        review: int,
        rejected: int,
        failed: int,
        errors: dict[str, int] | None,
    ) -> None:
        with self.session_factory.begin() as session:
            CatalogCrawlRepository(session).finish_run(
                crawl_run_id=run_id,
                status=status,
                finished_at=utc_now_naive(),
                discovered_count=discovered,
                fetched_count=fetched,
                accepted_count=accepted,
                review_count=review,
                rejected_count=rejected,
                failed_count=failed,
                error_summary=errors,
            )

    @staticmethod
    def _fetch_method(result: FetchResult) -> CollectionFetchMethod:
        return CollectionFetchMethod(result.fetch_method.value)

    @staticmethod
    def _entity_key(item: DiscoveredCatalogListing) -> str:
        return f"LISTING:{sha256(item.listing_key.encode()).hexdigest()}"

    @staticmethod
    def _dataset_entity_key(dataset: DiscoveredCatalogDataset) -> str:
        return f"PUBLIC_PRICE:{sha256(dataset.dataset_key.encode()).hexdigest()}"

    @staticmethod
    def _exception_code(error: Exception) -> str:
        declared = getattr(error, "error_code", None)
        if isinstance(declared, str) and declared.strip():
            return declared.strip().upper()[:64]
        return type(error).__name__.upper()[:64]

    @staticmethod
    def _validation_status(
        counts: Counter[QualityStatus],
    ) -> OperationStatus:
        if counts[QualityStatus.ACCEPTED] > 0:
            return OperationStatus.SUCCEEDED
        if counts[QualityStatus.REVIEW_REQUIRED] > 0:
            return OperationStatus.PENDING
        return OperationStatus.FAILED

    @staticmethod
    def _price_error_code(
        counts: Counter[QualityStatus],
        evaluated: list[EvaluatedPriceCandidate],
    ) -> str | None:
        if counts[QualityStatus.ACCEPTED] > 0:
            return None
        return next(
            (candidate.rejection_code for candidate in evaluated if candidate.rejection_code),
            "NO_ACCEPTED_PRICE",
        )
