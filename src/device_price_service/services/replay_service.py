from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from device_price_service.crawlers.base import BrandAdapter
from device_price_service.crawlers.registry import AdapterRegistry
from device_price_service.db.models import CrawlRecord, CrawlRun, SalesChannel
from device_price_service.domain.crawl import (
    DiscoveredProduct,
    NormalizedProduct,
    replay_fetch_result,
)
from device_price_service.services.artifact_store import RawArtifactStore
from device_price_service.validation.rules import QualityValidator, ValidationReport


@dataclass(frozen=True, slots=True)
class ReplayOutcome:
    record_id: int
    adapter: BrandAdapter
    product: NormalizedProduct
    validation: ValidationReport


class ReplayService:
    def __init__(
        self,
        *,
        session_factory: sessionmaker[Session],
        artifact_store: RawArtifactStore,
        registry: AdapterRegistry,
        validator: QualityValidator,
    ) -> None:
        self.session_factory = session_factory
        self.artifact_store = artifact_store
        self.registry = registry
        self.validator = validator

    def replay(self, record_id: int) -> ReplayOutcome:
        with self.session_factory() as session:
            row = session.execute(
                select(CrawlRecord, SalesChannel)
                .join(CrawlRun, CrawlRecord.crawl_run_id == CrawlRun.id)
                .join(SalesChannel, CrawlRun.channel_id == SalesChannel.id)
                .where(CrawlRecord.id == record_id)
            ).one_or_none()
            if row is None:
                raise ValueError(f"crawl record does not exist: {record_id}")
            record, channel = row
            if not record.raw_path or not record.raw_hash:
                raise ValueError(f"crawl record has no replayable artifact: {record_id}")
            snapshot = {
                "entity_key": record.entity_key,
                "request_url": record.request_url,
                "final_url": record.final_url,
                "http_status": record.http_status or 200,
                "fetched_at": record.fetched_at,
                "raw_path": record.raw_path,
                "raw_hash": record.raw_hash,
                "allowed_domains": list(channel.allowed_domains),
                "channel_code": channel.code,
            }

        body = self.artifact_store.load(snapshot["raw_path"], expected_hash=snapshot["raw_hash"])
        adapter = self.registry.get(str(snapshot["channel_code"]))
        item = DiscoveredProduct(
            official_product_id=str(snapshot["entity_key"]),
            url=str(snapshot["request_url"]),
        )
        result = replay_fetch_result(
            request_url=str(snapshot["request_url"]),
            final_url=str(snapshot["final_url"]),
            status_code=int(snapshot["http_status"]),
            body=body,
            fetched_at=snapshot["fetched_at"],
        )
        parsed = adapter.parse_product(item, result)
        product = adapter.normalize(item, parsed)
        validation = self.validator.validate(
            product,
            expected_brand_code=adapter.brand_code,
            expected_channel_code=adapter.channel_code,
            allowed_domains=snapshot["allowed_domains"],
        )
        return ReplayOutcome(record_id, adapter, product, validation)
