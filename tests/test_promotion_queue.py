"""common_utils.dealvant.promotion_queue: a queued affiliate promotion (here
an Awin one) turned into a Dealvant offer. MySQL is faked (one queue row); Dealvant is a throwaway
Postgres with dealvant/db/schema.sql applied, its URI in
DEALVANT_TEST_PG_URI (skipped otherwise)."""

from __future__ import annotations

import os
from datetime import datetime, timezone

import pytest

from common_utils.dealvant import promotion_queue

PG_URI = os.environ.get("DEALVANT_TEST_PG_URI")


def _row(**overrides):
    row = {
        "network": "awin",
        "promotion_id": "4242",
        "status": "READY",
        "active": 1,
        "advertiser_id": 11,
        "advertiser_name": "Brooks Running US",
        "merchant_name": "Brooks Running",
        "title": "20% off shoes",
        "terms": "Excludes sale items.",
        "voucher_code": "RUN20",
        "end_date": datetime(2026, 11, 30),
        "url": "https://www.brooksrunning.com/sale",
        "url_tracking": "https://www.awin1.com/cread.php?awinmid=11&awinaffid=99",
        "display_url": "www.brooksrunning.com",
        "click_through_url": "https://www.awin1.com/cread.php?awinmid=11&awinaffid=99&p=home",
        "logo_url": "https://ui.awin.com/logo.png",
        "image_url": "https://www.brooksrunning.com/og.jpg",
        "draft_title": "20% off running shoes at Brooks",
        "draft_short_description": "Use RUN20.",
        "draft_long_description": "Brooks takes **20% off**.",
        "draft_category_slug": "home",
        "draft_best_for": "runners",
        "draft_meta_title": None,
        "draft_meta_description": None,
        "raw_json": None,
    }
    row.update(overrides)
    return row


class FakeMysql:
    """One affiliate_promotions row: answers get_promotion's SELECT and records
    UPDATEs."""

    def __init__(self, row):
        self.row = row
        self.updates: list[tuple[str, tuple]] = []

    def cursor(self, dictionary=False):
        conn = self

        class Cursor:
            rowcount = 1

            def execute(self, sql, params=()):
                self._sql = sql
                if sql.lstrip().upper().startswith("UPDATE"):
                    conn.updates.append((sql, params))

            def fetchall(self):
                return [dict(conn.row)] if "SELECT p.*" in self._sql else []

            def close(self):
                pass

        return Cursor()

    def commit(self):
        pass


def test_draft_from_row_maps_awin_fields() -> None:
    draft = promotion_queue.draft_from_row(_row(), category_id="cat-home")
    assert (draft.network, draft.promotion_id, draft.merchant_name) == ("awin", "4242", "Brooks Running")
    assert draft.merchant_website_url == "https://www.brooksrunning.com"
    assert draft.merchant_tracking_url.endswith("p=home")
    assert draft.affiliate_url == "https://www.awin1.com/cread.php?awinmid=11&awinaffid=99"
    assert draft.coupon_code == "RUN20"
    assert draft.expires_at == datetime(2026, 11, 30, tzinfo=timezone.utc)
    assert promotion_queue.draft_from_row(_row(display_url=None), category_id="c").merchant_website_url == "https://www.brooksrunning.com"
    # Rakuten: no display URL and no plain page link -> the tracking link.
    rakuten = _row(network="rakuten", display_url=None, url=None, url_tracking="https://click.linksynergy.com/fs-bin/click?id=x&offerid=1.2")
    assert promotion_queue.draft_from_row(rakuten, category_id="c").merchant_website_url == rakuten["url_tracking"]

    with pytest.raises(promotion_queue.QueueError, match="title"):
        promotion_queue.draft_from_row(_row(draft_title=" "), category_id="c")
    with pytest.raises(promotion_queue.QueueError, match="tracking link"):
        promotion_queue.draft_from_row(_row(url_tracking=None), category_id="c")


def test_save_draft_only_takes_editable_fields_and_https_images() -> None:
    mysql = FakeMysql(_row())
    promotion_queue.save_draft(mysql, "awin", "4242", {"draft_title": " New title ", "status": "PUBLISHED", "draft_best_for": ""})
    sql, params = mysql.updates[-1]
    assert "draft_title = %s" in sql and "status" not in sql.split("WHERE")[0]
    assert params == ("New title", None, "awin", "4242")
    with pytest.raises(promotion_queue.QueueError, match="https"):
        promotion_queue.save_draft(mysql, "awin", "4242", {"image_url": "http://x.test/a.jpg"})


@pytest.fixture()
def pg():
    if not PG_URI:
        pytest.skip("DEALVANT_TEST_PG_URI not set")
    import psycopg
    from psycopg.rows import dict_row

    with psycopg.connect(PG_URI, row_factory=dict_row) as connection:
        with connection.cursor() as cursor:
            cursor.execute("TRUNCATE article_offers, articles, offers, merchants, categories CASCADE")
            cursor.execute(
                "INSERT INTO categories (id, name, slug, description, accent_color) VALUES ('cat-home', 'Home', 'home', 'd', '#000')"
            )
        connection.commit()
        yield connection


def test_publish_writes_the_offer_and_records_it(pg) -> None:
    mysql = FakeMysql(_row())
    published = promotion_queue.publish(mysql, pg, "awin", "4242", reviewed_by="sam")
    with pg.cursor() as cursor:
        cursor.execute("SELECT o.title, o.affiliate_url, o.source_name, m.name FROM offers o JOIN merchants m ON m.id = o.merchant_id")
        offer = cursor.fetchone()
    assert offer == {
        "title": "20% off running shoes at Brooks",
        "affiliate_url": "https://www.awin1.com/cread.php?awinmid=11&awinaffid=99",
        "source_name": "awin",
        "name": "Brooks Running",
    }
    sql, params = mysql.updates[-1]
    assert "status = 'PUBLISHED'" in sql
    assert params[0] == published.offer_id and params[1] == published.slug and params[3] == "sam"
    assert params[-2:] == ("awin", "4242")


def test_live_deals_elsewhere_finds_the_same_store_from_other_sources(pg) -> None:
    promotion_queue.publish(FakeMysql(_row()), pg, "awin", "4242", reviewed_by="sam")
    cj = _row(network="cj", promotion_id="77", url_tracking="https://www.anrdoezrs.net/click-1-2", draft_title="Brooks sale on CJ")
    promotion_queue.publish(FakeMysql(cj), pg, "cj", "77", reviewed_by="sam")
    elsewhere = promotion_queue.live_deals_elsewhere(pg, "Brooks Running", "cj")
    assert [(d["source_name"], d["title"]) for d in elsewhere] == [("awin", "20% off running shoes at Brooks")]
    assert promotion_queue.live_deals_elsewhere(pg, "Someone Else", "cj") == []


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"status": "REJECTED"}, "REJECTED"),
        ({"active": 0}, "no longer lists"),
        ({"draft_category_slug": None}, "category"),
        ({"draft_category_slug": "nope"}, "no category"),
    ],
)
def test_publish_refuses(pg, overrides, message) -> None:
    with pytest.raises(promotion_queue.QueueError, match=message):
        promotion_queue.publish(FakeMysql(_row(**overrides)), pg, "awin", "4242", reviewed_by="sam")
