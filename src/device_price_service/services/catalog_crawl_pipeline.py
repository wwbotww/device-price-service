from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from hashlib import sha256
from math import ceil
from typing import Any

import structlog
from sqlalchemy import Engine, select
from sqlalchemy.orm import Session, sessionmaker

from device_price_service.crawlers.base import AdapterContext, BrowserSnapshotFetcher, Fetcher
from device_price_service.crawlers.catalog import CatalogConnector, CatalogDatasetConnector
from device_price_service.db.catalog_models import (
    CatalogBrand,
    CatalogCrawlRun,
    CatalogItem,
    ListingMatch,
    ListingRevision,
    SourceChannel,
    SourceListing,
    TaxonomyCategory,
)
from device_price_service.db.catalog_repositories import (
    CatalogCrawlRepository,
    CatalogIdentityConflictError,
    CatalogPriceRepository,
    GeneralCatalogRepository,
)
from device_price_service.domain.catalog_crawl import (
    CatalogCollectionRequest,
    DiscoveredCatalogDataset,
    DiscoveredCatalogProduct,
    ParsedCatalogProduct,
)
from device_price_service.domain.catalog_enums import (
    Availability,
    BusinessMode,
    CatalogEntityType,
    CollectionFetchMethod,
    CollectionTriggerType,
    FeeStatus,
    ItemType,
    LifecycleStatus,
    MatchMethod,
    MatchStatus,
    OperationStatus,
    PriceNature,
    PriceType,
    PricingBasis,
    QualityStatus,
    RecordOrigin,
    RegionMode,
    RegionScope,
    RunStatus,
    RunType,
    SourceType,
)
from device_price_service.domain.catalog_models import CatalogPriceObservation
from device_price_service.domain.crawl import ArtifactReference, FetchResult
from device_price_service.domain.time import utc_now_naive
from device_price_service.fetchers.url_policy import UrlPolicy
from device_price_service.normalization.catalog_rules import CategoryRule, CategoryRuleRegistry
from device_price_service.normalization.devices import device_item_key
from device_price_service.services.artifact_store import RawArtifactStore
from device_price_service.services.catalog_device_guards import (
    CatalogDeviceGuards,
    KnownDeviceProduct,
    price_signature,
    require_product_review,
)
from device_price_service.services.catalog_preparation import (
    CatalogPreparationError,
    PreparedCatalogRow,
    PreparedCatalogRows,
    prepare_catalog_product,
    prepare_catalog_rows,
)
from device_price_service.services.locks import mysql_named_lock
from device_price_service.validation.catalog_price_policy import CatalogPricePolicy

Connector = CatalogConnector | CatalogDatasetConnector
CollectionUnit = DiscoveredCatalogProduct | DiscoveredCatalogDataset


class CatalogConfigurationError(RuntimeError):
    """The stored source configuration has not passed collection prerequisites."""


@dataclass(frozen=True, slots=True)
class CatalogCollectionOutcome:
    crawl_run_id: int
    status: RunStatus
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
    rows: int = 0
    fetched: int = 0
    accepted: int = 0
    review: int = 0
    rejected: int = 0
    failed: int = 0
    complete: bool = False
    error_code: str | None = None
    fresh_observed_at: datetime | None = None


class CatalogCrawlPipeline:
    """Product/document transactions share preparation, evidence and row persistence."""

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
        price_change_confirm_threshold: float = 0.30,
        missing_confirmation_runs: int = 3,
        discovery_count_floor_ratio: float = 0.50,
        stale_run_after_minutes: int = 120,
    ) -> None:
        self.engine = engine
        self.session_factory = session_factory
        self.http_fetcher = http_fetcher
        self.browser_fetcher = browser_fetcher
        self.artifact_store = artifact_store
        self.category_rules = category_rules
        self.price_policy = price_policy or CatalogPricePolicy()
        self.device_guards = CatalogDeviceGuards(session_factory)
        self.price_change_threshold = Decimal(str(price_change_confirm_threshold))
        self.missing_confirmation_runs = missing_confirmation_runs
        self.discovery_count_floor_ratio = discovery_count_floor_ratio
        self.stale_run_after_minutes = stale_run_after_minutes
        self.logger = structlog.get_logger()

    async def run(
        self,
        connector: CatalogConnector,
        request: CatalogCollectionRequest,
        *,
        trigger_type: CollectionTriggerType = CollectionTriggerType.MANUAL,
    ) -> CatalogCollectionOutcome:
        return await self._run(connector, request, trigger_type)

    async def run_dataset(
        self,
        connector: CatalogDatasetConnector,
        request: CatalogCollectionRequest,
        *,
        trigger_type: CollectionTriggerType = CollectionTriggerType.MANUAL,
    ) -> CatalogCollectionOutcome:
        return await self._run(connector, request, trigger_type)

    async def _run(
        self,
        connector: Connector,
        request: CatalogCollectionRequest,
        trigger_type: CollectionTriggerType,
    ) -> CatalogCollectionOutcome:
        lock_name = (
            f"device-price:catalog:{connector.channel_code}:"
            f"{request.region_scope.value}:{request.region_code}"
        )
        with mysql_named_lock(self.engine, lock_name):
            channel = self.validate_source(connector)
            context = AdapterContext(
                http=self.http_fetcher,
                browser=self.browser_fetcher,
                allowed_domains=channel.allowed_domains,
            )
            run_id = self._start_run(connector, request, trigger_type, channel.id)
            totals: Counter[str] = Counter()
            errors: Counter[str] = Counter()
            try:
                known = (
                    self.device_guards.snapshot(channel.id)
                    if isinstance(connector, CatalogConnector)
                    else {}
                )
                full_device_scope = (
                    isinstance(connector, CatalogConnector)
                    and request.region_scope is RegionScope.NATIONAL
                    and not request.source_item_codes
                    and set(request.category_codes or connector.default_category_codes)
                    == set(connector.default_category_codes)
                    and trigger_type is not CollectionTriggerType.QUERY_RECRAWL
                )
                fresh_times: list[datetime] = []
                units: list[CollectionUnit]
                try:
                    if isinstance(connector, CatalogConnector):
                        units = list(await connector.discover_products(context, request))
                    else:
                        units = [await connector.discover_dataset(context, request)]
                except Exception as error:
                    self.logger.error(
                        "catalog_discovery_failed",
                        crawl_run_id=run_id,
                        error_type=type(error).__name__,
                    )
                    totals["failed"] = 1
                    errors["DISCOVERY_FAILED"] = 1
                    return self._finish_run(run_id, totals, errors, RunStatus.FAILED)
                if not units:
                    errors["EMPTY_DISCOVERY"] = 1
                if isinstance(connector, CatalogConnector):
                    totals["discovered"] = len(units)
                if full_device_scope and len(units) < max(
                    1, ceil(len(known) * self.discovery_count_floor_ratio)
                ):
                    errors["DISCOVERY_COUNT_BELOW_FLOOR"] = 1
                    return self._finish_run(run_id, totals, errors, RunStatus.FAILED)
                if len({self._entity_key(unit) for unit in units}) != len(units):
                    for unit in units:
                        self._record_failure(
                            run_id,
                            connector,
                            unit,
                            request,
                            None,
                            None,
                            "DUPLICATE_DISCOVERY",
                            "ambiguous product/document discovery",
                            skipped=True,
                        )
                    totals["rejected"] = len(units)
                    errors["DUPLICATE_DISCOVERY"] = 1
                    return self._finish_run(run_id, totals, errors, RunStatus.FAILED)
                for unit in units:
                    try:
                        UrlPolicy(channel.allowed_domains).validate(unit.url)
                        if (
                            isinstance(unit, DiscoveredCatalogProduct)
                            and request.category_codes
                            and unit.category_code not in request.category_codes
                        ):
                            raise CatalogPreparationError(
                                "CATEGORY_OUT_OF_SCOPE",
                                "product category is out of scope",
                            )
                    except Exception as error:
                        code = self._exception_code(error)
                        self._record_failure(
                            run_id,
                            connector,
                            unit,
                            request,
                            None,
                            None,
                            code,
                            str(error),
                            skipped=True,
                        )
                        totals["rejected"] += 1
                        errors[code] += 1
                        continue
                    outcome = await self._process_unit(
                        run_id,
                        channel,
                        connector,
                        context,
                        request,
                        unit,
                        known=known.get(self._entity_key(unit)),
                    )
                    if isinstance(connector, CatalogDatasetConnector):
                        totals["discovered"] += outcome.rows
                    for name in ("fetched", "accepted", "review", "rejected", "failed"):
                        totals[name] += getattr(outcome, name)
                    if not outcome.complete:
                        errors[outcome.error_code or "NO_ACCEPTED_PRICE"] += 1
                    if outcome.fresh_observed_at is not None:
                        fresh_times.append(outcome.fresh_observed_at)
                # No omissions are inferred from replay, partial/old evidence or a restricted run.
                if (
                    full_device_scope
                    and isinstance(connector, CatalogConnector)
                    and not errors
                    and len(fresh_times) == len(units)
                    and fresh_times
                    and all(min(fresh_times) > item.last_seen_at for item in known.values())
                ):
                    seen = {self._entity_key(unit) for unit in units}
                    missing = self.device_guards.register_missing(
                        [product for key, product in known.items() if key not in seen],
                        observed_at=min(fresh_times),
                        confirmation_runs=self.missing_confirmation_runs,
                    )
                    for product in missing:
                        outcome = await self._confirm_missing_product(
                            run_id, channel, connector, context, request, product
                        )
                        for name in ("fetched", "accepted", "review", "rejected", "failed"):
                            totals[name] += getattr(outcome, name)
                        if not outcome.complete:
                            errors[outcome.error_code or "MISSING_CONFIRMATION_FAILED"] += 1
                status = (
                    RunStatus.FAILED
                    if not totals["accepted"]
                    else RunStatus.PARTIAL
                    if errors
                    else RunStatus.SUCCEEDED
                )
                return self._finish_run(run_id, totals, errors, status)
            except BaseException:
                # Cancellation/unhandled failure must not leave a misleading RUNNING run.
                self._finish_run(run_id, totals, errors, RunStatus.CANCELLED)
                raise

    async def _process_unit(
        self,
        run_id: int,
        channel: _ChannelRuntime,
        connector: Connector,
        context: AdapterContext,
        request: CatalogCollectionRequest,
        unit: CollectionUnit,
        *,
        known: KnownDeviceProduct | None = None,
        prefetched: FetchResult | None = None,
    ) -> _ProcessResult:
        result: FetchResult | None = None
        artifact: ArtifactReference | None = None
        rows = 0
        fetched = 0
        confirmation_manifest: list[dict[str, Any]] = []
        try:
            if prefetched is not None:
                result = prefetched
            elif isinstance(connector, CatalogConnector) and isinstance(
                unit, DiscoveredCatalogProduct
            ):
                result = await connector.fetch_product(context, unit)
            elif isinstance(connector, CatalogDatasetConnector) and isinstance(
                unit, DiscoveredCatalogDataset
            ):
                result = await connector.fetch_dataset(context, unit)
            else:
                raise TypeError("connector returned an incompatible discovery unit")
            fetched += 1
            result = self._point_time_result(result)
            artifact = self.artifact_store.save(
                source_code=connector.channel_code,
                crawl_run_id=run_id,
                result=result,
            )
            urls = UrlPolicy(channel.allowed_domains)
            urls.validate(result.request_url)
            urls.validate(result.final_url)
            if not 200 <= result.status_code < 300:
                raise CatalogPreparationError(
                    "HTTP_STATUS_ERROR",
                    f"unexpected HTTP status {result.status_code}",
                )
            product: ParsedCatalogProduct | None = None
            if isinstance(connector, CatalogConnector) and isinstance(
                unit, DiscoveredCatalogProduct
            ):
                product = connector.parse_product(unit, result)
                rows = len(product.rows)
                prepared = prepare_catalog_product(
                    product,
                    discovered=unit,
                    expected_brand_code=connector.brand_code,
                    request=request,
                    allowed_domains=channel.allowed_domains,
                    rule_for_category=self._load_category_rule,
                    price_policy=self.price_policy,
                )
                needs_confirmation = self.device_guards.price_change_requires_confirmation(
                    channel.id, prepared, result.fetched_at, self.price_change_threshold
                )
                if needs_confirmation and result.fetch_method.value == "REPLAY":
                    prepared = require_product_review(prepared, "PRICE_CHANGE_UNCONFIRMED")
                elif needs_confirmation:
                    first_result, first_prepared = result, prepared
                    confirmation_manifest = self._manifest(
                        connector, request, unit, result, artifact
                    )[1:]
                    for part in confirmation_manifest:
                        part["role"] = "price_change_initial_response"
                    # Keep the first evidence even if the second request fails or is cancelled.
                    self._record_failure(
                        run_id,
                        connector,
                        unit,
                        request,
                        result,
                        artifact,
                        "PRICE_CHANGE_CONFIRMATION_REQUIRED",
                        "large price change requires a second product fetch",
                        pending=True,
                    )
                    result, artifact = None, None
                    result = await connector.fetch_product(context, unit)
                    fetched += 1
                    result = self._point_time_result(result)
                    artifact = self.artifact_store.save(
                        source_code=connector.channel_code, crawl_run_id=run_id, result=result
                    )
                    urls.validate(result.request_url)
                    urls.validate(result.final_url)
                    if not 200 <= result.status_code < 300:
                        raise CatalogPreparationError(
                            "PRICE_CONFIRMATION_HTTP_ERROR",
                            "price confirmation returned a non-success response",
                        )
                    product = connector.parse_product(unit, result)
                    prepared = prepare_catalog_product(
                        product,
                        discovered=unit,
                        expected_brand_code=connector.brand_code,
                        request=request,
                        allowed_domains=channel.allowed_domains,
                        rule_for_category=self._load_category_rule,
                        price_policy=self.price_policy,
                    )
                    if (
                        result.fetched_at <= first_result.fetched_at
                        or not prepared.complete
                        or not first_prepared.complete
                        or price_signature(prepared) != price_signature(first_prepared)
                    ):
                        prepared = require_product_review(prepared, "PRICE_CHANGE_UNCONFIRMED")
                    # First evidence is already audited; only a confirmed second point is trusted.
            elif isinstance(connector, CatalogDatasetConnector) and isinstance(
                unit, DiscoveredCatalogDataset
            ):
                parsed = connector.parse_dataset(unit, result)
                rows = len(parsed.rows)
                prepared = prepare_catalog_rows(
                    parsed.rows,
                    request=request,
                    allowed_domains=channel.allowed_domains,
                    rule_for_category=self._load_category_rule,
                    price_policy=self.price_policy,
                )
            else:
                raise TypeError("incompatible connector result")
            missing_skus = known is not None and bool(
                known.listing_keys - {row.row.item.listing_key for row in prepared.rows}
            )
            self._persist_success(
                run_id,
                channel,
                connector,
                request,
                unit,
                result,
                artifact,
                prepared,
                product,
                extra_manifest=confirmation_manifest,
                missing_skus=missing_skus,
            )
            return _ProcessResult(
                rows=rows,
                fetched=fetched,
                accepted=prepared.accepted_count,
                review=prepared.review_count,
                rejected=prepared.rejected_count,
                complete=prepared.complete and not missing_skus,
                error_code="SKU_COVERAGE_INCOMPLETE" if missing_skus else prepared.error_code,
                fresh_observed_at=result.fetched_at
                if result.fetch_method.value != "REPLAY"
                else None,
            )
        except Exception as error:
            code = self._exception_code(error)
            self.logger.error(
                "catalog_unit_failed",
                crawl_run_id=run_id,
                channel_code=connector.channel_code,
                entity_key=self._entity_key(unit),
                error_type=type(error).__name__,
                error_code=code,
            )
            # Failed transactions never create/update listings as a side effect of auditing.
            self._record_failure(
                run_id,
                connector,
                unit,
                request,
                result,
                artifact,
                code,
                str(error),
                extra_manifest=confirmation_manifest,
            )
            return _ProcessResult(
                rows=rows,
                fetched=fetched,
                failed=1,
                error_code=code,
            )

    async def _confirm_missing_product(
        self,
        run_id: int,
        channel: _ChannelRuntime,
        connector: CatalogConnector,
        context: AdapterContext,
        request: CatalogCollectionRequest,
        known: KnownDeviceProduct,
    ) -> _ProcessResult:
        unit = known.discovery
        if unit is None:
            return _ProcessResult(failed=1, error_code="MISSING_PRODUCT_EVIDENCE_CONTEXT")
        result: FetchResult | None = None
        artifact: ArtifactReference | None = None
        try:
            urls = UrlPolicy(channel.allowed_domains)
            urls.validate(unit.url)
            result = await connector.fetch_product(context, unit)
            result = self._point_time_result(result)
            # Normal details re-enter the same single product validation/persistence path.
            if result.status_code not in {404, 410}:
                return await self._process_unit(
                    run_id,
                    channel,
                    connector,
                    context,
                    request,
                    unit,
                    known=known,
                    prefetched=result,
                )
            artifact = self.artifact_store.save(
                source_code=connector.channel_code, crawl_run_id=run_id, result=result
            )
            urls.validate(result.request_url)
            urls.validate(result.final_url)
            if (
                not connector.product_not_found_is_definitive
                or result.request_url != unit.url
                or result.final_url != unit.url
                or result.fetch_method.value == "REPLAY"
            ):
                raise CatalogPreparationError(
                    "PRODUCT_ABSENCE_UNCONFIRMED",
                    "response does not prove this complete product is unavailable",
                )
            self._persist_product_absence(
                run_id, channel, connector, request, known, unit, result, artifact
            )
            return _ProcessResult(
                fetched=1,
                accepted=len(known.listing_ids),
                complete=True,
                fresh_observed_at=result.fetched_at,
            )
        except Exception as error:
            code = self._exception_code(error)
            self._record_failure(
                run_id, connector, unit, request, result, artifact, code, str(error)
            )
            return _ProcessResult(fetched=int(result is not None), failed=1, error_code=code)

    def _persist_product_absence(
        self,
        run_id: int,
        channel: _ChannelRuntime,
        connector: CatalogConnector,
        request: CatalogCollectionRequest,
        known: KnownDeviceProduct,
        unit: DiscoveredCatalogProduct,
        result: FetchResult,
        artifact: ArtifactReference,
    ) -> None:
        with self.session_factory.begin() as session:
            listings = session.scalars(
                select(SourceListing)
                .where(SourceListing.id.in_(known.listing_ids))
                .with_for_update()
            ).all()
            if len(listings) != len(known.listing_ids) or any(
                listing.source_channel_id != channel.id
                or listing.external_product_id != known.product_id
                or listing.current_revision_id is None
                or result.fetched_at <= listing.last_seen_at
                for listing in listings
            ):
                raise CatalogPreparationError(
                    "STALE_PRODUCT_ABSENCE", "absence evidence cannot supersede known product state"
                )
            record = CatalogCrawlRepository(session).add_record(
                crawl_run_id=run_id,
                source_listing_id=None,
                entity_type=CatalogEntityType.PRODUCT,
                entity_key=known.product_id,
                request_url=result.request_url,
                final_url=result.final_url,
                fetch_method=CollectionFetchMethod(result.fetch_method.value),
                http_status=result.status_code,
                fetch_status=OperationStatus.SUCCEEDED,
                parse_status=OperationStatus.SKIPPED,
                validation_status=OperationStatus.SUCCEEDED,
                error_code="OFF_SHELF_CONFIRMED",
                raw_hash=artifact.source_hash,
                raw_path=artifact.relative_path,
                artifact_manifest=self._manifest(connector, request, unit, result, artifact)
                + [
                    {
                        "role": "confirmed_product_absence",
                        "listing_revision_ids": {
                            str(listing.id): listing.current_revision_id for listing in listings
                        },
                    }
                ],
                content_type=result.content_type or None,
                raw_size_bytes=artifact.size_bytes,
                duration_ms=result.duration_ms,
                fetched_at=result.fetched_at,
            )
            prices = CatalogPriceRepository(session)
            for listing in listings:
                assert listing.current_revision_id is not None
                # Reuse only the established identity, never copy a historical amount.
                observation = CatalogPriceObservation(
                    source_listing_id=listing.id,
                    listing_revision_id=listing.current_revision_id,
                    crawl_record_id=record.id,
                    region_scope=request.region_scope,
                    region_code=request.region_code,
                    price_nature=PriceNature.RETAIL_OFFER,
                    price_type=PriceType.AVAILABILITY_ONLY,
                    pricing_basis=PricingBasis.UNKNOWN,
                    fee_status=FeeStatus.NOT_APPLICABLE,
                    availability=Availability.OFF_SHELF,
                    quality_status=QualityStatus.ACCEPTED,
                    source_hash=artifact.source_hash,
                    observed_at=result.fetched_at,
                )
                prices.assert_same_time_consistent(observation)
                prices.record(observation)
                listing.lifecycle_status = LifecycleStatus.INACTIVE.value
                listing.last_seen_at = observation.observed_at

    def _persist_success(
        self,
        run_id: int,
        channel: _ChannelRuntime,
        connector: Connector,
        request: CatalogCollectionRequest,
        unit: CollectionUnit,
        result: FetchResult,
        artifact: ArtifactReference,
        prepared: PreparedCatalogRows,
        product: ParsedCatalogProduct | None,
        *,
        extra_manifest: list[dict[str, Any]] | None = None,
        missing_skus: bool = False,
    ) -> None:
        with self.session_factory.begin() as session:
            record = CatalogCrawlRepository(session).add_record(
                crawl_run_id=run_id,
                source_listing_id=None,
                entity_type=self._entity_type(unit),
                entity_key=self._entity_key(unit),
                request_url=result.request_url,
                final_url=result.final_url,
                fetch_method=CollectionFetchMethod(result.fetch_method.value),
                http_status=result.status_code,
                fetch_status=OperationStatus.SUCCEEDED,
                parse_status=OperationStatus.SUCCEEDED,
                validation_status=(
                    OperationStatus.SUCCEEDED
                    if prepared.complete and not missing_skus
                    else OperationStatus.PENDING
                    if prepared.review_count or missing_skus
                    else OperationStatus.FAILED
                ),
                error_code="SKU_COVERAGE_INCOMPLETE" if missing_skus else prepared.error_code,
                raw_hash=artifact.source_hash,
                raw_path=artifact.relative_path,
                artifact_manifest=self._manifest(connector, request, unit, result, artifact)
                + (extra_manifest or []),
                content_type=result.content_type or None,
                raw_size_bytes=artifact.size_bytes,
                duration_ms=result.duration_ms,
                fetched_at=result.fetched_at,
            )
            catalog = GeneralCatalogRepository(session)
            standard_item = None
            if product is not None and all(
                row.identity.quality_status is QualityStatus.ACCEPTED for row in prepared.rows
            ):
                standard_item = self._ensure_item(session, connector, product)
            for prepared_row in prepared.rows:
                self._persist_row(
                    session,
                    catalog,
                    channel.id,
                    record.id,
                    result,
                    artifact,
                    prepared_row,
                    standard_item,
                    public_dataset=isinstance(unit, DiscoveredCatalogDataset),
                )

    def _persist_row(
        self,
        session: Session,
        catalog: GeneralCatalogRepository,
        channel_id: int,
        record_id: int,
        result: FetchResult,
        artifact: ArtifactReference,
        prepared: PreparedCatalogRow,
        standard_item: CatalogItem | None,
        *,
        public_dataset: bool,
    ) -> None:
        row, identity = prepared.row, prepared.identity
        item = row.item
        trusted_candidates = [
            candidate
            for candidate in prepared.evaluated
            if candidate.quality_status is QualityStatus.ACCEPTED
            and identity.quality_status is QualityStatus.ACCEPTED
        ]
        trusted = bool(trusted_candidates)
        observed_at = max(
            (candidate.source_observed_at or result.fetched_at for candidate in prepared.evaluated),
            default=result.fetched_at,
        )
        trusted_at = max(
            (candidate.source_observed_at or result.fetched_at for candidate in trusted_candidates),
            default=observed_at,
        )
        merchant = catalog.get_or_create_merchant(
            source_channel_id=channel_id,
            merchant_key_hash=sha256(item.merchant.merchant_key.encode()).hexdigest(),
            name=item.merchant.name,
            seller_type=item.merchant.seller_type,
            verification_status=item.merchant.verification_status,
            external_merchant_id=item.merchant.external_merchant_id,
            observed_at=trusted_at,
            refresh_current=trusted,
        )
        listing = catalog.get_or_create_source_listing(
            source_channel_id=channel_id,
            merchant_id=merchant.id,
            listing_key=item.listing_key,
            listing_key_hash=sha256(item.listing_key.encode()).hexdigest(),
            price_nature=item.price_nature,
            canonical_url=item.url,
            url_hash=sha256(item.url.encode()).hexdigest(),
            observed_at=trusted_at,
            external_product_id=item.external_product_id,
            external_sku_id=item.external_sku_id,
            refresh_current=trusted,
            lifecycle_status=(
                LifecycleStatus.INACTIVE
                if trusted_candidates
                and all(
                    candidate.availability is Availability.OFF_SHELF
                    for candidate in trusted_candidates
                )
                else LifecycleStatus.ACTIVE
            ),
        )
        revision = catalog.get_or_create_listing_revision(
            source_listing_id=listing.id,
            first_crawl_record_id=record_id,
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
            normalizer_version=prepared.normalizer_version,
            quality_status=identity.quality_status,
            rejection_code=identity.rejection_code,
            observed_at=observed_at,
        )
        if identity.quality_status is not QualityStatus.ACCEPTED:
            return
        if standard_item is not None:
            self._ensure_variant_match(session, standard_item, revision, prepared)
        if trusted:
            catalog.set_current_revision(
                source_listing_id=listing.id,
                revision_id=revision.id,
                observed_at=trusted_at,
            )
        prices = CatalogPriceRepository(session)
        for candidate in prepared.evaluated:
            observation = CatalogPriceObservation(
                **candidate.model_dump(exclude={"region", "evidence_hash", "source_observed_at"}),
                source_listing_id=listing.id,
                listing_revision_id=revision.id,
                crawl_record_id=record_id,
                region_scope=candidate.region.scope,
                region_code=candidate.region.code,
                source_hash=candidate.evidence_hash or artifact.source_hash,
                observed_at=candidate.source_observed_at or result.fetched_at,
            )
            if observation.eligible_for_current:
                if public_dataset:
                    observation = prices.resolve_same_time_correction(observation)
                else:
                    prices.assert_same_time_consistent(observation)
            prices.record(observation)

    @staticmethod
    def _ensure_item(
        session: Session,
        connector: Connector,
        product: ParsedCatalogProduct,
    ) -> CatalogItem:
        brand = session.scalar(select(CatalogBrand).where(CatalogBrand.code == product.brand_code))
        category = session.scalar(
            select(TaxonomyCategory).where(TaxonomyCategory.code == product.category_code)
        )
        if brand is None or category is None:
            raise CatalogIdentityConflictError(
                "device brand/category must be seeded before collection"
            )
        return GeneralCatalogRepository(session).get_or_create_item(
            category_id=category.id,
            brand_id=brand.id,
            canonical_key=device_item_key(
                brand_code=brand.code,
                channel_code=connector.channel_code,
                product_id=product.external_product_id,
            ),
            item_type=ItemType.MODEL,
            name=product.name,
            series_name=product.series_name,
            model_number=product.model_number,
            base_attributes=product.base_attributes,
            record_origin=RecordOrigin.RULE,
        )

    @staticmethod
    def _ensure_variant_match(
        session: Session,
        item: CatalogItem,
        revision: ListingRevision,
        prepared: PreparedCatalogRow,
    ) -> None:
        catalog = GeneralCatalogRepository(session)
        identity = prepared.identity
        if identity.base_unit is None:
            raise CatalogIdentityConflictError("device variant requires a proven base unit")
        variant = catalog.get_or_create_variant(
            catalog_item_id=item.id,
            variant_key=identity.build_fingerprint(),
            name=prepared.row.parsed.source_title[:255],
            condition_code=identity.condition_code,
            measure_type=identity.measure_type,
            base_unit=identity.base_unit,
            quantity_value=identity.base_quantity_value,
            quantity_min=identity.base_quantity_min,
            quantity_max=identity.base_quantity_max,
            package_count=identity.package_count,
            attributes=identity.normalized_attributes,
            identity_fingerprint=identity.build_fingerprint(),
            manufacturer_part_number=identity.normalized_attributes.get("manufacturer_part_number"),
        )
        matcher_version = prepared.normalizer_version
        existing = session.scalar(
            select(ListingMatch).where(
                ListingMatch.listing_revision_id == revision.id,
                ListingMatch.item_variant_id == variant.id,
                ListingMatch.matcher_version == matcher_version,
            )
        )
        catalog.create_match(
            listing_revision_id=revision.id,
            item_variant_id=variant.id,
            match_status=MatchStatus.ACCEPTED,
            match_method=MatchMethod.RULE,
            confidence=Decimal("1"),
            matcher_version=matcher_version,
            matched_fields={"identity_fingerprint": identity.build_fingerprint()},
            effective_from=existing.effective_from
            if existing is not None
            else revision.first_observed_at,
        )

    def _record_failure(
        self,
        run_id: int,
        connector: Connector,
        unit: CollectionUnit,
        request: CatalogCollectionRequest,
        result: FetchResult | None,
        artifact: ArtifactReference | None,
        code: str,
        message: str,
        *,
        skipped: bool = False,
        pending: bool = False,
        extra_manifest: list[dict[str, Any]] | None = None,
    ) -> None:
        with self.session_factory.begin() as session:
            CatalogCrawlRepository(session).add_record(
                crawl_run_id=run_id,
                source_listing_id=None,
                entity_type=self._entity_type(unit),
                entity_key=self._entity_key(unit),
                request_url=result.request_url if result else unit.url,
                final_url=result.final_url if result else unit.url,
                fetch_method=CollectionFetchMethod(result.fetch_method.value)
                if result
                else connector.fetch_method,
                http_status=result.status_code
                if result and 100 <= result.status_code <= 599
                else None,
                fetch_status=(
                    OperationStatus.SKIPPED
                    if skipped
                    else OperationStatus.SUCCEEDED
                    if result and 100 <= result.status_code <= 599
                    else OperationStatus.FAILED
                ),
                parse_status=OperationStatus.SUCCEEDED
                if pending
                else OperationStatus.FAILED
                if result
                else OperationStatus.SKIPPED,
                validation_status=OperationStatus.PENDING
                if pending
                else OperationStatus.FAILED
                if skipped
                else OperationStatus.SKIPPED,
                error_code=code,
                error_message=message[:2000],
                raw_hash=artifact.source_hash if artifact else None,
                raw_path=artifact.relative_path if artifact else None,
                artifact_manifest=self._manifest(connector, request, unit, result, artifact)
                + (extra_manifest or []),
                content_type=(result.content_type or None) if result else None,
                raw_size_bytes=artifact.size_bytes if artifact else None,
                duration_ms=result.duration_ms if result else 0,
                fetched_at=result.fetched_at if result else utc_now_naive(),
            )

    @staticmethod
    def _manifest(
        connector: Connector,
        request: CatalogCollectionRequest,
        unit: CollectionUnit,
        result: FetchResult | None,
        artifact: ArtifactReference | None,
    ) -> list[dict[str, Any]]:
        manifest: list[dict[str, Any]] = [
            {
                "role": "discovery_context",
                "channel_code": connector.channel_code,
                "request": request.model_dump(mode="json"),
                "discovery": unit.model_dump(mode="json"),
                **(
                    {"brand_code": connector.brand_code}
                    if isinstance(connector, CatalogConnector)
                    else {}
                ),
            }
        ]
        if isinstance(unit, DiscoveredCatalogDataset):
            manifest.extend(
                [
                    {
                        "role": "source_page",
                        "url": unit.source_page_url,
                        "source_date": unit.metadata.get("source_date"),
                        "source_hash": unit.metadata.get("article_hash"),
                    },
                    {"role": "index", "source_hash": unit.metadata.get("index_hash")},
                ]
            )
        if result is not None and artifact is not None:
            manifest.append(
                {
                    "role": "response",
                    "url": result.request_url,
                    "final_url": result.final_url,
                    "fetched_at": result.fetched_at.isoformat(timespec="milliseconds"),
                    "source_hash": artifact.source_hash,
                    "relative_path": artifact.relative_path,
                }
            )
        return manifest

    @staticmethod
    def _entity_type(unit: CollectionUnit) -> CatalogEntityType:
        return (
            CatalogEntityType.PRODUCT
            if isinstance(unit, DiscoveredCatalogProduct)
            else CatalogEntityType.PUBLIC_PRICE
        )

    @staticmethod
    def _entity_key(unit: CollectionUnit) -> str:
        return (
            unit.external_product_id
            if isinstance(unit, DiscoveredCatalogProduct)
            else f"PUBLIC_PRICE:{sha256(unit.dataset_key.encode()).hexdigest()}"
        )

    def validate_source(self, connector: Connector) -> _ChannelRuntime:
        """Check enabled source settings for collection or scheduler preflight, without fetching."""
        with self.session_factory() as session:
            channel = session.scalar(
                select(SourceChannel).where(
                    SourceChannel.code == connector.channel_code.strip().upper(),
                )
            )
            if channel is None or not channel.enabled:
                raise CatalogConfigurationError(
                    f"catalog source channel is not enabled: {connector.channel_code}"
                )
            if channel.connector_code != connector.connector_code or channel.currency != "CNY":
                raise CatalogConfigurationError(
                    "source channel connector/currency differs from approved configuration"
                )
            allowed = set(channel.allowed_domains)
            if not allowed or not allowed.issubset(connector.allowed_domains):
                raise CatalogConfigurationError(
                    "database domains exceed the connector approved scope"
                )
            if isinstance(connector, CatalogConnector) and (
                channel.source_type != SourceType.OFFICIAL_MALL.value
                or channel.business_mode != BusinessMode.SELF_OPERATED.value
                or channel.region_mode != RegionMode.NATIONAL.value
            ):
                raise CatalogConfigurationError(
                    "device source must be a national self-operated official mall"
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
                category.attribute_profile_code, category.attribute_profile_version
            )

    def _start_run(
        self,
        connector: Connector,
        request: CatalogCollectionRequest,
        trigger_type: CollectionTriggerType,
        channel_id: int,
    ) -> int:
        with self.session_factory.begin() as session:
            now = utc_now_naive()
            # The named source/region lock is held. Leave other category scopes untouched.
            stale_runs = (
                session.scalars(
                    select(CatalogCrawlRun)
                    .where(
                        CatalogCrawlRun.source_channel_id == channel_id,
                        CatalogCrawlRun.region_scope == request.region_scope.value,
                        CatalogCrawlRun.region_code == request.region_code,
                        CatalogCrawlRun.status == RunStatus.RUNNING.value,
                        CatalogCrawlRun.started_at
                        < now - timedelta(minutes=self.stale_run_after_minutes),
                    )
                    .with_for_update()
                ).all()
                if isinstance(connector, CatalogConnector) and not request.source_item_codes
                else []
            )
            for stale in stale_runs:
                if set(stale.category_scope) == set(request.category_codes):
                    stale.status = RunStatus.FAILED.value
                    stale.finished_at = now
                    stale.error_summary = {"STALE_RUNNING_RECOVERED": 1}
            return (
                CatalogCrawlRepository(session)
                .start_run(
                    source_channel_id=channel_id,
                    region_scope=request.region_scope,
                    region_code=request.region_code,
                    category_scope=list(request.category_codes),
                    run_type=RunType.FULL,
                    trigger_type=trigger_type,
                    adapter_version=connector.version,
                    policy_version=self.price_policy.version,
                    started_at=now,
                )
                .id
            )

    def _finish_run(
        self,
        run_id: int,
        totals: Counter[str],
        errors: Counter[str],
        status: RunStatus,
    ) -> CatalogCollectionOutcome:
        with self.session_factory.begin() as session:
            CatalogCrawlRepository(session).finish_run(
                crawl_run_id=run_id,
                status=status,
                finished_at=utc_now_naive(),
                discovered_count=totals["discovered"],
                fetched_count=totals["fetched"],
                accepted_count=totals["accepted"],
                review_count=totals["review"],
                rejected_count=totals["rejected"],
                failed_count=totals["failed"],
                error_summary=dict(errors) or None,
            )
        return CatalogCollectionOutcome(
            crawl_run_id=run_id,
            status=status,
            discovered_count=totals["discovered"],
            fetched_count=totals["fetched"],
            accepted_count=totals["accepted"],
            review_count=totals["review"],
            rejected_count=totals["rejected"],
            failed_count=totals["failed"],
        )

    @staticmethod
    def _point_time_result(result: FetchResult) -> FetchResult:
        # MySQL facts have millisecond precision; comparisons must use the same clock grain.
        observed = result.fetched_at
        if observed.tzinfo is not None:
            observed = observed.astimezone(UTC).replace(tzinfo=None)
        return replace(
            result, fetched_at=observed.replace(microsecond=observed.microsecond // 1000 * 1000)
        )

    @staticmethod
    def _exception_code(error: Exception) -> str:
        declared = getattr(error, "error_code", None)
        if isinstance(declared, str) and declared.strip():
            return declared.strip().upper()[:64]
        return type(error).__name__.upper()[:64]
