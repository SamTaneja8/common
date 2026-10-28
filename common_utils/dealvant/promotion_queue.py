"""The affiliate promotion queue: the affiliate repo fills it (one run per
network: Awin, Rakuten, CJ), and reviewgate's /affiliate page reviews and
publishes from it.

One MySQL database, affiliate_db (unified-mysql on VPS1), for every network:
each row is keyed by (network, promotion_id), created from SCHEMA_STATEMENTS
by the affiliate repo. Promotion lifecycle (affiliate_promotions.status):

  NEW      collected, waiting for its image and AI write-up
  READY    written up, waiting for an editor on reviewgate /affiliate
  SKIPPED  the AI judged it not a usable deal (skip_reason); an editor can
           still publish it
  FAILED   the write-up failed AI_MAX_ATTEMPTS times; "Write up again" retries
  PUBLISHED  live on Dealvant (dealvant_offer_id)
  REJECTED   an editor turned it down
  ENDED    the network no longer lists it, or its end date passed; a
           published one is ENDED on Dealvant too

A promotion the network changes goes back to NEW for a fresh write-up,
unless it's PUBLISHED (marked changed_since_publish for the editor to
republish) or REJECTED (stays rejected).

MySQL connections are mysql.connector ones; functions here commit.
Postgres (Dealvant) connections are psycopg ones; publish() commits both.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlparse

from common_utils.dealvant import promotions, store

DATABASE = "affiliate_db"
PROMOTIONS = f"{DATABASE}.affiliate_promotions"
ADVERTISERS = f"{DATABASE}.affiliate_advertisers"
RUNS = f"{DATABASE}.affiliate_runs"
AI_MAX_ATTEMPTS = 3

# network -> how editors and alerts name it. The offers' source_name is the key.
NETWORKS = {"awin": "Awin", "rakuten": "Rakuten", "cj": "CJ"}
STATUSES = ("NEW", "READY", "SKIPPED", "FAILED", "PUBLISHED", "REJECTED", "ENDED")
PUBLISHABLE_STATUSES = ("READY", "SKIPPED", "PUBLISHED")
EDITABLE_FIELDS = (
    "draft_title",
    "draft_short_description",
    "draft_long_description",
    "draft_category_slug",
    "draft_best_for",
    "draft_meta_title",
    "draft_meta_description",
    "image_url",
)

SCHEMA_STATEMENTS = (
    f"CREATE DATABASE IF NOT EXISTS {DATABASE} CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci",
    f"""
    CREATE TABLE IF NOT EXISTS {ADVERTISERS} (
        network VARCHAR(16) NOT NULL,
        advertiser_id VARCHAR(64) NOT NULL,
        name VARCHAR(255) NOT NULL,
        merchant_name VARCHAR(255) NOT NULL,
        display_url TEXT,
        click_through_url TEXT,
        logo_url TEXT,
        primary_sector VARCHAR(255),
        updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
        PRIMARY KEY (network, advertiser_id)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
    """,
    f"""
    CREATE TABLE IF NOT EXISTS {PROMOTIONS} (
        network VARCHAR(16) NOT NULL,
        promotion_id VARCHAR(64) NOT NULL,
        advertiser_id VARCHAR(64) NOT NULL,
        advertiser_name VARCHAR(255) NOT NULL,
        merchant_name VARCHAR(255) NOT NULL,
        promo_type VARCHAR(32) NOT NULL,
        title TEXT NOT NULL,
        description TEXT,
        terms TEXT,
        voucher_code VARCHAR(255),
        start_date DATETIME NULL,
        end_date DATETIME NULL,
        url TEXT,
        url_tracking TEXT,
        raw_json JSON,
        content_hash CHAR(40) NOT NULL,
        active TINYINT(1) NOT NULL DEFAULT 1,
        first_seen_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
        last_seen_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
        status VARCHAR(16) NOT NULL DEFAULT 'NEW',
        image_url TEXT,
        image_source VARCHAR(16),
        ai_attempts INT NOT NULL DEFAULT 0,
        ai_error TEXT,
        ai_at DATETIME NULL,
        skip_reason TEXT,
        draft_title VARCHAR(255),
        draft_short_description TEXT,
        draft_long_description MEDIUMTEXT,
        draft_category_slug VARCHAR(128),
        draft_best_for VARCHAR(255),
        draft_meta_title VARCHAR(255),
        draft_meta_description TEXT,
        dealvant_offer_id VARCHAR(64),
        dealvant_slug VARCHAR(255),
        published_at DATETIME NULL,
        changed_since_publish TINYINT(1) NOT NULL DEFAULT 0,
        reviewed_by VARCHAR(128),
        reviewed_at DATETIME NULL,
        ended_at DATETIME NULL,
        ended_reason VARCHAR(255),
        updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
        PRIMARY KEY (network, promotion_id),
        KEY idx_affiliate_promotions_status (status, network, last_seen_at),
        KEY idx_affiliate_promotions_merchant (merchant_name)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
    """,
    f"""
    CREATE TABLE IF NOT EXISTS {RUNS} (
        id BIGINT AUTO_INCREMENT PRIMARY KEY,
        network VARCHAR(16) NOT NULL,
        started_at DATETIME NOT NULL,
        finished_at DATETIME NULL,
        status VARCHAR(16) NOT NULL,
        counts_json JSON,
        error TEXT,
        KEY idx_affiliate_runs_network (network, started_at)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
    """,
)


class QueueError(ValueError):
    """The promotion can't do that (unknown, wrong status, missing fields)."""


def network_label(network: str) -> str:
    return NETWORKS.get(network, network)


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _fetch(mysql_conn, sql: str, params: tuple = ()) -> list[dict[str, Any]]:
    cursor = mysql_conn.cursor(dictionary=True)
    try:
        cursor.execute(sql, params)
        return cursor.fetchall()
    finally:
        cursor.close()


def _execute(mysql_conn, sql: str, params: tuple = ()) -> int:
    cursor = mysql_conn.cursor()
    try:
        cursor.execute(sql, params)
        mysql_conn.commit()
        return cursor.rowcount
    finally:
        cursor.close()


# ---------------------------------------------------------------- reading


def status_counts(mysql_conn, network: str | None = None) -> dict[str, int]:
    """Promotions per status, for one network or all."""
    where, params = ("WHERE network = %s", (network,)) if network else ("", ())
    rows = _fetch(mysql_conn, f"SELECT status, COUNT(*) AS n FROM {PROMOTIONS} {where} GROUP BY status", params)  # noqa: S608
    counts = {status: 0 for status in STATUSES}
    counts.update({row["status"]: int(row["n"]) for row in rows})
    return counts


def list_promotions(mysql_conn, status: str, *, network: str | None = None, limit: int = 200) -> list[dict[str, Any]]:
    """Promotions in one status (one network or all). READY lists the
    soonest-ending first, so nothing expires waiting; the rest newest first."""
    if status not in STATUSES:
        raise QueueError(f"Unknown status {status!r}.")
    order = "end_date IS NULL, end_date ASC, last_seen_at DESC" if status == "READY" else "updated_at DESC"
    where = "status = %s" + (" AND network = %s" if network else "")
    params = (status, network, limit) if network else (status, limit)
    return _fetch(
        mysql_conn,
        f"""
        SELECT network, promotion_id, merchant_name, promo_type, title, voucher_code, end_date,
               draft_title, draft_category_slug, image_url, skip_reason, ai_error, ai_attempts,
               dealvant_slug, changed_since_publish, reviewed_by, updated_at, last_seen_at
        FROM {PROMOTIONS}
        WHERE {where}
        ORDER BY {order}
        LIMIT %s
        """,  # noqa: S608 - fixed table names and clauses
        params,
    )


def changed_since_publish(mysql_conn, *, network: str | None = None, limit: int = 100) -> list[dict[str, Any]]:
    where = " AND network = %s" if network else ""
    params = (network, limit) if network else (limit,)
    return _fetch(
        mysql_conn,
        f"""
        SELECT network, promotion_id, merchant_name, title, draft_title, dealvant_slug, updated_at
        FROM {PROMOTIONS}
        WHERE status = 'PUBLISHED' AND changed_since_publish = 1{where}
        ORDER BY updated_at DESC
        LIMIT %s
        """,  # noqa: S608
        params,
    )


def get_promotion(mysql_conn, network: str, promotion_id: str) -> dict[str, Any] | None:
    rows = _fetch(
        mysql_conn,
        f"""
        SELECT p.*, a.display_url, a.click_through_url, a.logo_url, a.primary_sector
        FROM {PROMOTIONS} p
        LEFT JOIN {ADVERTISERS} a ON a.network = p.network AND a.advertiser_id = p.advertiser_id
        WHERE p.network = %s AND p.promotion_id = %s
        """,  # noqa: S608
        (network, str(promotion_id)),
    )
    if not rows:
        return None
    row = rows[0]
    row.pop("raw_json", None)
    return row


def live_deals_elsewhere(pg_conn, merchant_name: str, network: str) -> list[dict[str, Any]]:
    """Live Dealvant offers for the same store from other sources (another
    network, amazonnew, ...), so an editor can spot a near-duplicate."""
    slug = promotions.slugify(merchant_name, fallback="store")
    with pg_conn.cursor() as cursor:
        cursor.execute(
            """
            SELECT o.title, o.slug, o.source_name, o.coupon_code
            FROM offers o
            INNER JOIN merchants m ON m.id = o.merchant_id
            WHERE m.slug = %s AND o.status = 'LIVE' AND o.removed_at IS NULL
              AND COALESCE(o.source_name, '') <> %s
            ORDER BY o.updated_at DESC
            LIMIT 10
            """,
            (slug, network),
        )
        return cursor.fetchall()


# ---------------------------------------------------------------- editing


def save_draft(mysql_conn, network: str, promotion_id: str, fields: dict[str, Any]) -> None:
    """An editor's changes to the write-up (EDITABLE_FIELDS only)."""
    values = {key: (str(fields[key]).strip() or None) for key in EDITABLE_FIELDS if key in fields}
    if values.get("image_url") and not str(values["image_url"]).startswith("https://"):
        raise QueueError("Image URL must start with https://.")
    if not values:
        return
    assignments = ", ".join(f"{key} = %s" for key in values)
    if not _execute(
        mysql_conn,
        f"UPDATE {PROMOTIONS} SET {assignments} WHERE network = %s AND promotion_id = %s",  # noqa: S608 - keys from EDITABLE_FIELDS
        (*values.values(), network, str(promotion_id)),
    ):
        if get_promotion(mysql_conn, network, promotion_id) is None:
            raise QueueError("Unknown promotion.")


def reject(mysql_conn, network: str, promotion_id: str, *, reviewed_by: str) -> None:
    _execute(
        mysql_conn,
        f"UPDATE {PROMOTIONS} SET status = 'REJECTED', reviewed_by = %s, reviewed_at = %s "  # noqa: S608
        "WHERE network = %s AND promotion_id = %s AND status IN ('NEW', 'READY', 'SKIPPED', 'FAILED')",
        (reviewed_by[:128], _now(), network, str(promotion_id)),
    )


def request_rewrite(mysql_conn, network: str, promotion_id: str) -> None:
    """Back to NEW: the network's next run writes it up again."""
    _execute(
        mysql_conn,
        f"UPDATE {PROMOTIONS} SET status = 'NEW', ai_attempts = 0, ai_error = NULL "  # noqa: S608
        "WHERE network = %s AND promotion_id = %s AND status IN ('READY', 'SKIPPED', 'FAILED', 'REJECTED')",
        (network, str(promotion_id)),
    )


# ---------------------------------------------------------------- publishing


def _as_utc(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    return None


def _website(row: dict[str, Any]) -> str:
    """The store's site: the network's display URL, else the deal page's
    origin, else the tracking link itself (it does lead to the store)."""
    display = (row.get("display_url") or "").strip()
    if display:
        return display if display.startswith(("http://", "https://")) else f"https://{display}"
    parsed = urlparse(row.get("url") or "")
    if parsed.scheme in ("http", "https") and parsed.netloc:
        return f"{parsed.scheme}://{parsed.netloc}"
    return row["url_tracking"]


def draft_from_row(row: dict[str, Any], *, category_id: str) -> promotions.PromotionDraft:
    """The Dealvant offer for a queue row (get_promotion's shape)."""
    title = (row.get("draft_title") or "").strip()
    if not title:
        raise QueueError("The deal needs a title before it can be published.")
    if not (row.get("url_tracking") or "").startswith("https://"):
        raise QueueError("The network gave this promotion no https tracking link.")
    short = (row.get("draft_short_description") or "").strip() or title
    return promotions.PromotionDraft(
        network=row["network"],
        promotion_id=str(row["promotion_id"]),
        merchant_name=row["merchant_name"],
        merchant_website_url=_website(row),
        merchant_tracking_url=(row.get("click_through_url") or "").strip() or row["url_tracking"],
        merchant_logo_url=row.get("logo_url"),
        title=title,
        short_description=short,
        long_description=(row.get("draft_long_description") or "").strip() or short,
        terms=(row.get("terms") or "").strip(),
        category_id=category_id,
        affiliate_url=row["url_tracking"],
        image_url=row.get("image_url") or None,
        coupon_code=(row.get("voucher_code") or "").strip() or None,
        expires_at=_as_utc(row.get("end_date")),
        meta_title=(row.get("draft_meta_title") or "").strip() or None,
        meta_description=(row.get("draft_meta_description") or "").strip() or None,
        best_for=(row.get("draft_best_for") or "").strip() or None,
    )


def category_id_for(pg_conn, slug: str | None) -> str:
    if not slug:
        raise QueueError("Pick a category before publishing.")
    with pg_conn.cursor() as cursor:
        cursor.execute("SELECT id FROM categories WHERE slug = %s", (slug,))
        row = cursor.fetchone()
    if row is None:
        raise QueueError(f"Dealvant has no category {slug!r}.")
    return row[0] if isinstance(row, tuple) else row["id"]


def publish(mysql_conn, pg_conn, network: str, promotion_id: str, *, reviewed_by: str) -> promotions.PublishedPromotion:
    """Publishes (or republishes) one promotion on Dealvant and records it."""
    row = get_promotion(mysql_conn, network, promotion_id)
    if row is None:
        raise QueueError("Unknown promotion.")
    if row["status"] not in PUBLISHABLE_STATUSES:
        raise QueueError(f"A {row['status']} promotion can't be published.")
    if not row["active"]:
        raise QueueError(f"{network_label(network)} no longer lists this promotion.")
    draft = draft_from_row(row, category_id=category_id_for(pg_conn, row.get("draft_category_slug")))
    store.require_schema(pg_conn)
    published = promotions.publish_promotion(pg_conn, draft)
    pg_conn.commit()
    _execute(
        mysql_conn,
        f"""
        UPDATE {PROMOTIONS}
        SET status = 'PUBLISHED', dealvant_offer_id = %s, dealvant_slug = %s, published_at = %s,
            changed_since_publish = 0, reviewed_by = %s, reviewed_at = %s
        WHERE network = %s AND promotion_id = %s
        """,  # noqa: S608
        (published.offer_id, published.slug, _now(), reviewed_by[:128], _now(), network, str(promotion_id)),
    )
    return published
