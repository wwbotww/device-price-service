"""Read immutable catalog evidence without fetching or changing stored facts."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime, time
from decimal import Decimal
from hashlib import sha256
from typing import Any
from urllib.parse import parse_qs, urlsplit
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from device_price_service.crawlers.catalog import (
    CatalogConnector,
    CatalogConnectorRegistry,
    CatalogDatasetConnector,
)
from device_price_service.crawlers.mofcom_fresh import (
    MOFCOM_SUPPORTED_COMMODITIES,
    MofcomFreshWholesaleConnector,
)
from device_price_service.crawlers.shanghai_fresh import ShanghaiFreshRetailConnector
from device_price_service.db.catalog_models import (
    CatalogCrawlRecord,
    CatalogCrawlRun,
    CatalogPriceObservationRecord,
    SourceChannel,
    TaxonomyCategory,
)
from device_price_service.domain.catalog_crawl import (
    CatalogCollectionRequest,
    DiscoveredCatalogDataset,
    DiscoveredCatalogProduct,
)
from device_price_service.domain.catalog_enums import RegionScope
from device_price_service.domain.crawl import FetchResult
from device_price_service.domain.enums import FetchMethod
from device_price_service.fetchers.url_policy import UrlPolicy
from device_price_service.normalization.catalog_rules import CategoryRule, CategoryRuleRegistry
from device_price_service.services.artifact_store import RawArtifactStore
from device_price_service.services.catalog_preparation import (
    PreparedCatalogRows,
    prepare_catalog_product,
    prepare_catalog_rows,
)
from device_price_service.validation.catalog_price_policy import CatalogPricePolicy


class ReplayError(ValueError):
    """Evidence is missing, inconsistent, or cannot be replayed deterministically."""


@dataclass(frozen=True, slots=True)
class ReplayOutcome:
    record_id: int
    channel_code: str
    fetched_at: datetime
    parser_version: str
    historical: dict[str, Any]
    prepared: PreparedCatalogRows | None
    evidence: list[dict[str, Any]]
    context_recovered: bool = False

    def to_dict(self) -> dict[str, Any]:
        static = None
        status = "SUCCEEDED"
        if self.prepared is not None:
            status = self.prepared.status.value
            static = {
                "status": status,
                "accepted_count": self.prepared.accepted_count,
                "review_count": self.prepared.review_count,
                "rejected_count": self.prepared.rejected_count,
                "error_code": self.prepared.error_code,
                "rows": [
                    {
                        "listing": row.row.item.model_dump(mode="json"),
                        "source_title": row.row.parsed.source_title,
                        "identity": row.identity.model_dump(mode="json"),
                        "normalizer_version": row.normalizer_version,
                        "prices": [
                            {
                                **candidate.model_dump(mode="json"),
                                "observed_at": (
                                    candidate.source_observed_at or self.fetched_at
                                ).isoformat(timespec="milliseconds"),
                            }
                            for candidate in row.evaluated
                        ],
                    }
                    for row in self.prepared.rows
                ],
            }
            # Static parsing cannot upgrade a point that never passed runtime confirmation.
            if self.historical["error_code"] in {
                "PRICE_CHANGE_CONFIRMATION_REQUIRED",
                "PRICE_CHANGE_UNCONFIRMED",
            }:
                status = "PARTIAL" if self.prepared.accepted_count else "FAILED"
        return {
            "status": status,
            "mode": "STATIC_REPLAY",
            "record_id": self.record_id,
            "channel_code": self.channel_code,
            "fetched_at": self.fetched_at.isoformat(timespec="milliseconds"),
            "parser_version": self.parser_version,
            "context_recovered": self.context_recovered,
            "static_validation": static,
            "historical_validation": self.historical,
            "evidence": self.evidence,
            "notice": (
                "只读重放原证据，不代表新时点报价；静态校验不重做变价复抓或缺失确认。"
                "历史校验与已存事实单独呈现，未确认变价不会因本次解析通过而转为可信价格。"
            ),
        }


class ReplayService:
    def __init__(
        self,
        *,
        session_factory: sessionmaker[Session],
        artifact_store: RawArtifactStore,
        registry: CatalogConnectorRegistry,
        rule_registry: CategoryRuleRegistry,
        price_policy: CatalogPricePolicy | None = None,
    ) -> None:
        self.session_factory = session_factory
        self.artifact_store = artifact_store
        self.registry = registry
        self.rule_registry = rule_registry
        self.price_policy = price_policy or CatalogPricePolicy()

    def replay(self, record_id: int) -> ReplayOutcome:
        with self.session_factory() as session:
            row = session.execute(
                select(CatalogCrawlRecord, CatalogCrawlRun, SourceChannel)
                .join(CatalogCrawlRun, CatalogCrawlRecord.crawl_run_id == CatalogCrawlRun.id)
                .join(SourceChannel, CatalogCrawlRun.source_channel_id == SourceChannel.id)
                .where(CatalogCrawlRecord.id == record_id)
            ).one_or_none()
            if row is None:
                raise ReplayError(f"V2 crawl record does not exist: {record_id}")
            record, run, channel = row
            connector = self.registry.get(channel.code)
            if channel.connector_code != connector.connector_code:
                raise ReplayError("record source connector no longer matches its registered code")
            if not channel.allowed_domains or not set(channel.allowed_domains).issubset(
                connector.allowed_domains
            ):
                raise ReplayError("record source has an invalid domain allowlist")
            urls = UrlPolicy(channel.allowed_domains)
            urls.validate(record.request_url)
            urls.validate(record.final_url)
            if not record.raw_path or not record.raw_hash or record.http_status is None:
                raise ReplayError("record has no complete replayable response evidence")
            body = self.artifact_store.load(record.raw_path, expected_hash=record.raw_hash)
            if record.raw_size_bytes is not None and len(body) != record.raw_size_bytes:
                raise ReplayError("record raw_size_bytes does not match response evidence")
            evidence = self._verify_manifest(record, urls)
            request, unit, recovered = self._restore_context(record, run, connector)
            urls.validate(unit.url)
            if isinstance(unit, DiscoveredCatalogDataset):
                urls.validate(unit.source_page_url)
            historical = self._historical(session, record, run)
            if record.error_code == "OFF_SHELF_CONFIRMED":
                self._validate_confirmed_absence(record, historical, unit)
                return ReplayOutcome(
                    record.id,
                    channel.code,
                    record.fetched_at,
                    connector.version,
                    historical,
                    None,
                    evidence,
                    recovered,
                )
            if not 200 <= record.http_status < 300:
                raise ReplayError(
                    f"response HTTP {record.http_status} is not parseable price evidence"
                )
            result = FetchResult(
                request_url=record.request_url,
                final_url=record.final_url,
                status_code=record.http_status,
                headers={"content-type": record.content_type or ""},
                body=body,
                fetched_at=record.fetched_at,
                duration_ms=record.duration_ms,
                fetch_method=FetchMethod.REPLAY,
            )

            def rule_for_category(code: str) -> CategoryRule:
                category = session.scalar(
                    select(TaxonomyCategory).where(TaxonomyCategory.code == code)
                )
                if category is None or not category.is_leaf:
                    raise ReplayError(f"record category is not a configured leaf: {code}")
                # Disabling live collection does not prevent inspection of historical evidence.
                return self.rule_registry.get(
                    category.attribute_profile_code, category.attribute_profile_version
                )

            if isinstance(connector, CatalogConnector) and isinstance(
                unit, DiscoveredCatalogProduct
            ):
                product = connector.parse_product(unit, result)
                prepared = prepare_catalog_product(
                    product,
                    discovered=unit,
                    expected_brand_code=connector.brand_code,
                    request=request,
                    allowed_domains=channel.allowed_domains,
                    rule_for_category=rule_for_category,
                    price_policy=self.price_policy,
                )
            elif isinstance(connector, CatalogDatasetConnector) and isinstance(
                unit, DiscoveredCatalogDataset
            ):
                parsed = connector.parse_dataset(unit, result)
                prepared = prepare_catalog_rows(
                    parsed.rows,
                    request=request,
                    allowed_domains=channel.allowed_domains,
                    rule_for_category=rule_for_category,
                    price_policy=self.price_policy,
                )
            else:
                raise ReplayError("record entity type does not match its source connector")
            return ReplayOutcome(
                record.id,
                channel.code,
                record.fetched_at,
                connector.version,
                historical,
                prepared,
                evidence,
                recovered,
            )

    def _verify_manifest(self, record: CatalogCrawlRecord, urls: UrlPolicy) -> list[dict[str, Any]]:
        if not isinstance(record.artifact_manifest, list):
            raise ReplayError("artifact manifest must be a list")
        evidence = [
            {
                "role": "response",
                "url": record.request_url,
                "final_url": record.final_url,
                "fetched_at": record.fetched_at.isoformat(timespec="milliseconds"),
                "source_hash": record.raw_hash,
                "relative_path": record.raw_path,
            }
        ]
        responses = 0
        for part in record.artifact_manifest:
            if not isinstance(part, dict):
                raise ReplayError("invalid artifact manifest entry")
            for key in ("url", "final_url"):
                if part.get(key) is not None:
                    if not isinstance(part[key], str):
                        raise ReplayError("manifest response URL must be a string")
                    urls.validate(part[key])
            if part.get("role") == "response":
                responses += 1
                if any(part.get(key) != value for key, value in evidence[0].items()):
                    raise ReplayError("response manifest disagrees with its immutable record")
            elif part.get("relative_path") is not None:
                if not all(
                    isinstance(part.get(key), str) and part[key]
                    for key in ("source_hash", "relative_path", "url", "final_url", "fetched_at")
                ):
                    raise ReplayError("additional response has incomplete evidence metadata")
                datetime.fromisoformat(part["fetched_at"])
                self.artifact_store.load(part["relative_path"], expected_hash=part["source_hash"])
                evidence.append(dict(part))
        if responses > 1:
            raise ReplayError("duplicate response manifest")
        return evidence

    @staticmethod
    def _restore_context(
        record: CatalogCrawlRecord,
        run: CatalogCrawlRun,
        connector: CatalogConnector | CatalogDatasetConnector,
    ) -> tuple[CatalogCollectionRequest, DiscoveredCatalogProduct | DiscoveredCatalogDataset, bool]:
        contexts = [p for p in record.artifact_manifest if p.get("role") == "discovery_context"]
        if len(contexts) > 1:
            raise ReplayError("ambiguous discovery context")
        if not contexts:
            if not isinstance(connector, CatalogDatasetConnector):
                raise ReplayError("product record is missing immutable discovery context")
            request, dataset = _restore_government_context(record, run, connector)
            return request, dataset, True
        context = contexts[0]
        if context.get("channel_code") != connector.channel_code:
            raise ReplayError("discovery context source differs from recorded source")
        request = CatalogCollectionRequest.model_validate(context.get("request"))
        if (request.region_scope.value, request.region_code, list(request.category_codes)) != (
            run.region_scope,
            run.region_code,
            run.category_scope,
        ):
            raise ReplayError("discovery request differs from recorded run scope")
        unit: DiscoveredCatalogProduct | DiscoveredCatalogDataset
        if record.entity_type == "PRODUCT" and isinstance(connector, CatalogConnector):
            if context.get("brand_code") != connector.brand_code:
                raise ReplayError("discovery context brand differs from recorded source")
            unit = DiscoveredCatalogProduct.model_validate(context.get("discovery"))
            expected_key = unit.external_product_id
        elif record.entity_type == "PUBLIC_PRICE" and isinstance(
            connector, CatalogDatasetConnector
        ):
            unit = DiscoveredCatalogDataset.model_validate(context.get("discovery"))
            expected_key = f"PUBLIC_PRICE:{sha256(unit.dataset_key.encode()).hexdigest()}"
        else:
            raise ReplayError("unsupported record entity type for source connector")
        if record.entity_key != expected_key:
            raise ReplayError("discovery identity differs from recorded entity key")
        return request, unit, False

    @staticmethod
    def _historical(
        session: Session, record: CatalogCrawlRecord, run: CatalogCrawlRun
    ) -> dict[str, Any]:
        facts = session.scalars(
            select(CatalogPriceObservationRecord)
            .where(CatalogPriceObservationRecord.crawl_record_id == record.id)
            .order_by(CatalogPriceObservationRecord.id)
        ).all()
        return {
            "run_status": run.status,
            "adapter_version": run.adapter_version,
            "policy_version": run.policy_version,
            "fetch_status": record.fetch_status,
            "parse_status": record.parse_status,
            "validation_status": record.validation_status,
            "error_code": record.error_code,
            "facts": [
                {
                    column.name: _json_scalar(getattr(fact, column.name))
                    for column in CatalogPriceObservationRecord.__table__.columns
                }
                for fact in facts
            ],
        }

    @staticmethod
    def _validate_confirmed_absence(
        record: CatalogCrawlRecord,
        historical: dict[str, Any],
        unit: DiscoveredCatalogProduct | DiscoveredCatalogDataset,
    ) -> None:
        parts = [
            p for p in record.artifact_manifest if p.get("role") == "confirmed_product_absence"
        ]
        facts = historical["facts"]
        expected = {str(fact["source_listing_id"]): fact["listing_revision_id"] for fact in facts}
        if (
            not isinstance(unit, DiscoveredCatalogProduct)
            or record.http_status not in {404, 410}
            or record.validation_status != "SUCCEEDED"
            or record.parse_status != "SKIPPED"
            or record.request_url != unit.url
            or record.final_url != unit.url
            or len(parts) != 1
            or not expected
            or parts[0].get("listing_revision_ids") != expected
            or any(
                fact["price_type"] != "AVAILABILITY_ONLY"
                or fact["availability"] != "OFF_SHELF"
                or fact["quality_status"] != "ACCEPTED"
                or fact["current_price"] is not None
                or fact["original_price"] is not None
                or fact["source_hash"] != record.raw_hash
                or fact["observed_at"] != _json_scalar(record.fetched_at)
                for fact in facts
            )
        ):
            raise ReplayError("confirmed absence record is inconsistent with its original facts")


def _restore_government_context(
    record: CatalogCrawlRecord,
    run: CatalogCrawlRun,
    connector: CatalogDatasetConnector,
) -> tuple[CatalogCollectionRequest, DiscoveredCatalogDataset]:
    """Recover the two actually deployed pre-context government evidence shapes."""
    pages = [p for p in record.artifact_manifest if p.get("role") == "source_page"]
    if record.entity_type != "PUBLIC_PRICE" or len(pages) != 1:
        raise ReplayError("government record is missing unambiguous source-page evidence")
    page = pages[0]
    if not isinstance(page.get("url"), str) or not isinstance(page.get("source_date"), str):
        raise ReplayError("government record is missing source URL or publication date")
    source_date = date.fromisoformat(page["source_date"])
    observed_at = (
        datetime.combine(source_date, time.min, ZoneInfo("Asia/Shanghai"))
        .astimezone(UTC)
        .replace(tzinfo=None)
    )
    metadata: dict[str, Any] = {"source_date": source_date.isoformat(), "time_precision": "DAY"}
    codes: tuple[str, ...] = ()
    if isinstance(connector, MofcomFreshWholesaleConnector):
        ids = parse_qs(urlsplit(record.request_url).query).get("commdityid", [])
        matches = [item for item in MOFCOM_SUPPORTED_COMMODITIES if ids == [item.commodity_id]]
        if len(matches) != 1:
            raise ReplayError("government record has no deterministic MOFCOM commodity identity")
        mapping = matches[0]
        metadata.update(commodity_code=mapping.commodity_code, commodity_id=mapping.commodity_id)
        codes = (mapping.commodity_code,)
        dataset_key = f"mofcom-bj:{mapping.commodity_id}:{source_date.isoformat()}"
    elif isinstance(connector, ShanghaiFreshRetailConnector):
        dataset_key = f"shanghai-fresh-retail:{source_date.isoformat()}"
    else:
        raise ReplayError(
            "source has no saved discovery context; deterministic recovery unavailable"
        )
    expected_key = f"PUBLIC_PRICE:{sha256(dataset_key.encode()).hexdigest()}"
    if record.entity_key != expected_key:
        raise ReplayError("recovered government identity differs from recorded entity key")
    request = CatalogCollectionRequest(
        region_scope=RegionScope(run.region_scope),
        region_code=run.region_code,
        category_codes=tuple(run.category_scope),
        source_item_codes=codes,
    )
    return request, DiscoveredCatalogDataset(
        dataset_key=dataset_key,
        url=record.request_url,
        source_page_url=page["url"],
        source_observed_at=observed_at,
        metadata=metadata,
    )


def _json_scalar(value: Any) -> Any:
    if isinstance(value, datetime):
        return value.isoformat(timespec="milliseconds")
    return str(value) if isinstance(value, Decimal) else value
