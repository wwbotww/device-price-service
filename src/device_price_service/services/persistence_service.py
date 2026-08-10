from __future__ import annotations

from sqlalchemy.orm import Session, sessionmaker

from device_price_service.db.models import PriceCurrent
from device_price_service.db.repositories import PriceRepository
from device_price_service.domain.models import PriceObservation


class PricePersistenceService:
    def __init__(self, session_factory: sessionmaker[Session]) -> None:
        self.session_factory = session_factory

    def persist(self, observation: PriceObservation) -> int:
        with self.session_factory.begin() as session:
            current = PriceRepository(session).record(observation)
            session.flush()
            return current.id

    def get_current(self, offer_id: int) -> PriceCurrent | None:
        with self.session_factory() as session:
            return session.query(PriceCurrent).filter_by(offer_id=offer_id).one_or_none()
