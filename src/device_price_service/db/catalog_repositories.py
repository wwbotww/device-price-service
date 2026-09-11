from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any, TypeVar

from sqlalchemy import delete, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session
from sqlalchemy.sql import Select

from device_price_service.db.catalog_models import (
    CatalogBrand,
    CatalogCrawlRecord,
    CatalogCrawlRun,
    CatalogItem,
    CatalogPriceCurrent,
    CatalogPriceObservationRecord,
    ItemVariant,
    ListingMatch,
    ListingRevision,
    Merchant,
    SourceChannel,
    SourceListing,
    TaxonomyCategory,
)
from device_price_service.db.repositories import RepositoryError
from device_price_service.domain.catalog_enums import (
    AccessMode,
    BusinessMode,
    CatalogEntityType,
    CollectionFetchMethod,
    CollectionTriggerType,
    ConditionCode,
    ItemStatus,
    ItemType,
    LifecycleStatus,
    MatchMethod,
    MatchStatus,
    MeasureType,
    OperationStatus,
    PriceNature,
    QualityStatus,
    RecordOrigin,
    RegionMode,
    RegionScope,
    RunStatus,
    RunType,
    SellerType,
    SourceType,
    VerificationStatus,
)
from device_price_service.domain.catalog_models import CatalogPriceObservation

ModelT = TypeVar("ModelT", bound=object)


class CatalogConsistencyError(RepositoryError):
    """Raised when related V2 records disagree on source or product identity."""


class CatalogIdentityConflictError(RepositoryError):
    """Raised when one stable identity key resolves to incompatible facts."""


class CatalogCorrectionError(RepositoryError):
    """Raised when a correction does not identify the observation it replaces."""


@dataclass(frozen=True, slots=True)
class CatalogPriceWriteResult:
    observation_id: int
    current_id: int | None
    observation_created: bool
    current_advanced: bool


def _utc_naive_milliseconds(value: datetime) -> datetime:
    if value.tzinfo is not None:
        value = value.astimezone(UTC).replace(tzinfo=None)
    return value.replace(microsecond=(value.microsecond // 1000) * 1000)


def _sha256_hex(value: str, *, field_name: str) -> str:
    normalized = value.lower()
    if len(normalized) != 64 or any(
        character not in "0123456789abcdef" for character in normalized
    ):
        raise ValueError(f"{field_name} must be a SHA-256 hexadecimal string")
    return normalized


def _get_or_create(
    session: Session,
    statement: Select[tuple[ModelT]],
    factory: Callable[[], ModelT],
) -> tuple[ModelT, bool]:
    existing = session.scalar(statement)
    if existing is not None:
        return existing, False

    candidate = factory()
    try:
        with session.begin_nested():
            session.add(candidate)
            session.flush()
    except IntegrityError:
        # Only retry the declared business key. An unrelated integrity error
        # remains visible instead of being hidden by a broad retry.
        existing = session.scalar(statement)
        if existing is None:
            raise
        return existing, False
    return candidate, True


def _validate_quantity(
    *,
    exact: Decimal | None,
    minimum: Decimal | None,
    maximum: Decimal | None,
    unit: str | None,
    label: str,
) -> None:
    if unit is not None and not unit.strip():
        raise ValueError(f"{label} unit cannot be blank")
    for value in (exact, minimum, maximum):
        if value is not None and value <= 0:
            raise ValueError(f"{label} quantities must be greater than zero")
    if (minimum is None) != (maximum is None):
        raise ValueError(f"{label} quantity range must provide both bounds")
    if exact is not None and minimum is not None:
        raise ValueError(f"{label} quantity cannot be both exact and a range")
    if minimum is not None and maximum is not None and maximum < minimum:
        raise ValueError(f"{label} quantity maximum cannot be below minimum")
    has_quantity = exact is not None or minimum is not None
    if has_quantity != (unit is not None and bool(unit.strip())):
        raise ValueError(f"{label} quantity and unit must both be present or absent")


def _comparable_quantity_identity(
    *,
    exact: Decimal | None,
    minimum: Decimal | None,
    maximum: Decimal | None,
    unit: str | None,
    base_exact: Decimal | None,
    base_minimum: Decimal | None,
    base_maximum: Decimal | None,
    base_unit: str | None,
) -> tuple[Decimal | None, Decimal | None, Decimal | None, str | None]:
    if base_exact is not None or base_minimum is not None:
        return (base_exact, base_minimum, base_maximum, base_unit)
    return (exact, minimum, maximum, unit)


class GeneralCatalogRepository:
    """Explicit V2 identity operations; no V1 table is read or mutated."""

    def __init__(self, session: Session) -> None:
        self.session = session

    def get_or_create_brand(
        self,
        *,
        code: str,
        name_zh: str,
        name_en: str | None = None,
        status: LifecycleStatus = LifecycleStatus.ACTIVE,
    ) -> CatalogBrand:
        normalized_code = code.strip().upper()
        statement = select(CatalogBrand).where(CatalogBrand.code == normalized_code)
        brand, _ = _get_or_create(
            self.session,
            statement,
            lambda: CatalogBrand(
                code=normalized_code,
                name_zh=name_zh.strip(),
                name_en=name_en.strip() if name_en else None,
                status=status.value,
            ),
        )
        return brand

    def get_or_create_category(
        self,
        *,
        code: str,
        name_zh: str,
        level: int,
        path: str,
        is_leaf: bool,
        attribute_profile_code: str,
        attribute_profile_version: str,
        parent_id: int | None = None,
        name_en: str | None = None,
        default_measure_type: MeasureType | None = None,
    ) -> TaxonomyCategory:
        if level < 0:
            raise ValueError("category level cannot be negative")
        normalized_code = code.strip().upper()
        statement = select(TaxonomyCategory).where(TaxonomyCategory.code == normalized_code)
        category, created = _get_or_create(
            self.session,
            statement,
            lambda: TaxonomyCategory(
                parent_id=parent_id,
                code=normalized_code,
                name_zh=name_zh.strip(),
                name_en=name_en.strip() if name_en else None,
                level=level,
                path=path.strip(),
                is_leaf=is_leaf,
                default_measure_type=(default_measure_type.value if default_measure_type else None),
                attribute_profile_code=attribute_profile_code.strip(),
                attribute_profile_version=attribute_profile_version.strip(),
                enabled=True,
            ),
        )
        if not created:
            expected_identity = (
                parent_id,
                level,
                path.strip(),
                is_leaf,
                default_measure_type.value if default_measure_type else None,
                attribute_profile_code.strip(),
                attribute_profile_version.strip(),
            )
            persisted_identity = (
                category.parent_id,
                category.level,
                category.path,
                category.is_leaf,
                category.default_measure_type,
                category.attribute_profile_code,
                category.attribute_profile_version,
            )
            if persisted_identity != expected_identity:
                raise CatalogIdentityConflictError(
                    f"category code {normalized_code!r} has incompatible identity or rule profile"
                )
        return category

    def get_or_create_item(
        self,
        *,
        category_id: int,
        canonical_key: str,
        item_type: ItemType,
        name: str,
        record_origin: RecordOrigin,
        brand_id: int | None = None,
        series_name: str | None = None,
        model_number: str | None = None,
        base_attributes: dict[str, Any] | None = None,
        status: ItemStatus = ItemStatus.ACTIVE,
    ) -> CatalogItem:
        key = canonical_key.strip()
        statement = select(CatalogItem).where(CatalogItem.canonical_key == key)
        item, created = _get_or_create(
            self.session,
            statement,
            lambda: CatalogItem(
                category_id=category_id,
                brand_id=brand_id,
                canonical_key=key,
                item_type=item_type.value,
                name=name.strip(),
                series_name=series_name.strip() if series_name else None,
                model_number=model_number.strip() if model_number else None,
                base_attributes=base_attributes or {},
                record_origin=record_origin.value,
                status=status.value,
            ),
        )
        if not created and (
            item.category_id != category_id
            or item.item_type != item_type.value
            or item.brand_id != brand_id
        ):
            raise CatalogIdentityConflictError(
                f"catalog item key {key!r} has incompatible identity"
            )
        return item

    def get_or_create_variant(
        self,
        *,
        catalog_item_id: int,
        variant_key: str,
        name: str,
        condition_code: ConditionCode,
        measure_type: MeasureType,
        base_unit: str,
        identity_fingerprint: str,
        quantity_value: Decimal | None = None,
        quantity_min: Decimal | None = None,
        quantity_max: Decimal | None = None,
        package_count: int | None = None,
        gtin: str | None = None,
        manufacturer_part_number: str | None = None,
        supersedes_variant_id: int | None = None,
        attributes: dict[str, Any] | None = None,
        status: ItemStatus = ItemStatus.ACTIVE,
    ) -> ItemVariant:
        if not base_unit.strip():
            raise ValueError("base_unit cannot be blank")
        if package_count is not None and package_count <= 0:
            raise ValueError("package_count must be greater than zero")
        for value in (quantity_value, quantity_min, quantity_max):
            if value is not None and value <= 0:
                raise ValueError("variant quantities must be greater than zero")
        if (quantity_min is None) != (quantity_max is None):
            raise ValueError("variant quantity range must provide both bounds")
        if quantity_value is not None and quantity_min is not None:
            raise ValueError("variant quantity cannot be both exact and a range")
        if quantity_min is not None and quantity_max is not None and quantity_max < quantity_min:
            raise ValueError("variant quantity maximum cannot be below minimum")

        fingerprint = _sha256_hex(identity_fingerprint, field_name="identity_fingerprint")
        key = variant_key.strip()
        statement = select(ItemVariant).where(
            ItemVariant.catalog_item_id == catalog_item_id,
            ItemVariant.variant_key == key,
        )
        variant, created = _get_or_create(
            self.session,
            statement,
            lambda: ItemVariant(
                catalog_item_id=catalog_item_id,
                supersedes_variant_id=supersedes_variant_id,
                variant_key=key,
                name=name.strip(),
                gtin=gtin.strip() if gtin else None,
                manufacturer_part_number=(
                    manufacturer_part_number.strip() if manufacturer_part_number else None
                ),
                condition_code=condition_code.value,
                measure_type=measure_type.value,
                quantity_value=quantity_value,
                quantity_min=quantity_min,
                quantity_max=quantity_max,
                base_unit=base_unit.strip().upper(),
                package_count=package_count,
                attributes=attributes or {},
                identity_fingerprint=fingerprint,
                status=status.value,
            ),
        )
        if not created and variant.identity_fingerprint != fingerprint:
            raise CatalogIdentityConflictError(
                f"variant key {key!r} resolves to a different identity fingerprint"
            )
        return variant

    def get_or_create_source_channel(
        self,
        *,
        code: str,
        name: str,
        source_type: SourceType,
        business_mode: BusinessMode,
        access_mode: AccessMode,
        base_url: str,
        allowed_domains: list[str],
        region_mode: RegionMode,
        connector_code: str,
        enabled: bool = True,
    ) -> SourceChannel:
        normalized_code = code.strip().upper()
        statement = select(SourceChannel).where(SourceChannel.code == normalized_code)
        channel, _ = _get_or_create(
            self.session,
            statement,
            lambda: SourceChannel(
                code=normalized_code,
                name=name.strip(),
                source_type=source_type.value,
                business_mode=business_mode.value,
                access_mode=access_mode.value,
                base_url=base_url.strip(),
                allowed_domains=sorted({domain.strip().lower() for domain in allowed_domains}),
                region_mode=region_mode.value,
                currency="CNY",
                connector_code=connector_code.strip(),
                enabled=enabled,
            ),
        )
        expected_identity = (
            source_type.value,
            business_mode.value,
            access_mode.value,
            region_mode.value,
            connector_code.strip(),
        )
        persisted_identity = (
            channel.source_type,
            channel.business_mode,
            channel.access_mode,
            channel.region_mode,
            channel.connector_code,
        )
        if persisted_identity != expected_identity:
            raise CatalogIdentityConflictError(
                f"source channel code {normalized_code!r} has incompatible identity"
            )
        return channel

    def get_or_create_merchant(
        self,
        *,
        source_channel_id: int,
        merchant_key_hash: str,
        name: str,
        seller_type: SellerType,
        verification_status: VerificationStatus,
        observed_at: datetime,
        external_merchant_id: str | None = None,
        status: LifecycleStatus = LifecycleStatus.ACTIVE,
        refresh_current: bool = True,
    ) -> Merchant:
        key_hash = _sha256_hex(merchant_key_hash, field_name="merchant_key_hash")
        observed = _utc_naive_milliseconds(observed_at)
        statement = select(Merchant).where(
            Merchant.source_channel_id == source_channel_id,
            Merchant.merchant_key_hash == key_hash,
        )
        merchant, created = _get_or_create(
            self.session,
            statement,
            lambda: Merchant(
                source_channel_id=source_channel_id,
                merchant_key_hash=key_hash,
                external_merchant_id=(
                    external_merchant_id.strip() if external_merchant_id else None
                ),
                name=name.strip(),
                seller_type=seller_type.value,
                verification_status=verification_status.value,
                status=status.value,
                first_seen_at=observed,
                last_seen_at=observed,
            ),
        )
        if not created and refresh_current and observed > merchant.last_seen_at:
            merchant.name = name.strip()
            merchant.external_merchant_id = (
                external_merchant_id.strip()
                if external_merchant_id
                else merchant.external_merchant_id
            )
            merchant.seller_type = seller_type.value
            merchant.verification_status = verification_status.value
            merchant.status = status.value
        if not created and refresh_current:
            merchant.first_seen_at = min(merchant.first_seen_at, observed)
            merchant.last_seen_at = max(merchant.last_seen_at, observed)
        return merchant

    def get_or_create_source_listing(
        self,
        *,
        source_channel_id: int,
        merchant_id: int,
        listing_key: str,
        listing_key_hash: str,
        price_nature: PriceNature,
        canonical_url: str,
        url_hash: str,
        observed_at: datetime,
        external_product_id: str | None = None,
        external_sku_id: str | None = None,
        lifecycle_status: LifecycleStatus = LifecycleStatus.ACTIVE,
        refresh_current: bool = True,
    ) -> SourceListing:
        merchant = self.session.get(Merchant, merchant_id)
        if merchant is None or merchant.source_channel_id != source_channel_id:
            raise CatalogConsistencyError("source listing merchant belongs to another channel")

        key_hash = _sha256_hex(listing_key_hash, field_name="listing_key_hash")
        normalized_url_hash = _sha256_hex(url_hash, field_name="url_hash")
        observed = _utc_naive_milliseconds(observed_at)
        statement = select(SourceListing).where(
            SourceListing.source_channel_id == source_channel_id,
            SourceListing.merchant_id == merchant_id,
            SourceListing.listing_key_hash == key_hash,
        )
        listing, created = _get_or_create(
            self.session,
            statement,
            lambda: SourceListing(
                source_channel_id=source_channel_id,
                merchant_id=merchant_id,
                listing_key=listing_key,
                listing_key_hash=key_hash,
                external_product_id=(external_product_id.strip() if external_product_id else None),
                external_sku_id=external_sku_id.strip() if external_sku_id else None,
                price_nature=price_nature.value,
                canonical_url=canonical_url.strip(),
                url_hash=normalized_url_hash,
                current_revision_id=None,
                lifecycle_status=lifecycle_status.value,
                consecutive_misses=0,
                first_seen_at=observed,
                last_seen_at=observed,
            ),
        )
        if not created:
            if listing.listing_key != listing_key or listing.price_nature != price_nature.value:
                raise CatalogIdentityConflictError(
                    "source listing key hash has incompatible identity"
                )
            for field, value in (
                ("external_product_id", external_product_id),
                ("external_sku_id", external_sku_id),
            ):
                existing_id = getattr(listing, field)
                if existing_id is not None and value is not None and existing_id != value.strip():
                    raise CatalogIdentityConflictError(
                        "source listing key resolves to a different external identity"
                    )
        if not created and refresh_current and observed > listing.last_seen_at:
            listing.external_product_id = (
                external_product_id.strip() if external_product_id else listing.external_product_id
            )
            listing.external_sku_id = (
                external_sku_id.strip() if external_sku_id else listing.external_sku_id
            )
            listing.canonical_url = canonical_url.strip()
            listing.url_hash = normalized_url_hash
            listing.lifecycle_status = lifecycle_status.value
            listing.consecutive_misses = 0
        if not created and refresh_current:
            listing.first_seen_at = min(listing.first_seen_at, observed)
            listing.last_seen_at = max(listing.last_seen_at, observed)
        return listing

    def get_or_create_listing_revision(
        self,
        *,
        source_listing_id: int,
        first_crawl_record_id: int,
        source_title: str,
        source_attributes: dict[str, Any],
        normalized_attributes: dict[str, Any],
        condition_code: ConditionCode,
        measure_type: MeasureType,
        identity_fingerprint: str,
        normalizer_version: str,
        quality_status: QualityStatus,
        observed_at: datetime,
        source_category_path: str | None = None,
        quantity_value: Decimal | None = None,
        quantity_min: Decimal | None = None,
        quantity_max: Decimal | None = None,
        quantity_unit: str | None = None,
        base_quantity_value: Decimal | None = None,
        base_quantity_min: Decimal | None = None,
        base_quantity_max: Decimal | None = None,
        base_unit: str | None = None,
        package_count: int | None = None,
        rejection_code: str | None = None,
    ) -> ListingRevision:
        observed = _utc_naive_milliseconds(observed_at)
        fingerprint = _sha256_hex(identity_fingerprint, field_name="identity_fingerprint")
        _validate_quantity(
            exact=quantity_value,
            minimum=quantity_min,
            maximum=quantity_max,
            unit=quantity_unit,
            label="source",
        )
        _validate_quantity(
            exact=base_quantity_value,
            minimum=base_quantity_min,
            maximum=base_quantity_max,
            unit=base_unit,
            label="base",
        )
        if package_count is not None and package_count <= 0:
            raise ValueError("package_count must be greater than zero")
        if quality_status is QualityStatus.ACCEPTED and rejection_code is not None:
            raise ValueError("accepted listing revisions cannot have rejection_code")
        if quality_status is not QualityStatus.ACCEPTED and rejection_code is None:
            raise ValueError("non-accepted listing revisions require rejection_code")

        listing = self.session.scalar(
            select(SourceListing).where(SourceListing.id == source_listing_id).with_for_update()
        )
        if listing is None:
            raise CatalogConsistencyError(f"unknown source listing id: {source_listing_id}")
        record_run = self.session.execute(
            select(CatalogCrawlRecord, CatalogCrawlRun)
            .join(CatalogCrawlRun, CatalogCrawlRun.id == CatalogCrawlRecord.crawl_run_id)
            .where(CatalogCrawlRecord.id == first_crawl_record_id)
        ).one_or_none()
        if record_run is None:
            raise CatalogConsistencyError(f"unknown crawl record id: {first_crawl_record_id}")
        record, run = record_run
        if record.source_listing_id not in {None, source_listing_id}:
            raise CatalogConsistencyError("listing revision evidence belongs to another listing")
        if run.source_channel_id != listing.source_channel_id:
            raise CatalogConsistencyError("listing revision evidence belongs to another channel")

        existing = self.session.scalar(
            select(ListingRevision).where(
                ListingRevision.source_listing_id == source_listing_id,
                ListingRevision.identity_fingerprint == fingerprint,
            )
        )
        identity = (
            condition_code.value,
            measure_type.value,
            _comparable_quantity_identity(
                exact=quantity_value,
                minimum=quantity_min,
                maximum=quantity_max,
                unit=quantity_unit.strip().upper() if quantity_unit else None,
                base_exact=base_quantity_value,
                base_minimum=base_quantity_min,
                base_maximum=base_quantity_max,
                base_unit=base_unit.strip().upper() if base_unit else None,
            ),
            package_count,
            normalized_attributes,
        )
        if existing is not None:
            persisted_identity = (
                existing.condition_code,
                existing.measure_type,
                _comparable_quantity_identity(
                    exact=existing.quantity_value,
                    minimum=existing.quantity_min,
                    maximum=existing.quantity_max,
                    unit=existing.quantity_unit,
                    base_exact=existing.base_quantity_value,
                    base_minimum=existing.base_quantity_min,
                    base_maximum=existing.base_quantity_max,
                    base_unit=existing.base_unit,
                ),
                existing.package_count,
                existing.normalized_attributes,
            )
            if persisted_identity != identity:
                raise CatalogIdentityConflictError(
                    "listing revision fingerprint resolves to incompatible normalized identity"
                )
            existing.last_observed_at = max(existing.last_observed_at, observed)
            return existing

        revision_no = (
            int(
                self.session.scalar(
                    select(func.max(ListingRevision.revision_no)).where(
                        ListingRevision.source_listing_id == source_listing_id
                    )
                )
                or 0
            )
            + 1
        )
        revision = ListingRevision(
            source_listing_id=source_listing_id,
            first_crawl_record_id=first_crawl_record_id,
            revision_no=revision_no,
            source_title=source_title.strip(),
            source_category_path=(source_category_path.strip() if source_category_path else None),
            source_attributes=source_attributes,
            normalized_attributes=normalized_attributes,
            condition_code=condition_code.value,
            measure_type=measure_type.value,
            quantity_value=quantity_value,
            quantity_min=quantity_min,
            quantity_max=quantity_max,
            quantity_unit=quantity_unit.strip().upper() if quantity_unit else None,
            base_quantity_value=base_quantity_value,
            base_quantity_min=base_quantity_min,
            base_quantity_max=base_quantity_max,
            base_unit=base_unit.strip().upper() if base_unit else None,
            package_count=package_count,
            identity_fingerprint=fingerprint,
            normalizer_version=normalizer_version.strip(),
            quality_status=quality_status.value,
            rejection_code=rejection_code,
            reviewed_by=None,
            reviewed_at=None,
            first_observed_at=observed,
            last_observed_at=observed,
        )
        self.session.add(revision)
        self.session.flush()
        return revision

    def set_current_revision(
        self,
        *,
        source_listing_id: int,
        revision_id: int,
        observed_at: datetime,
    ) -> SourceListing:
        listing = self.session.scalar(
            select(SourceListing).where(SourceListing.id == source_listing_id).with_for_update()
        )
        revision = self.session.get(ListingRevision, revision_id)
        if listing is None or revision is None or revision.source_listing_id != source_listing_id:
            raise CatalogConsistencyError("current revision must belong to the source listing")
        if revision.quality_status != QualityStatus.ACCEPTED.value:
            raise CatalogConsistencyError("current revision must have ACCEPTED identity quality")
        if listing.current_revision_id == revision_id:
            return listing
        if listing.current_revision_id is not None:
            current_revision = self.session.get(ListingRevision, listing.current_revision_id)
            current_time = self.session.scalar(
                select(func.max(CatalogPriceObservationRecord.observed_at)).where(
                    CatalogPriceObservationRecord.listing_revision_id
                    == listing.current_revision_id,
                    CatalogPriceObservationRecord.quality_status == QualityStatus.ACCEPTED.value,
                )
            )
            if current_time is None and current_revision is not None:
                current_time = current_revision.first_observed_at
            # last_observed_at includes rejected/review evidence. It must not
            # make an untrusted late quote decide which configuration is current.
            observed = _utc_naive_milliseconds(observed_at)
            if current_time is not None and observed < current_time:
                return listing
            if current_time is not None and observed == current_time:
                raise CatalogIdentityConflictError(
                    "different specifications share one trusted time"
                )
        self.session.execute(
            delete(CatalogPriceCurrent).where(
                CatalogPriceCurrent.source_listing_id == source_listing_id
            )
        )
        listing.current_revision_id = revision_id
        self.session.flush()
        return listing

    def create_match(
        self,
        *,
        listing_revision_id: int,
        item_variant_id: int,
        match_status: MatchStatus,
        match_method: MatchMethod,
        confidence: Decimal,
        matcher_version: str,
        effective_from: datetime,
        matched_fields: dict[str, Any] | None = None,
        mismatch_fields: dict[str, Any] | None = None,
        reviewed_by: str | None = None,
        reviewed_at: datetime | None = None,
    ) -> ListingMatch:
        if confidence < 0 or confidence > 1:
            raise ValueError("confidence must be between zero and one")
        effective = _utc_naive_milliseconds(effective_from)
        revision = self.session.scalar(
            select(ListingRevision)
            .where(ListingRevision.id == listing_revision_id)
            .with_for_update()
        )
        if revision is None or self.session.get(ItemVariant, item_variant_id) is None:
            raise CatalogConsistencyError("listing revision and item variant must exist")

        existing = self.session.scalar(
            select(ListingMatch).where(
                ListingMatch.listing_revision_id == listing_revision_id,
                ListingMatch.item_variant_id == item_variant_id,
                ListingMatch.matcher_version == matcher_version,
            )
        )
        if existing is not None:
            expected = (
                match_status.value,
                match_method.value,
                confidence,
                matched_fields or {},
                mismatch_fields or {},
                effective,
            )
            persisted = (
                existing.match_status,
                existing.match_method,
                existing.confidence,
                existing.matched_fields,
                existing.mismatch_fields,
                existing.effective_from,
            )
            if persisted != expected:
                raise CatalogIdentityConflictError(
                    "matcher version returned incompatible data for the same match key"
                )
            return existing

        if match_status is MatchStatus.ACCEPTED:
            open_matches = self.session.scalars(
                select(ListingMatch)
                .where(
                    ListingMatch.listing_revision_id == listing_revision_id,
                    ListingMatch.match_status == MatchStatus.ACCEPTED.value,
                    ListingMatch.effective_to.is_(None),
                )
                .with_for_update()
            ).all()
            for open_match in open_matches:
                if effective < open_match.effective_from:
                    raise CatalogConsistencyError("accepted match cannot move backwards in time")
                open_match.effective_to = effective

        match = ListingMatch(
            listing_revision_id=listing_revision_id,
            item_variant_id=item_variant_id,
            match_status=match_status.value,
            match_method=match_method.value,
            confidence=confidence,
            matched_fields=matched_fields or {},
            mismatch_fields=mismatch_fields or {},
            matcher_version=matcher_version.strip(),
            effective_from=effective,
            effective_to=None,
            reviewed_by=reviewed_by,
            reviewed_at=(_utc_naive_milliseconds(reviewed_at) if reviewed_at is not None else None),
        )
        self.session.add(match)
        self.session.flush()
        return match


class CatalogCrawlRepository:
    def __init__(self, session: Session) -> None:
        self.session = session

    def start_run(
        self,
        *,
        source_channel_id: int,
        region_scope: RegionScope,
        region_code: str,
        category_scope: list[str],
        run_type: RunType,
        trigger_type: CollectionTriggerType,
        adapter_version: str,
        policy_version: str,
        started_at: datetime,
    ) -> CatalogCrawlRun:
        normalized_region = region_code.strip().upper()
        if region_scope is RegionScope.NATIONAL and normalized_region != "CN":
            raise ValueError("NATIONAL crawl runs must use region_code CN")
        if region_scope is RegionScope.MULTI and normalized_region != "*":
            raise ValueError("MULTI crawl runs must use region_code *")
        run = CatalogCrawlRun(
            source_channel_id=source_channel_id,
            region_scope=region_scope.value,
            region_code=normalized_region,
            category_scope=category_scope,
            run_type=run_type.value,
            trigger_type=trigger_type.value,
            status=RunStatus.RUNNING.value,
            adapter_version=adapter_version.strip(),
            policy_version=policy_version.strip(),
            started_at=_utc_naive_milliseconds(started_at),
            finished_at=None,
            discovered_count=0,
            fetched_count=0,
            accepted_count=0,
            review_count=0,
            rejected_count=0,
            failed_count=0,
            error_summary=None,
        )
        self.session.add(run)
        self.session.flush()
        return run

    def add_record(
        self,
        *,
        crawl_run_id: int,
        entity_type: CatalogEntityType,
        entity_key: str,
        request_url: str,
        final_url: str,
        fetch_method: CollectionFetchMethod,
        fetch_status: OperationStatus,
        parse_status: OperationStatus,
        validation_status: OperationStatus,
        duration_ms: int,
        fetched_at: datetime,
        source_listing_id: int | None = None,
        replayed_from_record_id: int | None = None,
        http_status: int | None = None,
        error_code: str | None = None,
        error_message: str | None = None,
        raw_hash: str | None = None,
        raw_path: str | None = None,
        artifact_manifest: list[dict[str, Any]] | None = None,
        content_type: str | None = None,
        raw_size_bytes: int | None = None,
    ) -> CatalogCrawlRecord:
        if duration_ms < 0 or (raw_size_bytes is not None and raw_size_bytes < 0):
            raise ValueError("crawl record sizes and duration cannot be negative")
        if http_status is not None and not 100 <= http_status <= 599:
            raise ValueError("http_status must be between 100 and 599")
        record = CatalogCrawlRecord(
            crawl_run_id=crawl_run_id,
            source_listing_id=source_listing_id,
            replayed_from_record_id=replayed_from_record_id,
            entity_type=entity_type.value,
            entity_key=entity_key.strip(),
            request_url=request_url.strip(),
            final_url=final_url.strip(),
            fetch_method=fetch_method.value,
            http_status=http_status,
            fetch_status=fetch_status.value,
            parse_status=parse_status.value,
            validation_status=validation_status.value,
            error_code=error_code,
            error_message=error_message,
            raw_hash=_sha256_hex(raw_hash, field_name="raw_hash") if raw_hash else None,
            raw_path=raw_path,
            artifact_manifest=artifact_manifest or [],
            content_type=content_type,
            raw_size_bytes=raw_size_bytes,
            duration_ms=duration_ms,
            fetched_at=_utc_naive_milliseconds(fetched_at),
        )
        self.session.add(record)
        self.session.flush()
        return record

    def finish_run(
        self,
        *,
        crawl_run_id: int,
        status: RunStatus,
        finished_at: datetime,
        discovered_count: int,
        fetched_count: int,
        accepted_count: int,
        review_count: int,
        rejected_count: int,
        failed_count: int,
        error_summary: dict[str, int] | None = None,
    ) -> CatalogCrawlRun:
        if status is RunStatus.RUNNING:
            raise ValueError("a finished crawl run cannot remain RUNNING")
        counts = (
            discovered_count,
            fetched_count,
            accepted_count,
            review_count,
            rejected_count,
            failed_count,
        )
        if any(value < 0 for value in counts):
            raise ValueError("crawl run counts cannot be negative")
        finished = _utc_naive_milliseconds(finished_at)
        run = self.session.scalar(
            select(CatalogCrawlRun).where(CatalogCrawlRun.id == crawl_run_id).with_for_update()
        )
        if run is None:
            raise CatalogConsistencyError(f"unknown crawl run id: {crawl_run_id}")
        if finished < run.started_at:
            raise ValueError("finished_at cannot be earlier than started_at")
        run.status = status.value
        run.finished_at = finished
        run.discovered_count = discovered_count
        run.fetched_count = fetched_count
        run.accepted_count = accepted_count
        run.review_count = review_count
        run.rejected_count = rejected_count
        run.failed_count = failed_count
        run.error_summary = error_summary
        self.session.flush()
        return run


class CatalogPriceRepository:
    def __init__(self, session: Session) -> None:
        self.session = session

    def record(self, observation: CatalogPriceObservation) -> CatalogPriceWriteResult:
        listing = self.session.scalar(
            select(SourceListing)
            .where(SourceListing.id == observation.source_listing_id)
            .with_for_update()
        )
        if listing is None:
            raise CatalogConsistencyError(
                f"unknown source listing id: {observation.source_listing_id}"
            )
        revision = self.session.get(ListingRevision, observation.listing_revision_id)
        if (
            revision is None
            or revision.source_listing_id != listing.id
            or revision.quality_status != QualityStatus.ACCEPTED.value
        ):
            raise CatalogConsistencyError(
                "price observation requires an accepted revision belonging to the listing"
            )
        if listing.price_nature != observation.price_nature.value:
            raise CatalogConsistencyError("price nature differs from source listing")

        record_run = self.session.execute(
            select(CatalogCrawlRecord, CatalogCrawlRun)
            .join(CatalogCrawlRun, CatalogCrawlRun.id == CatalogCrawlRecord.crawl_run_id)
            .where(CatalogCrawlRecord.id == observation.crawl_record_id)
        ).one_or_none()
        if record_run is None:
            raise CatalogConsistencyError(f"unknown crawl record id: {observation.crawl_record_id}")
        record, run = record_run
        if record.source_listing_id not in {None, listing.id}:
            raise CatalogConsistencyError("price evidence belongs to another listing")
        if run.source_channel_id != listing.source_channel_id:
            raise CatalogConsistencyError("price evidence belongs to another source channel")
        if run.region_scope != RegionScope.MULTI.value and (
            run.region_scope != observation.region_scope.value
            or run.region_code != observation.region_code
        ):
            raise CatalogConsistencyError("price observation region differs from crawl run")

        if observation.supersedes_observation_id is not None:
            prior = self.session.get(
                CatalogPriceObservationRecord,
                observation.supersedes_observation_id,
            )
            if prior is None or self._correction_identity(prior) != self._command_identity(
                observation
            ):
                raise CatalogCorrectionError(
                    "corrected observation must share source, revision, region, and time"
                )

        observation_key = observation.build_observation_key(
            adapter_version=run.adapter_version,
            policy_version=run.policy_version,
        )
        persisted = self.session.scalar(
            select(CatalogPriceObservationRecord).where(
                CatalogPriceObservationRecord.observation_key == observation_key
            )
        )
        if persisted is not None:
            if self._record_payload(persisted) != self._command_payload(observation):
                raise CatalogIdentityConflictError(
                    "observation key resolves to a different payload"
                )
            current = self._current_for(observation)
            return CatalogPriceWriteResult(
                observation_id=persisted.id,
                current_id=current.id if current is not None else None,
                observation_created=False,
                current_advanced=False,
            )

        persisted = CatalogPriceObservationRecord(
            observation_key=observation_key,
            supersedes_observation_id=observation.supersedes_observation_id,
            source_listing_id=observation.source_listing_id,
            listing_revision_id=observation.listing_revision_id,
            crawl_record_id=observation.crawl_record_id,
            region_scope=observation.region_scope.value,
            region_code=observation.region_code,
            currency=observation.currency,
            original_price=observation.original_price,
            original_price_type=observation.original_price_type.value,
            current_price=observation.current_price,
            price_nature=observation.price_nature.value,
            price_type=observation.price_type.value,
            pricing_basis=observation.pricing_basis.value,
            promotion_label=observation.promotion_label,
            availability=observation.availability.value,
            unit_price=observation.unit_price,
            unit_price_unit=observation.unit_price_unit,
            fee_status=observation.fee_status.value,
            quality_status=observation.quality_status.value,
            rejection_code=observation.rejection_code,
            reviewed_by=None,
            reviewed_at=None,
            displayed_price_text=observation.displayed_price_text,
            source_hash=observation.source_hash,
            observed_at=observation.observed_at,
        )
        self.session.add(persisted)
        self.session.flush()

        if not observation.eligible_for_current or listing.current_revision_id != revision.id:
            current = self._current_for(observation)
            return CatalogPriceWriteResult(
                persisted.id,
                current.id if current is not None else None,
                True,
                False,
            )

        current = self._current_for(observation, lock=True)
        if current is None:
            current = CatalogPriceCurrent(
                source_listing_id=listing.id,
                listing_revision_id=revision.id,
                region_scope=observation.region_scope.value,
                region_code=observation.region_code,
                price_observation_id=persisted.id,
                observed_at=observation.observed_at,
            )
            self.session.add(current)
            self.session.flush()
            return CatalogPriceWriteResult(persisted.id, current.id, True, True)

        if current.listing_revision_id != listing.current_revision_id:
            raise CatalogConsistencyError("current price points to a stale listing revision")
        advances = observation.observed_at > current.observed_at or (
            observation.observed_at == current.observed_at
            and observation.supersedes_observation_id == current.price_observation_id
        )
        if advances:
            current.listing_revision_id = revision.id
            current.price_observation_id = persisted.id
            current.observed_at = observation.observed_at
            self.session.flush()
        return CatalogPriceWriteResult(persisted.id, current.id, True, advances)

    def assert_same_time_consistent(self, observation: CatalogPriceObservation) -> None:
        """Device fetches cannot silently revise a different quote at the same instant."""
        prior_rows = self.session.scalars(
            select(CatalogPriceObservationRecord)
            .where(
                CatalogPriceObservationRecord.source_listing_id == observation.source_listing_id,
                CatalogPriceObservationRecord.region_scope == observation.region_scope.value,
                CatalogPriceObservationRecord.region_code == observation.region_code,
                CatalogPriceObservationRecord.observed_at == observation.observed_at,
                CatalogPriceObservationRecord.quality_status == QualityStatus.ACCEPTED.value,
            )
            .with_for_update()
        ).all()
        run = self.session.scalar(
            select(CatalogCrawlRun)
            .join(CatalogCrawlRecord, CatalogCrawlRecord.crawl_run_id == CatalogCrawlRun.id)
            .where(CatalogCrawlRecord.id == observation.crawl_record_id)
        )
        if run is None:
            raise CatalogConsistencyError("device observation requires persisted run evidence")
        key = observation.build_observation_key(
            adapter_version=run.adapter_version,
            policy_version=run.policy_version,
        )
        # Only exact replays reuse the point. Even equal amounts from different
        # evidence cannot create two unlinked accepted rows that rebuild may swap.
        if any(prior.observation_key != key for prior in prior_rows):
            raise CatalogIdentityConflictError(
                "conflicting device quotes at the same observation time"
            )

    def resolve_same_time_correction(
        self,
        observation: CatalogPriceObservation,
    ) -> CatalogPriceObservation:
        """Link a changed same-time publication while keeping exact replays idempotent."""

        if observation.supersedes_observation_id is not None:
            return observation
        prior_rows = self.session.scalars(
            select(CatalogPriceObservationRecord)
            .where(
                CatalogPriceObservationRecord.source_listing_id == observation.source_listing_id,
                CatalogPriceObservationRecord.listing_revision_id
                == observation.listing_revision_id,
                CatalogPriceObservationRecord.region_scope == observation.region_scope.value,
                CatalogPriceObservationRecord.region_code == observation.region_code,
                CatalogPriceObservationRecord.observed_at == observation.observed_at,
                CatalogPriceObservationRecord.quality_status == QualityStatus.ACCEPTED.value,
            )
            .order_by(CatalogPriceObservationRecord.id.desc())
            .with_for_update()
        ).all()
        if not prior_rows:
            return observation

        command_payload = self._command_payload(observation)[1:]
        for prior in prior_rows:
            if (
                prior.source_hash == observation.source_hash
                and self._record_payload(prior)[1:] == command_payload
            ):
                return observation.model_copy(
                    update={
                        "supersedes_observation_id": prior.supersedes_observation_id,
                    }
                )

        superseded_ids = {
            prior.supersedes_observation_id
            for prior in prior_rows
            if prior.supersedes_observation_id is not None
        }
        active_rows = [prior for prior in prior_rows if prior.id not in superseded_ids]
        if len(active_rows) != 1:
            raise CatalogCorrectionError("same-time publication has an ambiguous correction chain")
        return observation.model_copy(update={"supersedes_observation_id": active_rows[0].id})

    def rebuild_current(self, *, source_listing_id: int) -> int:
        listing = self.session.scalar(
            select(SourceListing).where(SourceListing.id == source_listing_id).with_for_update()
        )
        if listing is None:
            raise CatalogConsistencyError(f"unknown source listing id: {source_listing_id}")
        self.session.execute(
            delete(CatalogPriceCurrent).where(
                CatalogPriceCurrent.source_listing_id == source_listing_id
            )
        )
        if listing.current_revision_id is None:
            return 0

        observations = self.session.scalars(
            select(CatalogPriceObservationRecord)
            .where(
                CatalogPriceObservationRecord.source_listing_id == source_listing_id,
                CatalogPriceObservationRecord.listing_revision_id == listing.current_revision_id,
                CatalogPriceObservationRecord.quality_status == QualityStatus.ACCEPTED.value,
            )
            .order_by(
                CatalogPriceObservationRecord.observed_at.desc(),
                CatalogPriceObservationRecord.id.desc(),
            )
        ).all()
        superseded_ids = {
            item.supersedes_observation_id
            for item in observations
            if item.supersedes_observation_id is not None
        }
        selected_regions: set[tuple[str, str]] = set()
        for item in observations:
            region = (item.region_scope, item.region_code)
            if item.id in superseded_ids or region in selected_regions:
                continue
            selected_regions.add(region)
            self.session.add(
                CatalogPriceCurrent(
                    source_listing_id=source_listing_id,
                    listing_revision_id=item.listing_revision_id,
                    region_scope=item.region_scope,
                    region_code=item.region_code,
                    price_observation_id=item.id,
                    observed_at=item.observed_at,
                )
            )
        self.session.flush()
        return len(selected_regions)

    def _current_for(
        self,
        observation: CatalogPriceObservation,
        *,
        lock: bool = False,
    ) -> CatalogPriceCurrent | None:
        statement = select(CatalogPriceCurrent).where(
            CatalogPriceCurrent.source_listing_id == observation.source_listing_id,
            CatalogPriceCurrent.region_scope == observation.region_scope.value,
            CatalogPriceCurrent.region_code == observation.region_code,
        )
        if lock:
            statement = statement.with_for_update()
        return self.session.scalar(statement)

    @staticmethod
    def _correction_identity(record: CatalogPriceObservationRecord) -> tuple[object, ...]:
        return (
            record.source_listing_id,
            record.listing_revision_id,
            record.region_scope,
            record.region_code,
            record.observed_at,
        )

    @staticmethod
    def _command_identity(observation: CatalogPriceObservation) -> tuple[object, ...]:
        return (
            observation.source_listing_id,
            observation.listing_revision_id,
            observation.region_scope.value,
            observation.region_code,
            observation.observed_at,
        )

    @staticmethod
    def _record_payload(record: CatalogPriceObservationRecord) -> tuple[object, ...]:
        return (
            record.supersedes_observation_id,
            record.currency,
            record.original_price,
            record.original_price_type,
            record.current_price,
            record.price_nature,
            record.price_type,
            record.pricing_basis,
            record.promotion_label,
            record.availability,
            record.unit_price,
            record.unit_price_unit,
            record.fee_status,
            record.quality_status,
            record.rejection_code,
            record.displayed_price_text,
        )

    @staticmethod
    def _command_payload(observation: CatalogPriceObservation) -> tuple[object, ...]:
        return (
            observation.supersedes_observation_id,
            observation.currency,
            observation.original_price,
            observation.original_price_type.value,
            observation.current_price,
            observation.price_nature.value,
            observation.price_type.value,
            observation.pricing_basis.value,
            observation.promotion_label,
            observation.availability.value,
            observation.unit_price,
            observation.unit_price_unit,
            observation.fee_status.value,
            observation.quality_status.value,
            observation.rejection_code,
            observation.displayed_price_text,
        )
