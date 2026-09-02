from __future__ import annotations

import logging

from common_utils.metering import open_telemetry_mysql_connection


logger = logging.getLogger("common_utils.deal_correlation")


def _execute(query: str, params: tuple, *, context: str) -> None:
    connection = None
    cursor = None
    try:
        connection = open_telemetry_mysql_connection()
        cursor = connection.cursor()
        cursor.execute(query, params)
        connection.commit()
    except Exception as exc:
        logger.warning("deal_correlation write failed context=%s error=%s", context, exc)
    finally:
        try:
            if cursor is not None:
                cursor.close()
        except Exception:
            pass
        try:
            if connection is not None and connection.is_connected():
                connection.close()
        except Exception:
            pass


def record_deal_match(
    *,
    source_repo: str,
    content_id: str,
    asin: str,
    marketplace: str | None = None,
) -> None:
    """Call when a candidate is first matched to an ASIN (e.g.
    upsert_amzn_source_candidate in amazonnew), so the correlation row exists
    before publish_deal ever runs."""
    _execute(
        """
        INSERT INTO deal_correlation (
            source_repo, content_id, asin, marketplace, matched_at
        ) VALUES (%s, %s, %s, %s, UTC_TIMESTAMP())
        ON DUPLICATE KEY UPDATE
            asin = VALUES(asin),
            marketplace = VALUES(marketplace),
            matched_at = VALUES(matched_at)
        """,
        (source_repo, content_id, asin, marketplace),
        context=f"match source_repo={source_repo} content_id={content_id}",
    )


def record_deal_publish(
    *,
    source_repo: str,
    content_id: str,
    dealvant_target_table: str,
    dealvant_target_id: str,
) -> None:
    """Call once a candidate is actually published to Dealvant, filling in the
    side of the correlation row that can't be derived from a live join."""
    _execute(
        """
        UPDATE deal_correlation
        SET dealvant_target_table = %s,
            dealvant_target_id = %s,
            published_at = UTC_TIMESTAMP()
        WHERE source_repo = %s AND content_id = %s
        """,
        (dealvant_target_table, dealvant_target_id, source_repo, content_id),
        context=f"publish source_repo={source_repo} content_id={content_id}",
    )
