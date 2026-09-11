"""Device-only checks against trusted V2 facts; public datasets do not use these rules."""

from __future__ import annotations

import json
from dataclasses import dataclass, replace
from datetime import datetime
from decimal import Decimal
from hashlib import sha256

from sqlalchemy import and_, select
from sqlalchemy.orm import Session, sessionmaker

from device_price_service.db.catalog_models import (
    CatalogCrawlRecord,
    CatalogPriceObservationRecord,
    ListingRevision,
    Merchant,
    SourceListing,
)
from device_price_service.domain.catalog_crawl import DiscoveredCatalogProduct
from device_price_service.domain.catalog_enums import LifecycleStatus, QualityStatus
from device_price_service.services.catalog_preparation import PreparedCatalogRows


@dataclass(frozen=True, slots=True)
class KnownDeviceProduct:
    product_id: str
    discovery: DiscoveredCatalogProduct | None
    listing_keys: frozenset[str]
    listing_ids: tuple[int, ...]
    last_seen_at: datetime


class CatalogDeviceGuards:
    def __init__(self, session_factory: sessionmaker[Session]) -> None:
        self.session_factory = session_factory

    def snapshot(self, channel_id: int) -> dict[str, KnownDeviceProduct]:
        """Only active, trusted identities participate in discovery/missing counts."""
        with self.session_factory() as session:
            latest_record = (
                select(CatalogPriceObservationRecord.crawl_record_id)
                .where(
                    CatalogPriceObservationRecord.source_listing_id == SourceListing.id,
                    CatalogPriceObservationRecord.quality_status == QualityStatus.ACCEPTED.value,
                )
                .order_by(
                    CatalogPriceObservationRecord.observed_at.desc(),
                    CatalogPriceObservationRecord.id.desc(),
                )
                .limit(1)
                .correlate(SourceListing)
                .scalar_subquery()
            )
            rows = session.execute(
                select(SourceListing, CatalogCrawlRecord)
                .join(ListingRevision, ListingRevision.id == SourceListing.current_revision_id)
                .join(
                    CatalogCrawlRecord,
                    and_(
                        CatalogCrawlRecord.id == latest_record,
                        CatalogCrawlRecord.entity_key == SourceListing.external_product_id,
                    ),
                )
                .where(
                    SourceListing.source_channel_id == channel_id,
                    SourceListing.lifecycle_status == LifecycleStatus.ACTIVE.value,
                    SourceListing.external_product_id.is_not(None),
                    ListingRevision.quality_status == QualityStatus.ACCEPTED.value,
                )
            ).all()
            grouped: dict[str, list[tuple[SourceListing, CatalogCrawlRecord]]] = {}
            for listing, record in rows:
                grouped.setdefault(listing.external_product_id, []).append((listing, record))
            result: dict[str, KnownDeviceProduct] = {}
            for product_id, entries in grouped.items():
                discovery = None
                # Use the newest trusted evidence context: the original revision may
                # predate a legitimate URL change and its old endpoint may now return 404.
                for _, record in sorted(
                    entries, key=lambda entry: entry[1].fetched_at, reverse=True
                ):
                    for part in record.artifact_manifest:
                        if part.get("role") != "discovery_context":
                            continue
                        try:
                            candidate = DiscoveredCatalogProduct.model_validate(part["discovery"])
                        except (ValueError, KeyError):
                            continue
                        if candidate.external_product_id == product_id:
                            discovery = candidate
                            break
                    if discovery is not None:
                        break
                result[product_id] = KnownDeviceProduct(
                    product_id,
                    discovery,
                    frozenset(listing.listing_key for listing, _ in entries),
                    tuple(listing.id for listing, _ in entries),
                    max(listing.last_seen_at for listing, _ in entries),
                )
            return result

    def price_change_requires_confirmation(
        self,
        channel_id: int,
        prepared: PreparedCatalogRows,
        observed_at: datetime,
        threshold: Decimal,
    ) -> bool:
        with self.session_factory() as session:
            for row in prepared.rows:
                if row.identity.quality_status is not QualityStatus.ACCEPTED:
                    continue
                for candidate in row.evaluated:
                    if (
                        candidate.quality_status is not QualityStatus.ACCEPTED
                        or candidate.current_price is None
                    ):
                        continue
                    previous = session.scalar(
                        select(CatalogPriceObservationRecord)
                        .join(
                            SourceListing,
                            SourceListing.id == CatalogPriceObservationRecord.source_listing_id,
                        )
                        .join(Merchant, Merchant.id == SourceListing.merchant_id)
                        .join(
                            ListingRevision,
                            ListingRevision.id == CatalogPriceObservationRecord.listing_revision_id,
                        )
                        .where(
                            SourceListing.source_channel_id == channel_id,
                            SourceListing.listing_key_hash
                            == sha256(row.row.item.listing_key.encode()).hexdigest(),
                            Merchant.merchant_key_hash
                            == sha256(row.row.item.merchant.merchant_key.encode()).hexdigest(),
                            ListingRevision.identity_fingerprint
                            == row.identity.build_fingerprint(),
                            CatalogPriceObservationRecord.region_scope
                            == candidate.region.scope.value,
                            CatalogPriceObservationRecord.region_code == candidate.region.code,
                            CatalogPriceObservationRecord.quality_status
                            == QualityStatus.ACCEPTED.value,
                            CatalogPriceObservationRecord.current_price.is_not(None),
                        )
                        .order_by(
                            CatalogPriceObservationRecord.observed_at.desc(),
                            CatalogPriceObservationRecord.id.desc(),
                        )
                        .limit(1)
                    )
                    # Historical replays cannot trigger a new network observation.
                    if previous is None or observed_at <= previous.observed_at:
                        continue
                    assert previous.current_price is not None
                    if (
                        abs(candidate.current_price - previous.current_price)
                        / previous.current_price
                        > threshold
                    ):
                        return True
        return False

    def register_missing(
        self,
        missing: list[KnownDeviceProduct],
        *,
        observed_at: datetime,
        confirmation_runs: int,
    ) -> list[KnownDeviceProduct]:
        candidates: list[KnownDeviceProduct] = []
        with self.session_factory.begin() as session:
            for product in missing:
                listings = session.scalars(
                    select(SourceListing)
                    .where(SourceListing.id.in_(product.listing_ids))
                    .with_for_update()
                ).all()
                if not listings or any(observed_at <= row.last_seen_at for row in listings):
                    continue
                for listing in listings:
                    listing.consecutive_misses += 1
                if min(row.consecutive_misses for row in listings) >= confirmation_runs:
                    candidates.append(product)
        return candidates


def price_signature(prepared: PreparedCatalogRows) -> list[str]:
    """Compare every trusted SKU/region/configuration and monetary/status field."""
    return sorted(
        json.dumps(
            [
                row.row.item.discovery_key,
                row.identity.build_fingerprint(),
                candidate.model_dump(
                    mode="json",
                    exclude={"evidence_hash", "source_observed_at", "displayed_price_text"},
                ),
            ],
            sort_keys=True,
            ensure_ascii=False,
        )
        for row in prepared.rows
        for candidate in row.evaluated
        if candidate.quality_status is QualityStatus.ACCEPTED
    )


def require_product_review(prepared: PreparedCatalogRows, code: str) -> PreparedCatalogRows:
    return PreparedCatalogRows(
        [
            replace(
                row,
                evaluated=[
                    candidate.model_copy(
                        update={
                            "quality_status": QualityStatus.REVIEW_REQUIRED,
                            "rejection_code": code,
                        }
                    )
                    if candidate.quality_status is QualityStatus.ACCEPTED
                    else candidate
                    for candidate in row.evaluated
                ],
            )
            for row in prepared.rows
        ]
    )
