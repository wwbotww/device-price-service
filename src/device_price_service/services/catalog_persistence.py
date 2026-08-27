from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from device_price_service.db.catalog_models import CatalogPriceCurrent
from device_price_service.db.catalog_repositories import (
    CatalogPriceRepository,
    CatalogPriceWriteResult,
)
from device_price_service.domain.catalog_enums import RegionScope
from device_price_service.domain.catalog_models import CatalogPriceObservation


class CatalogPricePersistenceService:
    """Own the transaction boundary for one V2 point-in-time price write."""

    def __init__(self, session_factory: sessionmaker[Session]) -> None:
        self.session_factory = session_factory

    def persist(self, observation: CatalogPriceObservation) -> CatalogPriceWriteResult:
        with self.session_factory.begin() as session:
            return CatalogPriceRepository(session).record(observation)

    def rebuild_current(self, *, source_listing_id: int) -> int:
        with self.session_factory.begin() as session:
            return CatalogPriceRepository(session).rebuild_current(
                source_listing_id=source_listing_id
            )

    def get_current(
        self,
        *,
        source_listing_id: int,
        region_scope: RegionScope,
        region_code: str,
    ) -> CatalogPriceCurrent | None:
        with self.session_factory() as session:
            return session.scalar(
                select(CatalogPriceCurrent).where(
                    CatalogPriceCurrent.source_listing_id == source_listing_id,
                    CatalogPriceCurrent.region_scope == region_scope.value,
                    CatalogPriceCurrent.region_code == region_code.strip().upper(),
                )
            )
