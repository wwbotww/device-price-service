from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import Connection, text


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
    require_completed_runs: bool,
) -> DatabaseAuditReport:
    critical = {
        "price_projection_cardinality": _scalar(
            connection,
            """
            SELECT COUNT(*)
            FROM (
                SELECT pc.offer_id
                FROM price_current pc
                LEFT JOIN price_history ph
                    ON ph.offer_id = pc.offer_id AND ph.valid_to IS NULL
                GROUP BY pc.offer_id
                HAVING COUNT(ph.id) <> 1
                UNION ALL
                SELECT ph.offer_id
                FROM price_history ph
                LEFT JOIN price_current pc ON pc.offer_id = ph.offer_id
                WHERE ph.valid_to IS NULL AND pc.id IS NULL
            ) inconsistent_projection
            """,
        ),
        "price_projection_state_mismatch": _scalar(
            connection,
            """
            SELECT COUNT(*)
            FROM price_current pc
            JOIN price_history ph ON ph.offer_id = pc.offer_id AND ph.valid_to IS NULL
            JOIN official_offer oo ON oo.id = pc.offer_id
            WHERE NOT (pc.currency <=> ph.currency)
               OR NOT (pc.original_price <=> ph.original_price)
               OR NOT (pc.original_price_type <=> ph.original_price_type)
               OR NOT (pc.current_price <=> ph.current_price)
               OR NOT (oo.availability <=> ph.availability)
            """,
        ),
        "overlapping_price_history": _scalar(
            connection,
            """
            SELECT COUNT(*)
            FROM price_history left_history
            JOIN price_history right_history
              ON right_history.offer_id = left_history.offer_id
             AND right_history.id > left_history.id
             AND left_history.valid_from < COALESCE(right_history.valid_to, '9999-12-31')
             AND right_history.valid_from < COALESCE(left_history.valid_to, '9999-12-31')
            """,
        ),
        "untraceable_current_price": _scalar(
            connection,
            """
            SELECT COUNT(*)
            FROM price_current pc
            LEFT JOIN crawl_record cr
              ON cr.crawl_run_id = pc.crawl_run_id
             AND cr.raw_hash = pc.source_hash
             AND cr.fetch_status = 'SUCCEEDED'
             AND (
                    cr.parse_status = 'SUCCEEDED'
                    OR cr.error_code = 'OFF_SHELF_CONFIRMED'
                 )
            WHERE cr.id IS NULL
            """,
        ),
        "invalid_official_scope": _scalar(
            connection,
            """
            SELECT COUNT(*)
            FROM sales_channel
            WHERE region_code <> 'CN'
               OR currency <> 'CNY'
               OR seller_type <> 'OFFICIAL_DIRECT'
            """,
        ),
        "stale_running_crawl": _scalar(
            connection,
            """
            SELECT COUNT(*)
            FROM crawl_run
            WHERE status = 'RUNNING'
              AND started_at < UTC_TIMESTAMP(3) - INTERVAL :stale_run_minutes MINUTE
            """,
            {"stale_run_minutes": stale_run_minutes},
        ),
    }
    channels_without_completed_run = _scalar(
        connection,
        """
        SELECT COUNT(*)
        FROM sales_channel sc
        WHERE sc.enabled = 1
          AND NOT EXISTS (
              SELECT 1
              FROM crawl_run cr
              WHERE cr.channel_id = sc.id
                AND cr.status IN ('SUCCEEDED', 'PARTIAL')
          )
        """,
    )
    if require_completed_runs:
        critical["channels_without_completed_run"] = channels_without_completed_run

    warnings = {
        "channels_without_completed_run": channels_without_completed_run,
        "stale_current_price": _scalar(
            connection,
            """
            SELECT COUNT(*)
            FROM price_current
            WHERE observed_at < UTC_TIMESTAMP(3) - INTERVAL :stale_price_hours HOUR
            """,
            {"stale_price_hours": stale_price_hours},
        ),
        "recent_rejected_price_changes": _scalar(
            connection,
            """
            SELECT COUNT(*)
            FROM crawl_record
            WHERE error_code = 'LARGE_PRICE_CHANGE_UNCONFIRMED'
              AND fetched_at >= UTC_TIMESTAMP(3) - INTERVAL 24 HOUR
            """,
        ),
        "offers_waiting_off_shelf_confirmation": _scalar(
            connection,
            """
            SELECT COUNT(*)
            FROM official_offer
            WHERE consecutive_misses > 0 AND availability <> 'OFF_SHELF'
            """,
        ),
    }
    return DatabaseAuditReport(critical=critical, warnings=warnings)


def _scalar(
    connection: Connection,
    statement: str,
    parameters: dict[str, int] | None = None,
) -> int:
    return int(connection.scalar(text(statement), parameters or {}) or 0)
