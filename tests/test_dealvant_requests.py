"""common_utils.dealvant.requests: the roundup request queue between
reviewgate and autopub.

The queue is in dealstage MySQL, which these tests can't start, so MySQL is
a scripted fake that records each statement and returns canned rows: the
tests check the statements, parameters, commits and row handling, not MySQL
itself (that happens on VPS1, OPERATIONS "Rollout: autopub" step 7). Offer
checks and request descriptions use the real Postgres harness
(DEALVANT_TEST_PG_URI); those tests are skipped without it.
"""

from __future__ import annotations

import json
import os
from datetime import datetime

import pytest

from common_utils.dealvant import requests

PG_URI = os.environ.get("DEALVANT_TEST_PG_URI")
needs_pg = pytest.mark.skipif(not PG_URI, reason="DEALVANT_TEST_PG_URI not set")


class FakeMySql:
    """mysql.connector stand-in. `results` holds what successive fetches
    return; every execute is recorded with its params."""

    def __init__(self, results=(), fail_execute: bool = False):
        self.results = list(results)
        self.executed: list[tuple[str, tuple]] = []
        self.commits = 0
        self.fail_execute = fail_execute

    def cursor(self, dictionary: bool = False):
        return FakeCursor(self)

    def commit(self):
        self.commits += 1


class FakeCursor:
    def __init__(self, conn: FakeMySql):
        self.conn = conn

    def execute(self, sql, params=()):
        if self.conn.fail_execute:
            raise RuntimeError("Table 'dealstage_db.reviewgate_roundup_requests' doesn't exist")
        self.conn.executed.append((" ".join(sql.split()), tuple(params or ())))

    def fetchone(self):
        return self.conn.results.pop(0) if self.conn.results else None

    def fetchall(self):
        return self.conn.results.pop(0) if self.conn.results else []

    def close(self):
        pass


@pytest.fixture()
def pg():
    psycopg = pytest.importorskip("psycopg")
    from common_utils.dealvant import store

    with psycopg.connect(PG_URI) as connection:
        with connection.cursor() as cursor:
            cursor.execute("TRUNCATE price_observations, products, article_offers, articles, offers, merchants, categories CASCADE")
            cursor.execute("INSERT INTO categories (id, name, slug, description, accent_color) VALUES ('c1','Home','home','d','#000'), ('c2','Tech','tech','d','#000')")
            cursor.execute(
                "INSERT INTO merchants (id, name, slug, tagline, description, website_url, tracking_url, hero_image,"
                " brand_color, commission_rate, category_id) VALUES ('m1','Amazon','amazon','t','d','https://amazon.com','','/i.svg','#000',0,'c1')"
            )
            cursor.execute(
                "INSERT INTO articles (id, title, slug, excerpt, body, status, hero_image, category_id, is_roundup)"
                " VALUES ('roundup-1', 'Kettle roundup', 'kettle-roundup', 'e', 'b', 'PUBLISHED', '/i.svg', 'c1', TRUE)"
            )
        for oid, category, status in [("o1", "c1", "LIVE"), ("o2", "c1", "LIVE"), ("o3", "c1", "LIVE"), ("t1", "c2", "LIVE"), ("x1", "c1", "ENDED")]:
            store.upsert_offer(connection, store.OfferRecord(
                id=oid, title=f"Offer {oid}", slug=oid, short_description="s", long_description="l", terms="t",
                cashback_percent=0, status=status, is_featured=False, merchant_id="m1", category_id=category,
            ))
        connection.commit()
        yield connection


@needs_pg
def test_validate_offers(pg) -> None:
    with pytest.raises(requests.RequestError, match="between 2 and 8"):
        requests.validate_offers(pg, category_id="c1", offer_ids=["o1", "o1", " "])
    with pytest.raises(requests.RequestError, match="1 selected offer"):
        requests.validate_offers(pg, category_id="c1", offer_ids=["o1", "x1"])  # ended
    with pytest.raises(requests.RequestError, match="no longer live"):
        requests.validate_offers(pg, category_id="c1", offer_ids=["o1", "t1"])  # other category
    assert requests.validate_offers(pg, category_id="c1", offer_ids=["o3", "o1", "o3"]) == ["o3", "o1"]


@needs_pg
def test_enqueue_inserts_the_checked_offers(pg) -> None:
    mysql = FakeMySql()
    request_id = requests.enqueue(mysql, pg, category_id="c1", offer_ids=["o3", "o1", "o3"], requested_by=" ed@example.com ")
    (sql, params), = mysql.executed
    assert sql.startswith("INSERT INTO reviewgate_roundup_requests (id, category_id, offer_ids_json, requested_by)")
    assert params == (request_id, "c1", '["o3", "o1"]', "ed@example.com")
    assert mysql.commits == 1
    with pytest.raises(requests.RequestError):
        requests.enqueue(FakeMySql(), pg, category_id="c1", offer_ids=["o1"], requested_by="ed")


def test_request_ids_sort_by_creation_time() -> None:
    first = requests.new_request_id()
    import time

    time.sleep(0.002)
    second = requests.new_request_id()
    assert first < second and len(first) <= 32 and first.startswith("rr-")


def test_claim_next_requeues_stale_then_claims_the_oldest() -> None:
    queued = {"id": "rr-1", "category_id": "c1", "offer_ids_json": '["o1", "o2"]', "requested_by": "ed", "attempts": 0}
    mysql = FakeMySql(results=[queued])
    claimed = requests.claim_next(mysql)
    assert claimed == requests.RoundupRequest(id="rr-1", category_id="c1", offer_ids=["o1", "o2"], requested_by="ed", attempts=1)
    stale, select, claim = mysql.executed
    assert "WHERE status = 'RUNNING' AND started_at < NOW() - INTERVAL %s MINUTE" in stale[0]
    assert stale[1] == (requests.MAX_ATTEMPTS, requests.MAX_ATTEMPTS, requests.MAX_ATTEMPTS, requests.STALE_AFTER_MINUTES)
    assert "WHERE status = 'QUEUED' ORDER BY id LIMIT 1 FOR UPDATE SKIP LOCKED" in select[0]
    assert "SET status = 'RUNNING', attempts = attempts + 1" in claim[0] and claim[1] == ("rr-1",)
    assert mysql.commits == 2  # the requeue, then the claim (which releases the row lock)


def test_claim_next_with_nothing_queued() -> None:
    mysql = FakeMySql(results=[None])
    assert requests.claim_next(mysql) is None
    assert len(mysql.executed) == 2 and mysql.commits == 2  # no row lock left open


def test_claim_next_reads_json_returned_as_bytes() -> None:
    row = {"id": "rr-2", "category_id": "c1", "offer_ids_json": b'["o3", "o1"]', "requested_by": "ed", "attempts": 1}
    assert requests.claim_next(FakeMySql(results=[row])).offer_ids == ["o3", "o1"]


def test_finish_and_fail() -> None:
    mysql = FakeMySql()
    requests.finish(mysql, "rr-1", run_id="req-rr-1", article_id="roundup-1", result_status="DRAFT", gate={"passed": False})
    requests.fail(mysql, "rr-2", run_id=None, error="x" * 5000)
    (finish_sql, finish_params), (fail_sql, fail_params) = mysql.executed
    assert "SET status = 'DONE'" in finish_sql
    assert finish_params == ("req-rr-1", "roundup-1", "DRAFT", json.dumps({"passed": False}), "rr-1")
    assert "SET status = 'FAILED'" in fail_sql and len(fail_params[1]) == 2000 and fail_params[2] == "rr-2"
    assert mysql.commits == 2


def test_check_table_explains_a_missing_table() -> None:
    requests.check_table(FakeMySql(results=[[]]))  # table present: no error
    with pytest.raises(requests.RequestQueueMissing, match="Deploy reviewgate first"):
        requests.check_table(FakeMySql(fail_execute=True))


@needs_pg
def test_list_recent_describes_requests_from_dealvant(pg) -> None:
    now = datetime(2026, 10, 7, 12, 0)
    rows = [
        {"id": "rr-2", "category_id": "c1", "offer_ids_json": '["o3", "o1"]', "requested_by": "ed", "status": "DONE",
         "attempts": 1, "result_status": "PUBLISHED", "gate_json": '{"passed": true, "seo_score": 90}', "error": None,
         "article_id": "roundup-1", "created_at": now, "started_at": now, "finished_at": now},
        {"id": "rr-1", "category_id": "c1", "offer_ids_json": '["gone", "o2"]', "requested_by": "ed", "status": "QUEUED",
         "attempts": 0, "result_status": None, "gate_json": None, "error": None,
         "article_id": None, "created_at": now, "started_at": None, "finished_at": None},
    ]
    mysql = FakeMySql(results=[rows])
    listed = requests.list_recent(mysql, pg, limit=5)
    assert mysql.executed[0][1] == (5,) and "ORDER BY id DESC" in mysql.executed[0][0]
    done, queued = listed
    assert done["category_name"] == "Home" and done["offer_titles"] == ["Offer o3", "Offer o1"]
    assert done["gate"] == {"passed": True, "seo_score": 90}
    assert (done["article_slug"], done["article_title"], done["article_status"]) == ("kettle-roundup", "Kettle roundup", "PUBLISHED")
    assert queued["offer_titles"] == ["gone", "Offer o2"]  # a deleted offer shows its id
    assert queued["article_slug"] is None
    assert requests.list_recent(FakeMySql(results=[[]]), pg) == []
