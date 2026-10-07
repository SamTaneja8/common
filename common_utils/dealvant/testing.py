"""In-memory stand-in for the roundup request queue, for tests in the repos
that use it (reviewgate, autopub) -- the real queue is in dealstage MySQL,
which their tests can't start. Same functions as requests.py, with the
MySQL connection argument ignored; offer checks and descriptions still use
the real code against the Postgres connection given.

    queue = MemoryRoundupQueue()
    monkeypatch.setattr(module_under_test, "roundup_requests", queue)
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Sequence

from common_utils.dealvant import requests


class MemoryRoundupQueue:
    RequestError = requests.RequestError
    RequestQueueMissing = requests.RequestQueueMissing
    RoundupRequest = requests.RoundupRequest
    MIN_OFFERS = requests.MIN_OFFERS
    MAX_OFFERS = requests.MAX_OFFERS

    def __init__(self) -> None:
        self.rows: dict[str, dict[str, Any]] = {}

    def check_table(self, mysql_conn=None) -> None:
        return None

    def validate_offers(self, pg_conn, *, category_id: str, offer_ids: Sequence[str]) -> list[str]:
        return requests.validate_offers(pg_conn, category_id=category_id, offer_ids=offer_ids)

    def enqueue(self, mysql_conn, pg_conn, *, category_id: str, offer_ids: Sequence[str], requested_by: str) -> str:
        ids = requests.validate_offers(pg_conn, category_id=category_id, offer_ids=offer_ids)
        request_id = requests.new_request_id()
        self.rows[request_id] = {
            "id": request_id,
            "category_id": category_id,
            "offer_ids": ids,
            "requested_by": (requested_by or "").strip() or "unknown",
            "status": "QUEUED",
            "attempts": 0,
            "result_status": None,
            "gate": None,
            "error": None,
            "article_id": None,
            "run_id": None,
            "created_at": datetime.now(timezone.utc),
            "started_at": None,
            "finished_at": None,
        }
        return request_id

    def claim_next(self, mysql_conn=None) -> requests.RoundupRequest | None:
        queued = sorted(request_id for request_id, row in self.rows.items() if row["status"] == "QUEUED")
        if not queued:
            return None
        row = self.rows[queued[0]]
        row.update(status="RUNNING", attempts=row["attempts"] + 1, started_at=datetime.now(timezone.utc), error=None)
        return requests.RoundupRequest(
            id=row["id"],
            category_id=row["category_id"],
            offer_ids=list(row["offer_ids"]),
            requested_by=row["requested_by"],
            attempts=row["attempts"],
        )

    def finish(self, mysql_conn, request_id: str, *, run_id: str, article_id: str, result_status: str, gate: dict[str, Any]) -> None:
        self.rows[request_id].update(
            status="DONE", run_id=run_id, article_id=article_id, result_status=result_status, gate=gate,
            error=None, finished_at=datetime.now(timezone.utc),
        )

    def fail(self, mysql_conn, request_id: str, *, run_id: str | None, error: str) -> None:
        self.rows[request_id].update(status="FAILED", run_id=run_id, error=error[:2000], finished_at=datetime.now(timezone.utc))

    def list_recent(self, mysql_conn, pg_conn, *, limit: int = 20) -> list[dict[str, Any]]:
        rows = [dict(self.rows[request_id]) for request_id in sorted(self.rows, reverse=True)[:limit]]
        return requests.describe(rows, pg_conn)
