"""Roundup requests: reviewgate picks a category and its offers, autopub
writes the roundup.

The queue is reviewgate_roundup_requests in dealstage MySQL (VPS1, next to
both reviewgate and autopub's worker), defined in reviewgate's
schema/reviewgate_schema.sql. The offers themselves are in Dealvant's
Postgres, so enqueue() and list_recent() also take a Postgres connection to
check and describe them.

reviewgate's /roundups page calls enqueue(); autopub's worker calls
claim_next(), runs the roundup stages, then finish() or fail().

MySQL connections are mysql.connector ones; every function here commits
(each is a queue state change the other side must see straight away).
"""

from __future__ import annotations

import json
import time
import uuid
from dataclasses import dataclass
from typing import Any, Sequence

TABLE = "reviewgate_roundup_requests"
MIN_OFFERS = 2
MAX_OFFERS = 8
# A RUNNING request older than this is assumed abandoned (worker stopped
# mid-run) and is queued again, up to MAX_ATTEMPTS runs in total.
STALE_AFTER_MINUTES = 30
MAX_ATTEMPTS = 2


class RequestError(ValueError):
    """The request can't be queued (wrong offer count, offers not live)."""


class RequestQueueMissing(RuntimeError):
    """dealstage MySQL has no reviewgate_roundup_requests table yet."""


@dataclass
class RoundupRequest:
    id: str
    category_id: str
    offer_ids: list[str]
    requested_by: str
    attempts: int


def _json(value: Any) -> Any:
    if isinstance(value, (bytes, bytearray)):
        value = value.decode("utf-8")
    if isinstance(value, str):
        return json.loads(value) if value else None
    return value


def new_request_id() -> str:
    """Sorts by creation time (millisecond prefix), so ordering by id is
    first in, first out even within one second."""
    return f"rr-{int(time.time() * 1000):013x}{uuid.uuid4().hex[:6]}"


def check_table(mysql_conn) -> None:
    """Raises RequestQueueMissing, with what to do, if the table isn't there."""
    cursor = mysql_conn.cursor()
    try:
        cursor.execute(f"SELECT 1 FROM {TABLE} LIMIT 1")  # noqa: S608 - fixed table name
        cursor.fetchall()
    except Exception as exc:  # mysql.connector.errors.ProgrammingError: table doesn't exist
        raise RequestQueueMissing(
            f"dealstage MySQL has no {TABLE} table, or this login can't read it. Deploy reviewgate first "
            "(its startup creates the table) and check the login's grants (autopub/sql/autopub_mysql_login.sql)."
        ) from exc
    finally:
        cursor.close()


def validate_offers(pg_conn, *, category_id: str, offer_ids: Sequence[str]) -> list[str]:
    """The picked offers, de-duplicated in order, if there are 2-8 of them
    and every one is live in the category; RequestError otherwise."""
    ids = list(dict.fromkeys(str(offer_id).strip() for offer_id in offer_ids if str(offer_id).strip()))
    if not MIN_OFFERS <= len(ids) <= MAX_OFFERS:
        raise RequestError(f"Pick between {MIN_OFFERS} and {MAX_OFFERS} offers (got {len(ids)}).")
    with pg_conn.cursor() as cursor:
        cursor.execute(
            "SELECT id FROM offers WHERE id = ANY(%s) AND category_id = %s AND status = 'LIVE'",
            (ids, category_id),
        )
        live = {row[0] if isinstance(row, tuple) else row["id"] for row in cursor.fetchall()}
    if any(offer_id not in live for offer_id in ids):
        missing = sum(1 for offer_id in ids if offer_id not in live)
        raise RequestError(f"{missing} selected offer(s) are no longer live in this category; reload and pick again.")
    return ids


def enqueue(mysql_conn, pg_conn, *, category_id: str, offer_ids: Sequence[str], requested_by: str) -> str:
    """Checks the offers in Dealvant, then queues the request. Returns its id."""
    ids = validate_offers(pg_conn, category_id=category_id, offer_ids=offer_ids)
    request_id = new_request_id()
    cursor = mysql_conn.cursor()
    try:
        cursor.execute(
            f"INSERT INTO {TABLE} (id, category_id, offer_ids_json, requested_by) VALUES (%s, %s, %s, %s)",  # noqa: S608
            (request_id, category_id, json.dumps(ids), (requested_by or "").strip()[:128] or "unknown"),
        )
        mysql_conn.commit()
    finally:
        cursor.close()
    return request_id


def claim_next(mysql_conn) -> RoundupRequest | None:
    """Takes the oldest QUEUED request and marks it RUNNING, or returns None.
    Safe with several workers (FOR UPDATE SKIP LOCKED). Abandoned RUNNING
    requests are queued again first, or failed after MAX_ATTEMPTS."""
    cursor = mysql_conn.cursor(dictionary=True)
    try:
        cursor.execute(
            f"""
            UPDATE {TABLE}
            SET status = IF(attempts >= %s, 'FAILED', 'QUEUED'),
                error = IF(attempts >= %s, 'Stopped mid-run too many times.', error),
                finished_at = IF(attempts >= %s, NOW(), finished_at)
            WHERE status = 'RUNNING' AND started_at < NOW() - INTERVAL %s MINUTE
            """,  # noqa: S608 - fixed table name
            (MAX_ATTEMPTS, MAX_ATTEMPTS, MAX_ATTEMPTS, STALE_AFTER_MINUTES),
        )
        mysql_conn.commit()
        cursor.execute(
            f"""
            SELECT id, category_id, offer_ids_json, requested_by, attempts
            FROM {TABLE}
            WHERE status = 'QUEUED'
            ORDER BY id
            LIMIT 1
            FOR UPDATE SKIP LOCKED
            """  # noqa: S608
        )
        row = cursor.fetchone()
        if row is None:
            mysql_conn.commit()
            return None
        cursor.execute(
            f"""
            UPDATE {TABLE}
            SET status = 'RUNNING', attempts = attempts + 1, started_at = NOW(), error = NULL
            WHERE id = %s
            """,  # noqa: S608
            (row["id"],),
        )
        mysql_conn.commit()
    finally:
        cursor.close()
    return RoundupRequest(
        id=row["id"],
        category_id=row["category_id"],
        offer_ids=list(_json(row["offer_ids_json"]) or []),
        requested_by=row["requested_by"],
        attempts=int(row["attempts"]) + 1,
    )


def finish(mysql_conn, request_id: str, *, run_id: str, article_id: str, result_status: str, gate: dict[str, Any]) -> None:
    cursor = mysql_conn.cursor()
    try:
        cursor.execute(
            f"""
            UPDATE {TABLE}
            SET status = 'DONE', run_id = %s, article_id = %s, result_status = %s, gate_json = %s,
                error = NULL, finished_at = NOW()
            WHERE id = %s
            """,  # noqa: S608
            (run_id, article_id, result_status, json.dumps(gate), request_id),
        )
        mysql_conn.commit()
    finally:
        cursor.close()


def fail(mysql_conn, request_id: str, *, run_id: str | None, error: str) -> None:
    cursor = mysql_conn.cursor()
    try:
        cursor.execute(
            f"UPDATE {TABLE} SET status = 'FAILED', run_id = %s, error = %s, finished_at = NOW() WHERE id = %s",  # noqa: S608
            (run_id, error[:2000], request_id),
        )
        mysql_conn.commit()
    finally:
        cursor.close()


def list_recent(mysql_conn, pg_conn, *, limit: int = 20) -> list[dict[str, Any]]:
    """Newest requests, described from Dealvant: category name, offer titles
    (in the reviewer's order) and the resulting article."""
    cursor = mysql_conn.cursor(dictionary=True)
    try:
        cursor.execute(
            f"""
            SELECT id, category_id, offer_ids_json, requested_by, status, attempts, result_status,
                   gate_json, error, article_id, created_at, started_at, finished_at
            FROM {TABLE}
            ORDER BY id DESC
            LIMIT %s
            """,  # noqa: S608
            (limit,),
        )
        rows = cursor.fetchall()
        mysql_conn.commit()  # end the read snapshot so the next call sees new rows
    finally:
        cursor.close()
    for row in rows:
        row["offer_ids"] = list(_json(row.pop("offer_ids_json")) or [])
        row["gate"] = _json(row.pop("gate_json"))
    return describe(rows, pg_conn)


def describe(rows: list[dict[str, Any]], pg_conn) -> list[dict[str, Any]]:
    """Adds category_name, offer_titles (in order) and the article's slug,
    title and status from Dealvant to request rows that have category_id,
    offer_ids and article_id."""
    if not rows:
        return []
    category_ids = sorted({row["category_id"] for row in rows})
    offer_ids = sorted({offer_id for row in rows for offer_id in row["offer_ids"]})
    article_ids = sorted({row["article_id"] for row in rows if row["article_id"]})
    with pg_conn.cursor() as pg:
        pg.execute("SELECT id, name FROM categories WHERE id = ANY(%s)", (category_ids,))
        categories = {r[0]: r[1] for r in map(_as_tuple, pg.fetchall())}
        pg.execute("SELECT id, title FROM offers WHERE id = ANY(%s)", (offer_ids,))
        offers = {r[0]: r[1] for r in map(_as_tuple, pg.fetchall())}
        pg.execute("SELECT id, slug, title, status FROM articles WHERE id = ANY(%s)", (article_ids,))
        articles = {r[0]: r[1:] for r in map(_as_tuple, pg.fetchall())}
    for row in rows:
        row["category_name"] = categories.get(row["category_id"], row["category_id"])
        row["offer_titles"] = [offers.get(offer_id, offer_id) for offer_id in row["offer_ids"]]
        row["article_slug"], row["article_title"], row["article_status"] = articles.get(row["article_id"], (None, None, None))
    return rows


def _as_tuple(row: Any) -> tuple:
    return tuple(row.values()) if isinstance(row, dict) else tuple(row)
