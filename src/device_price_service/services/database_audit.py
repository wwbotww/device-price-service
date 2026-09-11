from __future__ import annotations

from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Any

from sqlalchemy import CheckConstraint, Connection, select, text

from device_price_service.db.catalog_models import (
    CatalogBrand,
    CatalogCrawlRecord,
    CatalogItem,
    CatalogPriceObservationRecord,
    ItemVariant,
    ListingMatch,
    ListingRevision,
    SourceChannel,
    SourceListing,
    TaxonomyCategory,
)
from device_price_service.normalization.devices import device_item_key
from device_price_service.services.artifact_store import ArtifactError, RawArtifactStore


@dataclass(frozen=True, slots=True)
class DatabaseAuditReport:
    critical: dict[str, int]
    warnings: dict[str, int]

    @property
    def healthy(self) -> bool:
        return not any(self.critical.values())


def audit_database(
    connection: Connection,
    *,
    stale_run_minutes: int,
    stale_price_hours: int,
    artifact_store: RawArtifactStore | None = None,
) -> DatabaseAuditReport:
    """Read V2 facts only; never repair pointers, finish runs, or access a source.

    Warnings describe operational freshness, not data corruption. Raw files may
    live on another machine, so filesystem verification is explicitly optional.
    """
    if stale_run_minutes <= 0 or stale_price_hours <= 0:
        raise ValueError("audit stale thresholds must be positive")
    existing_tables = set(
        connection.scalars(
            text("SELECT table_name FROM information_schema.tables WHERE table_schema = DATABASE()")
        )
    )
    expected_tables = set(CatalogPriceObservationRecord.metadata.tables)
    missing = expected_tables - existing_tables
    if missing:
        return DatabaseAuditReport(critical={"missing_v2_tables": len(missing)}, warnings={})
    critical = {name: _scalar(connection, sql) for name, sql in _CONSISTENCY_QUERIES.items()}
    # Reuse the authoritative constraints, including the narrow no-price state
    # branch, rather than maintaining a second eligibility policy here.
    constraints = [
        f"({constraint.sqltext})"
        for constraint in CatalogPriceObservationRecord.metadata.tables[
            CatalogPriceObservationRecord.__tablename__
        ].constraints
        if isinstance(constraint, CheckConstraint)
    ]
    critical["invalid_observation_semantics"] = _scalar(
        connection,
        "SELECT COUNT(*) FROM v2_price_observation WHERE NOT ("
        + " AND ".join(constraints)
        + ") OR (quality_status = 'ACCEPTED' AND price_nature = 'RETAIL_OFFER' "
        "AND original_price < current_price)",
    )
    critical["device_standard_identity_mismatch"] = _device_identity_errors(connection)
    critical["invalid_artifact_reference"] = _artifact_errors(connection, artifact_store)
    warnings = {
        "channels_without_completed_run": _scalar(
            connection,
            """
            SELECT COUNT(*) FROM v2_source_channel sc
            WHERE sc.enabled = 1 AND NOT EXISTS (
                SELECT 1 FROM v2_crawl_run cr WHERE cr.source_channel_id = sc.id
                AND cr.status IN ('SUCCEEDED', 'PARTIAL') AND cr.accepted_count > 0
            )
            """,
        ),
        "stale_running_crawl": _scalar(
            connection,
            """
            SELECT COUNT(*) FROM v2_crawl_run
            WHERE status = 'RUNNING'
              AND started_at < UTC_TIMESTAMP(3) - INTERVAL :age MINUTE
            """,
            {"age": stale_run_minutes},
        ),
        "stale_current_price": _scalar(
            connection,
            """
            SELECT COUNT(*) FROM v2_price_current pc
            JOIN v2_source_listing sl ON sl.id = pc.source_listing_id
            JOIN v2_source_channel sc ON sc.id = sl.source_channel_id
            WHERE sc.enabled = 1 AND pc.observed_at < UTC_TIMESTAMP(3) - INTERVAL :age HOUR
            """,
            {"age": stale_price_hours},
        ),
        "recent_unconfirmed_price_changes": _scalar(
            connection,
            """
            SELECT COUNT(*) FROM v2_price_observation
            WHERE rejection_code = 'PRICE_CHANGE_UNCONFIRMED'
              AND observed_at >= UTC_TIMESTAMP(3) - INTERVAL 24 HOUR
            """,
        ),
        "listings_waiting_off_shelf_confirmation": _scalar(
            connection,
            """
            SELECT COUNT(*) FROM v2_source_listing sl
            JOIN v2_source_channel sc ON sc.id = sl.source_channel_id
            WHERE sc.enabled = 1 AND sc.source_type = 'OFFICIAL_MALL'
              AND sl.consecutive_misses > 0 AND sl.lifecycle_status <> 'INACTIVE'
            """,
        ),
    }
    return DatabaseAuditReport(critical=critical, warnings=warnings)


_CONSISTENCY_QUERIES = {
    "current_pointer_mismatch": """
        SELECT COUNT(*) FROM v2_price_current pc
        LEFT JOIN v2_price_observation po ON po.id = pc.price_observation_id
        LEFT JOIN v2_source_listing sl ON sl.id = pc.source_listing_id
        LEFT JOIN v2_listing_revision lr ON lr.id = pc.listing_revision_id
        WHERE po.id IS NULL OR sl.id IS NULL OR lr.id IS NULL
           OR po.quality_status <> 'ACCEPTED' OR lr.quality_status <> 'ACCEPTED'
           OR po.source_listing_id <> pc.source_listing_id
           OR lr.source_listing_id <> pc.source_listing_id
           OR po.listing_revision_id <> pc.listing_revision_id
           OR NOT (sl.current_revision_id <=> pc.listing_revision_id)
           OR po.region_scope <> pc.region_scope OR po.region_code <> pc.region_code
           OR po.observed_at <> pc.observed_at
    """,
    "missing_current_pointer": """
        SELECT COUNT(*) FROM (
            SELECT po.source_listing_id, po.region_scope, po.region_code
            FROM v2_price_observation po
            JOIN v2_source_listing sl ON sl.id = po.source_listing_id
              AND sl.current_revision_id = po.listing_revision_id
            LEFT JOIN v2_price_current pc ON pc.source_listing_id = po.source_listing_id
              AND pc.region_scope = po.region_scope AND pc.region_code = po.region_code
            WHERE po.quality_status = 'ACCEPTED' AND pc.id IS NULL
            GROUP BY po.source_listing_id, po.region_scope, po.region_code
        ) missing
    """,
    "outdated_current_pointer": """
        SELECT COUNT(*) FROM v2_price_current pc WHERE EXISTS (
            SELECT 1 FROM v2_price_observation po
            WHERE po.source_listing_id = pc.source_listing_id
              AND po.listing_revision_id = pc.listing_revision_id
              AND po.region_scope = pc.region_scope AND po.region_code = pc.region_code
              AND po.quality_status = 'ACCEPTED'
              AND (po.observed_at > pc.observed_at
                   OR po.supersedes_observation_id = pc.price_observation_id)
        )
    """,
    "invalid_current_revision": """
        SELECT COUNT(*) FROM v2_source_listing sl
        LEFT JOIN v2_listing_revision lr ON lr.id = sl.current_revision_id
        WHERE sl.current_revision_id IS NOT NULL AND (
            lr.id IS NULL OR lr.source_listing_id <> sl.id OR lr.quality_status <> 'ACCEPTED'
        )
    """,
    "outdated_current_revision": """
        SELECT COUNT(*) FROM v2_source_listing sl WHERE EXISTS (
            SELECT 1 FROM v2_price_observation newer
            WHERE newer.source_listing_id = sl.id AND newer.quality_status = 'ACCEPTED'
              AND (sl.current_revision_id IS NULL OR (
                  newer.listing_revision_id <> sl.current_revision_id AND NOT EXISTS (
                      SELECT 1 FROM v2_price_observation current_fact
                      WHERE current_fact.source_listing_id = sl.id
                        AND current_fact.listing_revision_id = sl.current_revision_id
                        AND current_fact.quality_status = 'ACCEPTED'
                        AND current_fact.observed_at >= newer.observed_at
                  )
              ))
        )
    """,
    "observation_identity_mismatch": """
        SELECT COUNT(*) FROM v2_price_observation po
        LEFT JOIN v2_listing_revision lr ON lr.id = po.listing_revision_id
        LEFT JOIN v2_source_listing sl ON sl.id = po.source_listing_id
        LEFT JOIN v2_crawl_record cr ON cr.id = po.crawl_record_id
        LEFT JOIN v2_crawl_run run ON run.id = cr.crawl_run_id
        WHERE lr.id IS NULL OR sl.id IS NULL OR cr.id IS NULL OR run.id IS NULL
           OR lr.source_listing_id <> sl.id OR lr.quality_status <> 'ACCEPTED'
           OR po.price_nature <> sl.price_nature
           OR (cr.source_listing_id IS NOT NULL AND cr.source_listing_id <> sl.id)
           OR run.source_channel_id <> sl.source_channel_id
           OR (run.region_scope <> 'MULTI' AND (
               run.region_scope <> po.region_scope OR run.region_code <> po.region_code))
    """,
    "revision_evidence_mismatch": """
        SELECT COUNT(*) FROM v2_listing_revision lr
        LEFT JOIN v2_source_listing sl ON sl.id = lr.source_listing_id
        LEFT JOIN v2_crawl_record cr ON cr.id = lr.first_crawl_record_id
        LEFT JOIN v2_crawl_run run ON run.id = cr.crawl_run_id
        WHERE sl.id IS NULL OR cr.id IS NULL OR run.id IS NULL
           OR run.source_channel_id <> sl.source_channel_id
           OR (cr.source_listing_id IS NOT NULL AND cr.source_listing_id <> sl.id)
    """,
    "untraceable_accepted_observation": """
        SELECT COUNT(*) FROM v2_price_observation po
        LEFT JOIN v2_crawl_record cr ON cr.id = po.crawl_record_id
        WHERE po.quality_status = 'ACCEPTED' AND (
            cr.id IS NULL OR cr.fetch_status <> 'SUCCEEDED'
            OR (cr.parse_status <> 'SUCCEEDED'
                AND NOT (cr.error_code <=> 'OFF_SHELF_CONFIRMED'))
            OR cr.raw_hash IS NULL OR cr.raw_path IS NULL
            OR (cr.entity_type = 'PRODUCT' AND (
                po.source_hash <> cr.raw_hash OR po.observed_at <> cr.fetched_at))
        )
    """,
    "invalid_same_time_correction": """
        SELECT COUNT(*) FROM v2_price_observation po
        LEFT JOIN v2_price_observation prior ON prior.id = po.supersedes_observation_id
        LEFT JOIN v2_crawl_record cr ON cr.id = po.crawl_record_id
        WHERE po.supersedes_observation_id IS NOT NULL AND (
            prior.id IS NULL OR prior.id >= po.id OR cr.entity_type = 'PRODUCT'
            OR prior.source_listing_id <> po.source_listing_id
            OR prior.listing_revision_id <> po.listing_revision_id
            OR prior.region_scope <> po.region_scope OR prior.region_code <> po.region_code
            OR prior.observed_at <> po.observed_at
            OR prior.quality_status <> 'ACCEPTED' OR po.quality_status <> 'ACCEPTED'
        )
    """,
    "ambiguous_same_time_observations": """
        SELECT COUNT(*) FROM (
            SELECT po.source_listing_id, po.region_scope, po.region_code, po.observed_at
            FROM v2_price_observation po
            WHERE po.quality_status = 'ACCEPTED' AND NOT EXISTS (
                SELECT 1 FROM v2_price_observation replacement
                WHERE replacement.supersedes_observation_id = po.id
                  AND replacement.quality_status = 'ACCEPTED'
            )
            GROUP BY po.source_listing_id, po.region_scope, po.region_code, po.observed_at
            HAVING COUNT(*) > 1
        ) ambiguous
    """,
    "overlapping_accepted_matches": """
        SELECT COUNT(*) FROM v2_listing_match a JOIN v2_listing_match b
          ON a.listing_revision_id = b.listing_revision_id AND a.id < b.id
        WHERE a.match_status = 'ACCEPTED' AND b.match_status = 'ACCEPTED'
          AND a.effective_from < COALESCE(b.effective_to, '9999-12-31')
          AND b.effective_from < COALESCE(a.effective_to, '9999-12-31')
    """,
    "device_match_cardinality": """
        SELECT COUNT(*) FROM (
            SELECT sl.id FROM v2_source_listing sl
            JOIN v2_source_channel sc ON sc.id = sl.source_channel_id
            LEFT JOIN v2_listing_match lm ON lm.listing_revision_id = sl.current_revision_id
              AND lm.match_status = 'ACCEPTED' AND lm.effective_to IS NULL
            WHERE sc.source_type = 'OFFICIAL_MALL' AND sl.current_revision_id IS NOT NULL
            GROUP BY sl.id HAVING COUNT(lm.id) <> 1
        ) unmatched
    """,
    "invalid_official_scope": """
        SELECT COUNT(*) FROM v2_source_listing sl
        JOIN v2_source_channel sc ON sc.id = sl.source_channel_id
        JOIN v2_merchant m ON m.id = sl.merchant_id
        WHERE m.source_channel_id <> sl.source_channel_id OR (
            sc.source_type = 'OFFICIAL_MALL' AND (
                sc.business_mode <> 'SELF_OPERATED' OR sc.region_mode <> 'NATIONAL'
                OR sc.currency <> 'CNY' OR sl.price_nature <> 'RETAIL_OFFER'
                OR m.seller_type <> 'BRAND_OFFICIAL' OR m.verification_status <> 'VERIFIED'
            )
        )
    """,
    "invalid_batch_terminal_state": """
        SELECT COUNT(*) FROM v2_crawl_run
        WHERE (status = 'RUNNING' AND finished_at IS NOT NULL)
           OR (status <> 'RUNNING' AND finished_at IS NULL)
           OR (status IN ('SUCCEEDED','PARTIAL') AND accepted_count = 0)
           OR (status = 'SUCCEEDED' AND failed_count > 0)
    """,
}


def _device_identity_errors(connection: Connection) -> int:
    statement = (
        select(
            ItemVariant.__table__,
            ListingRevision.identity_fingerprint.label("revision_fingerprint"),
            ListingRevision.normalized_attributes,
            CatalogItem.canonical_key,
            CatalogItem.item_type,
            CatalogBrand.code.label("brand_code"),
            SourceChannel.code.label("channel_code"),
            SourceListing.external_product_id,
            TaxonomyCategory.attribute_profile_code,
        )
        .select_from(SourceListing)
        .join(SourceChannel, SourceChannel.id == SourceListing.source_channel_id)
        .join(ListingRevision, ListingRevision.id == SourceListing.current_revision_id)
        .join(ListingMatch, ListingMatch.listing_revision_id == ListingRevision.id)
        .join(ItemVariant, ItemVariant.id == ListingMatch.item_variant_id)
        .join(CatalogItem, CatalogItem.id == ItemVariant.catalog_item_id)
        .outerjoin(CatalogBrand, CatalogBrand.id == CatalogItem.brand_id)
        .join(TaxonomyCategory, TaxonomyCategory.id == CatalogItem.category_id)
        .where(
            SourceChannel.source_type == "OFFICIAL_MALL",
            ListingMatch.match_status == "ACCEPTED",
            ListingMatch.effective_to.is_(None),
        )
    )
    errors = 0
    for row in connection.execute(statement).mappings():
        expected_key = (
            device_item_key(
                brand_code=row["brand_code"],
                channel_code=row["channel_code"],
                product_id=row["external_product_id"],
            )
            if row["brand_code"] and row["external_product_id"]
            else None
        )
        if (
            row["canonical_key"] != expected_key
            or row["item_type"] != "MODEL"
            or row["attribute_profile_code"] != "electronic-device"
            or row["identity_fingerprint"] != row["revision_fingerprint"]
            or row["attributes"] != row["normalized_attributes"]
            or row["measure_type"] != "COUNT"
            or row["base_unit"] != "PIECE"
            or row["quantity_value"] != 1
        ):
            errors += 1
    return errors


def _artifact_errors(connection: Connection, store: RawArtifactStore | None) -> int:
    errors = 0
    records = connection.execute(
        select(
            CatalogCrawlRecord.raw_path,
            CatalogCrawlRecord.raw_hash,
            CatalogCrawlRecord.raw_size_bytes,
            CatalogCrawlRecord.artifact_manifest,
        )
    ).mappings()
    for record in records:
        references: dict[str, tuple[str, int | None]] = {}
        invalid = False
        if record["raw_path"] is not None or record["raw_hash"] is not None:
            if not record["raw_path"] or not record["raw_hash"]:
                invalid = True
            else:
                references[record["raw_path"]] = (record["raw_hash"], record["raw_size_bytes"])
        manifest = record["artifact_manifest"]
        if not isinstance(manifest, list):
            errors += 1
            continue
        for part in manifest:
            if not isinstance(part, dict):
                invalid = True
                continue
            if "relative_path" not in part:
                continue  # Government discovery hashes do not promise a stored file.
            path, digest = part.get("relative_path"), part.get("source_hash")
            if (
                not isinstance(path, str)
                or not isinstance(digest, str)
                or not path
                or path in references
                and references[path][0] != digest
            ):
                invalid = True
            else:
                references.setdefault(path, (digest, None))
        for path, (digest, expected_size) in references.items():
            if (
                PurePosixPath(path).is_absolute()
                or ".." in PurePosixPath(path).parts
                or len(digest) != 64
                or any(character not in "0123456789abcdef" for character in digest)
            ):
                invalid = True
                continue
            if store is not None:
                try:
                    body = store.load(path, expected_hash=digest)
                    if expected_size is not None and len(body) != expected_size:
                        invalid = True
                except (ArtifactError, OSError, EOFError):
                    invalid = True
        errors += int(invalid)
    return errors


def _scalar(
    connection: Connection,
    statement: str,
    parameters: dict[str, Any] | None = None,
) -> int:
    return int(connection.scalar(text(statement), parameters or {}) or 0)
