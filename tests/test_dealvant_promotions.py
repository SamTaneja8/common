"""common_utils.dealvant.promotions: an affiliate network promotion (from
the affiliate repo, here Awin) published as a Dealvant offer, with its
merchant.

Needs a throwaway Postgres with dealvant/db/schema.sql applied, its URI in
DEALVANT_TEST_PG_URI; skipped otherwise.
"""

from __future__ import annotations

import os
from datetime import datetime, timezone

import pytest

from common_utils.dealvant import promotions, store

PG_URI = os.environ.get("DEALVANT_TEST_PG_URI")


def _draft(**overrides) -> promotions.PromotionDraft:
    values = dict(
        network="awin",
        promotion_id="777",
        merchant_name="Brooks Running",
        merchant_website_url="https://www.brooksrunning.com",
        merchant_tracking_url="https://www.awin1.com/cread.php?awinmid=1&awinaffid=2",
        merchant_logo_url="https://ui.awin.com/logos/1/logo.png",
        title="20% off running shoes",
        short_description="Save on Ghost and Glycerin.",
        long_description="Brooks takes 20% off.",
        terms="Ends Sunday.",
        category_id="cat-home",
        affiliate_url="https://www.awin1.com/cread.php?awinmid=1&awinaffid=2&ued=https%3A%2F%2Fwww.brooksrunning.com%2Fsale",
        image_url="https://www.brooksrunning.com/og.jpg",
        coupon_code="RUN20",
        expires_at=datetime(2026, 11, 30, tzinfo=timezone.utc),
    )
    values.update(overrides)
    return promotions.PromotionDraft(**values)


def test_slugify_and_ids_are_stable() -> None:
    assert promotions.slugify("Brooks Running!") == "brooks-running"
    assert promotions.slugify("***") == "deal"
    assert promotions.offer_id_for("awin", "777") == promotions.offer_id_for("awin", "777")
    assert promotions.offer_id_for("awin", "777") != promotions.offer_id_for("awin", "778")


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
                "INSERT INTO categories (id, name, slug, description, accent_color)"
                " VALUES ('cat-home', 'Home', 'home', 'd', '#000')"
            )
        connection.commit()
        yield connection


def test_publish_creates_merchant_and_offer_and_republish_updates_it(pg) -> None:
    first = promotions.publish_promotion(pg, _draft())
    pg.commit()
    with pg.cursor() as cursor:
        cursor.execute("SELECT name, slug, website_url, tracking_url, hero_image FROM merchants")
        merchants = cursor.fetchall()
        cursor.execute(
            "SELECT slug, source_name, coupon_code, affiliate_url, image_url, expires_at, status, merchant_id FROM offers"
        )
        offer = cursor.fetchone()
    assert merchants == [
        {
            "name": "Brooks Running",
            "slug": "brooks-running",
            "website_url": "https://www.brooksrunning.com",
            "tracking_url": "https://www.awin1.com/cread.php?awinmid=1&awinaffid=2",
            "hero_image": "https://ui.awin.com/logos/1/logo.png",
        }
    ]
    assert offer["slug"] == first.slug == "20-off-running-shoes"
    assert (offer["source_name"], offer["coupon_code"], offer["status"]) == ("awin", "RUN20", "LIVE")
    assert offer["affiliate_url"].startswith("https://www.awin1.com/")
    assert offer["image_url"] == "https://www.brooksrunning.com/og.jpg"
    assert offer["expires_at"] == datetime(2026, 11, 30, tzinfo=timezone.utc)
    assert offer["merchant_id"] == first.merchant_id

    # Republished with a new title: same offer, same slug, one merchant.
    second = promotions.publish_promotion(pg, _draft(title="25% off running shoes", coupon_code=None))
    pg.commit()
    assert (second.offer_id, second.slug) == (first.offer_id, first.slug)
    with pg.cursor() as cursor:
        cursor.execute("SELECT COUNT(*) AS n FROM merchants")
        assert cursor.fetchone()["n"] == 1
        cursor.execute("SELECT title, coupon_code FROM offers")
        assert cursor.fetchone() == {"title": "25% off running shoes", "coupon_code": None}


def test_existing_merchant_is_reused_and_slugs_stay_unique(pg) -> None:
    store.ensure_merchant(
        pg,
        store.MerchantRecord(
            id="m-existing", name="Brooks Running", slug="brooks-running", tagline="t", description="d",
            website_url="https://brooks.test", tracking_url="https://brooks.test", hero_image="",
            brand_color="#000", category_id="cat-home",
        ),
    )
    a = promotions.publish_promotion(pg, _draft(promotion_id="1"))
    b = promotions.publish_promotion(pg, _draft(promotion_id="2"))
    pg.commit()
    assert a.merchant_id == b.merchant_id == "m-existing"
    assert (a.slug, b.slug) == ("20-off-running-shoes", "20-off-running-shoes-2")


def test_affiliate_url_must_be_a_network_link(pg) -> None:
    import psycopg

    with pytest.raises(psycopg.errors.CheckViolation):
        promotions.publish_promotion(pg, _draft(affiliate_url="https://evil.test/r"))
    pg.rollback()
