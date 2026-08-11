from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from device_price_service.db.models import (
    Brand,
    Category,
    CrawlRecord,
    CrawlRun,
    OfficialOffer,
    PriceCurrent,
    PriceHistory,
    Product,
    SalesChannel,
    Sku,
)
from device_price_service.domain.enums import (
    Availability,
    EntityType,
    FetchMethod,
    OperationStatus,
    OriginalPriceType,
    RunStatus,
    RunType,
    TriggerType,
)
from device_price_service.domain.models import PriceObservation, utc_now_naive


class RepositoryError(RuntimeError):
    """Base class for persistence errors with stable behavior."""


class OutOfOrderObservationError(RepositoryError):
    """Raised when an old observation would overwrite newer trusted state."""


class InconsistentPriceStateError(RepositoryError):
    """Raised when current and history projections no longer agree."""


@dataclass(frozen=True, slots=True)
class MissingProductCandidate:
    product_id: int
    official_product_id: str
    url: str
    category_code: str


class CatalogRepository:
    def __init__(self, session: Session) -> None:
        self.session = session

    def require_brand(self, code: str) -> Brand:
        brand = self.session.scalar(select(Brand).where(Brand.code == code))
        if brand is None:
            raise RepositoryError(f"unknown brand code: {code}")
        return brand

    def require_category(self, code: str) -> Category:
        category = self.session.scalar(select(Category).where(Category.code == code))
        if category is None:
            raise RepositoryError(f"unknown category code: {code}")
        return category

    def require_channel(self, code: str) -> SalesChannel:
        channel = self.session.scalar(select(SalesChannel).where(SalesChannel.code == code))
        if channel is None:
            raise RepositoryError(f"unknown channel code: {code}")
        return channel

    def upsert_product(
        self,
        *,
        brand_id: int,
        category_id: int,
        official_product_id: str,
        name: str,
        official_url: str,
        observed_at: datetime,
        series_name: str | None = None,
        model_number: str | None = None,
        lifecycle_status: str = "ACTIVE",
    ) -> Product:
        product = self.session.scalar(
            select(Product).where(
                Product.brand_id == brand_id,
                Product.official_product_id == official_product_id,
            )
        )
        if product is None:
            product = Product(
                brand_id=brand_id,
                category_id=category_id,
                official_product_id=official_product_id,
                name=name,
                series_name=series_name,
                model_number=model_number,
                official_url=official_url,
                lifecycle_status=lifecycle_status,
                first_seen_at=observed_at,
                last_seen_at=observed_at,
            )
            self.session.add(product)
            self.session.flush()
            return product

        product.category_id = category_id
        product.name = name
        product.series_name = series_name
        product.model_number = model_number
        product.official_url = official_url
        product.lifecycle_status = lifecycle_status
        product.last_seen_at = max(product.last_seen_at, observed_at)
        return product

    def upsert_sku(
        self,
        *,
        product_id: int,
        official_sku_id: str | None,
        name: str,
        spec_fingerprint: str,
        observed_at: datetime,
        color: str | None = None,
        capacity: str | None = None,
        memory: str | None = None,
        connectivity: str | None = None,
        size: str | None = None,
        attributes: dict[str, Any] | None = None,
        status: str = "ACTIVE",
    ) -> Sku:
        clauses = [Sku.product_id == product_id]
        if official_sku_id is not None:
            clauses.append(Sku.official_sku_id == official_sku_id)
        else:
            clauses.append(Sku.spec_fingerprint == spec_fingerprint)
        sku = self.session.scalar(select(Sku).where(*clauses))

        if sku is None:
            sku = Sku(
                product_id=product_id,
                official_sku_id=official_sku_id,
                name=name,
                color=color,
                capacity=capacity,
                memory=memory,
                connectivity=connectivity,
                size=size,
                attributes=attributes or {},
                spec_fingerprint=spec_fingerprint,
                status=status,
                first_seen_at=observed_at,
                last_seen_at=observed_at,
            )
            self.session.add(sku)
            self.session.flush()
            return sku

        sku.name = name
        sku.color = color
        sku.capacity = capacity
        sku.memory = memory
        sku.connectivity = connectivity
        sku.size = size
        sku.attributes = attributes or {}
        sku.spec_fingerprint = spec_fingerprint
        sku.status = status
        sku.last_seen_at = max(sku.last_seen_at, observed_at)
        return sku

    def upsert_offer(
        self,
        *,
        sku_id: int,
        channel_id: int,
        official_offer_id: str | None,
        source_url: str,
        availability: str,
        observed_at: datetime,
    ) -> OfficialOffer:
        offer = self.session.scalar(
            select(OfficialOffer).where(
                OfficialOffer.sku_id == sku_id,
                OfficialOffer.channel_id == channel_id,
            )
        )
        if offer is None:
            offer = OfficialOffer(
                sku_id=sku_id,
                channel_id=channel_id,
                official_offer_id=official_offer_id,
                source_url=source_url,
                availability=availability,
                consecutive_misses=0,
                first_seen_at=observed_at,
                last_seen_at=observed_at,
            )
            self.session.add(offer)
            self.session.flush()
            return offer

        offer.official_offer_id = official_offer_id
        offer.source_url = source_url
        offer.availability = availability
        offer.consecutive_misses = 0
        offer.last_seen_at = max(offer.last_seen_at, observed_at)
        return offer

    def current_prices_for_product(
        self,
        *,
        brand_code: str,
        channel_code: str,
        official_product_id: str,
    ) -> dict[str, Decimal]:
        rows = self.session.execute(
            select(Sku.spec_fingerprint, PriceCurrent.current_price)
            .select_from(Product)
            .join(Brand, Product.brand_id == Brand.id)
            .join(Sku, Sku.product_id == Product.id)
            .join(OfficialOffer, OfficialOffer.sku_id == Sku.id)
            .join(SalesChannel, OfficialOffer.channel_id == SalesChannel.id)
            .join(PriceCurrent, PriceCurrent.offer_id == OfficialOffer.id)
            .where(
                Brand.code == brand_code,
                SalesChannel.code == channel_code,
                Product.official_product_id == official_product_id,
                PriceCurrent.current_price.is_not(None),
            )
        ).all()
        return {
            str(fingerprint): price for fingerprint, price in rows if isinstance(price, Decimal)
        }

    def active_product_count(self, channel_id: int) -> int:
        return int(
            self.session.scalar(
                select(func.count(func.distinct(Product.id)))
                .select_from(Product)
                .join(Sku, Sku.product_id == Product.id)
                .join(OfficialOffer, OfficialOffer.sku_id == Sku.id)
                .where(
                    OfficialOffer.channel_id == channel_id,
                    OfficialOffer.availability != Availability.OFF_SHELF.value,
                )
            )
            or 0
        )

    def register_missing_products(
        self,
        *,
        channel_id: int,
        seen_product_ids: set[str],
        confirmation_runs: int,
    ) -> list[MissingProductCandidate]:
        query = (
            select(Product, Category.code)
            .distinct()
            .join(Category, Product.category_id == Category.id)
            .join(Sku, Sku.product_id == Product.id)
            .join(OfficialOffer, OfficialOffer.sku_id == Sku.id)
            .where(
                OfficialOffer.channel_id == channel_id,
                OfficialOffer.availability != Availability.OFF_SHELF.value,
            )
        )
        if seen_product_ids:
            query = query.where(Product.official_product_id.not_in(seen_product_ids))

        candidates: list[MissingProductCandidate] = []
        for product, category_code in self.session.execute(query).all():
            offers = self.session.scalars(
                select(OfficialOffer)
                .join(Sku, OfficialOffer.sku_id == Sku.id)
                .where(
                    Sku.product_id == product.id,
                    OfficialOffer.channel_id == channel_id,
                    OfficialOffer.availability != Availability.OFF_SHELF.value,
                )
                .with_for_update()
            ).all()
            for offer in offers:
                offer.consecutive_misses += 1
            if offers and min(offer.consecutive_misses for offer in offers) >= confirmation_runs:
                candidates.append(
                    MissingProductCandidate(
                        product_id=product.id,
                        official_product_id=product.official_product_id,
                        url=product.official_url,
                        category_code=str(category_code),
                    )
                )
        return candidates

    def confirm_product_off_shelf(
        self,
        *,
        product_id: int,
        channel_id: int,
        crawl_run_id: int,
        observed_at: datetime,
        source_hash: str,
        confirmation_runs: int,
    ) -> None:
        product = self.session.scalar(
            select(Product).where(Product.id == product_id).with_for_update()
        )
        if product is None:
            raise RepositoryError(f"unknown product id: {product_id}")
        product.lifecycle_status = "INACTIVE"

        skus = self.session.scalars(
            select(Sku).where(Sku.product_id == product_id).with_for_update()
        ).all()
        price_repository = PriceRepository(self.session)
        for sku in skus:
            sku.status = "INACTIVE"
            offer = self.session.scalar(
                select(OfficialOffer)
                .where(
                    OfficialOffer.sku_id == sku.id,
                    OfficialOffer.channel_id == channel_id,
                )
                .with_for_update()
            )
            if offer is None:
                continue
            current = self.session.scalar(
                select(PriceCurrent).where(PriceCurrent.offer_id == offer.id)
            )
            if current is not None:
                price_repository.record(
                    PriceObservation(
                        offer_id=offer.id,
                        crawl_run_id=crawl_run_id,
                        currency=current.currency,
                        original_price=current.original_price,
                        original_price_type=OriginalPriceType(current.original_price_type),
                        current_price=current.current_price,
                        availability=Availability.OFF_SHELF,
                        observed_at=max(observed_at, current.observed_at),
                        source_hash=source_hash,
                    )
                )
            else:
                offer.availability = Availability.OFF_SHELF.value
            offer.consecutive_misses = confirmation_runs


class CrawlRunRepository:
    def __init__(self, session: Session) -> None:
        self.session = session

    def start(
        self,
        *,
        channel_id: int,
        run_type: RunType,
        trigger_type: TriggerType,
        adapter_version: str,
        started_at: datetime | None = None,
    ) -> CrawlRun:
        run = CrawlRun(
            channel_id=channel_id,
            run_type=run_type.value,
            trigger_type=trigger_type.value,
            status=RunStatus.RUNNING.value,
            adapter_version=adapter_version,
            started_at=started_at or utc_now_naive(),
        )
        self.session.add(run)
        self.session.flush()
        return run

    def finish(
        self,
        run: CrawlRun,
        *,
        status: RunStatus,
        discovered_count: int = 0,
        success_count: int = 0,
        skipped_count: int = 0,
        failed_count: int = 0,
        error_summary: dict[str, Any] | None = None,
        finished_at: datetime | None = None,
    ) -> CrawlRun:
        if status is RunStatus.RUNNING:
            raise RepositoryError("finish status cannot be RUNNING")
        run.status = status.value
        run.discovered_count = discovered_count
        run.success_count = success_count
        run.skipped_count = skipped_count
        run.failed_count = failed_count
        run.error_summary = error_summary
        run.finished_at = finished_at or utc_now_naive()
        return run

    def fail_stale_runs(
        self,
        *,
        channel_id: int,
        stale_before: datetime,
        finished_at: datetime | None = None,
    ) -> int:
        stale_runs = self.session.scalars(
            select(CrawlRun)
            .where(
                CrawlRun.channel_id == channel_id,
                CrawlRun.status == RunStatus.RUNNING.value,
                CrawlRun.started_at < stale_before,
            )
            .with_for_update()
        ).all()
        ended_at = finished_at or utc_now_naive()
        for run in stale_runs:
            run.status = RunStatus.FAILED.value
            run.finished_at = ended_at
            run.failed_count = max(run.failed_count, 1)
            run.error_summary = {"STALE_RUN": 1}
        return len(stale_runs)


class CrawlRecordRepository:
    def __init__(self, session: Session) -> None:
        self.session = session

    def add(
        self,
        *,
        crawl_run_id: int,
        entity_type: EntityType,
        entity_key: str,
        request_url: str,
        final_url: str,
        fetch_method: FetchMethod,
        http_status: int | None,
        fetch_status: OperationStatus,
        parse_status: OperationStatus,
        duration_ms: int,
        fetched_at: datetime,
        error_code: str | None = None,
        error_message: str | None = None,
        raw_hash: str | None = None,
        raw_path: str | None = None,
    ) -> CrawlRecord:
        record = CrawlRecord(
            crawl_run_id=crawl_run_id,
            entity_type=entity_type.value,
            entity_key=entity_key,
            request_url=request_url,
            final_url=final_url,
            fetch_method=fetch_method.value,
            http_status=http_status,
            fetch_status=fetch_status.value,
            parse_status=parse_status.value,
            error_code=error_code,
            error_message=error_message[:1000] if error_message else None,
            raw_hash=raw_hash,
            raw_path=raw_path,
            duration_ms=duration_ms,
            fetched_at=fetched_at,
        )
        self.session.add(record)
        self.session.flush()
        return record


class PriceRepository:
    def __init__(self, session: Session) -> None:
        self.session = session

    def record(self, observation: PriceObservation) -> PriceCurrent:
        offer = self.session.scalar(
            select(OfficialOffer).where(OfficialOffer.id == observation.offer_id).with_for_update()
        )
        if offer is None:
            raise RepositoryError(f"unknown offer id: {observation.offer_id}")

        current = self.session.scalar(
            select(PriceCurrent)
            .where(PriceCurrent.offer_id == observation.offer_id)
            .with_for_update()
        )
        open_history = self.session.scalar(
            select(PriceHistory)
            .where(
                PriceHistory.offer_id == observation.offer_id,
                PriceHistory.valid_to.is_(None),
            )
            .order_by(PriceHistory.id.desc())
            .with_for_update()
        )

        if current is not None and observation.observed_at < current.observed_at:
            raise OutOfOrderObservationError(
                f"observation {observation.observed_at!s} is older than current "
                f"{current.observed_at!s}"
            )
        if (current is None) != (open_history is None):
            raise InconsistentPriceStateError(
                "price_current and open price_history must either both exist or both be absent"
            )

        offer.availability = observation.availability.value
        offer.consecutive_misses = 0
        offer.last_seen_at = max(offer.last_seen_at, observation.observed_at)

        if current is None:
            current = PriceCurrent(
                offer_id=observation.offer_id,
                currency=observation.currency,
                original_price=observation.original_price,
                original_price_type=observation.original_price_type.value,
                current_price=observation.current_price,
                observed_at=observation.observed_at,
                source_hash=observation.source_hash,
                crawl_run_id=observation.crawl_run_id,
            )
            self.session.add(current)
            self.session.add(self._new_history(observation))
            self.session.flush()
            return current

        assert open_history is not None
        state_changed = self._state(current, open_history.availability) != self._observation_state(
            observation
        )
        if state_changed:
            open_history.valid_to = observation.observed_at
            current.currency = observation.currency
            current.original_price = observation.original_price
            current.original_price_type = observation.original_price_type.value
            current.current_price = observation.current_price
            self.session.add(self._new_history(observation))
        else:
            open_history.last_observed_at = observation.observed_at

        current.observed_at = observation.observed_at
        current.source_hash = observation.source_hash
        current.crawl_run_id = observation.crawl_run_id
        self.session.flush()
        return current

    @staticmethod
    def _state(current: PriceCurrent, availability: str) -> tuple[object, ...]:
        return (
            current.currency,
            current.original_price,
            current.original_price_type,
            current.current_price,
            availability,
        )

    @staticmethod
    def _observation_state(observation: PriceObservation) -> tuple[object, ...]:
        return (
            observation.currency,
            observation.original_price,
            observation.original_price_type.value,
            observation.current_price,
            observation.availability.value,
        )

    @staticmethod
    def _new_history(observation: PriceObservation) -> PriceHistory:
        return PriceHistory(
            offer_id=observation.offer_id,
            currency=observation.currency,
            original_price=observation.original_price,
            original_price_type=observation.original_price_type.value,
            current_price=observation.current_price,
            availability=observation.availability.value,
            valid_from=observation.observed_at,
            last_observed_at=observation.observed_at,
            valid_to=None,
            source_hash=observation.source_hash,
            crawl_run_id=observation.crawl_run_id,
        )
