"""Tests for the shared Dealvant write path (common_utils.dealvant: store,
products, roundups).

The database tests need a Postgres with dealvant/db/schema.sql applied and
its URI in DEALVANT_TEST_PG_URI (a throwaway database: they write rows);
they are skipped otherwise.
"""

from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone

import pytest

from common_utils.dealvant import store
from common_utils.dealvant.products import is_foreign_price, parse_usd_cents, product_from_scan
from common_utils.dealvant.roundups import (
    LINK_TOKEN,
    assemble_roundup,
    build_user_payload,
    stable_article_id,
    structural_problems,
)

NOW = datetime(2026, 10, 7, 6, 0, tzinfo=timezone.utc)


# ---------------------------------------------------------- dealvant.products


@pytest.mark.parametrize(
    ("text", "cents"),
    [
        ("$1,299.99", 129999),
        ("$19.99 - $29.99", 1999),
        ("USD 12.50", 1250),
        ("US$5", 500),
        ("12.99", 1299),
        ("EUR6.45", None),
        ("C$12.00", None),
        ("£9.99", None),
        ("", None),
        ("See price in cart", None),
        (None, None),
        ("$0.00", None),
    ],
)
def test_parse_usd_cents(text, cents) -> None:
    assert parse_usd_cents(text) == cents


def test_is_foreign_price_keeps_us_dollars() -> None:
    assert not is_foreign_price("$5.00")
    assert not is_foreign_price("USD5.00")
    assert is_foreign_price("ARS83,971.00")
    assert is_foreign_price("MX$100")


def _scan(**overrides):
    scan = {
        "asin": "b0abc12345",
        "final_url": "https://www.amazon.com/dp/B0ABC12345?th=1",
        "product_title": "Acme Kettle",
        "brand": "Acme",
        "price_text": "$39.99",
        "availability_text": "In Stock",
        "rating_text": "4.5 out of 5 stars",
        "review_count_text": "1,234 ratings",
        "bullets_json": '["Boils fast", "", "Auto shutoff"]',
        "details_json": '{"Capacity": "1.7 L", "Wattage": "1500 W", "": "x"}',
        "image_urls_json": '["https://m.media-amazon.com/a.jpg", "https://m.media-amazon.com/b.jpg"]',
        "scanned_at": datetime(2026, 10, 7, 6, 0),
    }
    scan.update(overrides)
    return scan


def test_product_from_scan_parses_json_columns_and_price() -> None:
    product = product_from_scan(_scan())
    assert product is not None
    assert product.id == "amazon:amazon.com:B0ABC12345"
    assert product.bullets == ["Boils fast", "Auto shutoff"]
    assert product.specs == {"Capacity": "1.7 L", "Wattage": "1500 W"}
    assert product.image_url == "https://m.media-amazon.com/a.jpg"
    assert (product.price_cents, product.currency) == (3999, "USD")
    assert product.price_checked_at == NOW  # naive MySQL time read as UTC


def test_product_from_scan_foreign_price_has_no_cents() -> None:
    product = product_from_scan(_scan(price_text="EUR6.45"))
    assert product is not None and product.price_cents is None and product.currency is None
    assert product.price_text == "EUR6.45"


def test_product_from_scan_rejects_block_pages_and_bad_asins() -> None:
    assert product_from_scan(_scan(product_title="")) is None
    assert product_from_scan(_scan(asin="not-an-asin")) is None
    assert product_from_scan(_scan(product_title=""), fallback_title="From deal") is not None


# ---------------------------------------------------------- dealvant.roundups

OFFERS = [
    {"id": "o1", "title": "Kettle", "slug": "kettle", "merchant_name": "Amazon", "short_description": "A kettle."},
    {"id": "o2", "title": "Toaster", "slug": "toaster", "merchant_name": "Amazon", "short_description": "A toaster."},
    {"id": "o3", "title": "Blender", "slug": "blender", "merchant_name": "Amazon", "short_description": "A <b>blender</b>."},
]
CATEGORY = {"id": "cat-home", "name": "Home", "slug": "home"}


def _result(**overrides):
    result = {
        "title": "Home Deals",
        "sections": [
            {"offer_id": "o2", "pick_label": "best value", "verdict": "Cheap.", "heading": "T", "paragraphs": ["Hi", f"Buy {LINK_TOKEN}"]},
            {"offer_id": "o1", "pick_label": "Best value", "verdict": "Fast.", "heading": "K", "paragraphs": [f"<script>x</script> {LINK_TOKEN}"]},
        ],
        "faq": [{"question": "Q?", "answer": "A."}, {"question": "", "answer": "dropped"}],
    }
    result.update(overrides)
    return result


def test_assemble_roundup_links_escapes_and_covers_every_offer() -> None:
    record = assemble_roundup(category=CATEGORY, offers=OFFERS, result=_result(), base_url="https://dealvant.com/")
    assert [p.offer_id for p in record.picks] == ["o2", "o1", "o3"]
    for offer in OFFERS:
        assert record.body_html.count(f'href="/offers/{offer["slug"]}"') == 1
    assert "<script>" not in record.body_html and "&lt;script&gt;" in record.body_html
    assert "&lt;b&gt;blender" in record.body_html  # fallback section is escaped too
    assert record.status == "DRAFT"
    assert record.id == stable_article_id("home", ["o1", "o2", "o3"])
    faq = [g for g in record.structured_data["@graph"] if g["@type"] == "FAQPage"][0]
    assert len(faq["mainEntity"]) == 1


def test_assemble_roundup_repairs_a_wrong_token_count() -> None:
    result = _result(sections=[{"offer_id": "o1", "paragraphs": [f"{LINK_TOKEN} and {LINK_TOKEN}", "End."]}])
    record = assemble_roundup(category=CATEGORY, offers=OFFERS[:2], result=result, base_url="https://x")
    assert record.body_html.count('href="/offers/kettle"') == 1
    assert LINK_TOKEN not in record.body_html


def test_clean_pick_labels_normalizes_and_dedupes() -> None:
    record = assemble_roundup(category=CATEGORY, offers=OFFERS, result=_result(), base_url="https://x")
    labels = [p.pick_label for p in store.clean_pick_labels(record.picks)]
    # "best value" -> canonical; the second "Best value" is a repeat; the
    # skipped offer gets the repeatable "Also great".
    assert labels == ["Best value", None, "Also great"]
    picks = [store.RoundupPick("a", "Also great"), store.RoundupPick("b", "also GREAT"), store.RoundupPick("c", "Top pick!")]
    assert [p.pick_label for p in store.clean_pick_labels(picks)] == ["Also great", "Also great", None]


def test_structural_problems() -> None:
    assert structural_problems(_result(), OFFERS[:2]) == []
    problems = structural_problems(_result(), OFFERS)
    assert problems == ["no section for offer o3"]
    bad = _result(sections=[{"offer_id": "o1", "paragraphs": ["no token"]}, {"offer_id": "zz", "paragraphs": []}])
    assert structural_problems(bad, OFFERS[:2]) == [
        "offer o1: link token appears 0 times, expected 1",
        "section for unknown offer id 'zz'",
        "no section for offer o2",
    ]


def test_build_user_payload_carries_facts_and_keywords() -> None:
    offers = [dict(OFFERS[0], price_text="$10", pros=["Fast"]), OFFERS[1]]
    payload = build_user_payload("Home", offers, target_keywords=["best kettle"])
    assert payload["offers"][0]["price_text"] == "$10" and payload["offers"][0]["pros"] == ["Fast"]
    assert "price_text" not in payload["offers"][1]
    assert payload["target_keywords"] == ["best kettle"]
    assert "pick_label" in payload["output_schema"]["sections"][0]


def test_assemble_roundup_needs_two_offers() -> None:
    with pytest.raises(ValueError):
        assemble_roundup(category=CATEGORY, offers=OFFERS[:1], result={}, base_url="https://x")


# ---------------------------------------------------------- dealvant.store (real Postgres)

PG_URI = os.environ.get("DEALVANT_TEST_PG_URI")
needs_pg = pytest.mark.skipif(not PG_URI, reason="DEALVANT_TEST_PG_URI not set")


@pytest.fixture()
def conn():
    psycopg = pytest.importorskip("psycopg")
    from psycopg.rows import dict_row

    with psycopg.connect(PG_URI, row_factory=dict_row) as connection:
        with connection.cursor() as cursor:
            cursor.execute("TRUNCATE price_observations, products, article_offers, articles, offers, merchants, categories CASCADE")
            cursor.execute(
                "INSERT INTO categories (id, name, slug, description, accent_color) VALUES ('cat-home', 'Home', 'home', 'd', '#000')"
            )
            cursor.execute(
                """
                INSERT INTO merchants (id, name, slug, tagline, description, website_url, tracking_url, hero_image,
                                       brand_color, commission_rate, category_id)
                VALUES ('m1', 'Amazon', 'amazon', 't', 'd', 'https://amazon.com', '', '/i.svg', '#000', 0, 'cat-home')
                """
            )
        yield connection
        connection.rollback()


def _offer(**overrides) -> store.OfferRecord:
    fields = dict(
        id="o1", title="Kettle deal", slug="kettle-deal", short_description="s", long_description="l", terms="t",
        cashback_percent=0, status="LIVE", is_featured=False, merchant_id="m1", category_id="cat-home",
        amazon_url="https://www.amazon.com/dp/B0ABC12345", source_name="dealnews", meta_title="MT",
    )
    fields.update(overrides)
    return store.OfferRecord(**fields)


def _row(conn, sql, *params):
    with conn.cursor() as cursor:
        cursor.execute(sql, params)
        return cursor.fetchone()


@needs_pg
def test_require_schema(conn) -> None:
    store.require_schema(conn)
    with pytest.raises(store.DealvantSchemaError, match=r"schema version \d+, this publisher needs 99"):
        store.require_schema(conn, 99)


@needs_pg
def test_upsert_offer_keeps_fields_other_writers_set(conn) -> None:
    store.upsert_offer(conn, _offer(pros=["Fast"], verdict="Good", embedding="[" + ",".join(["0.1"] * 512) + "]"))
    # A second writer without meta_title, embedding or guide fields.
    store.upsert_offer(conn, _offer(title="New title", meta_title=None, source_name=None))
    row = _row(conn, "SELECT title, meta_title, source_name, pros, verdict, embedding IS NOT NULL AS has_emb FROM offers WHERE id='o1'")
    assert row == {"title": "New title", "meta_title": "MT", "source_name": "dealnews", "pros": ["Fast"], "verdict": "Good", "has_emb": True}
    # An empty list clears.
    store.upsert_offer(conn, _offer(pros=[]))
    assert _row(conn, "SELECT pros FROM offers WHERE id='o1'")["pros"] == []


@needs_pg
def test_removed_deals_stay_hidden_on_republish(conn) -> None:
    store.upsert_offer(conn, _offer())
    store.upsert_article(conn, store.ArticleRecord(
        id="a1", title="t", slug="a1", excerpt="e", body="b", status="PUBLISHED", is_featured=False,
        featured_rank=100, hero_image="/i.svg", published_at=NOW, category_id="cat-home", merchant_id="m1",
    ))
    with conn.cursor() as cursor:
        cursor.execute("UPDATE offers SET status='ENDED', removed_at=NOW() WHERE id='o1'")
        cursor.execute("UPDATE articles SET status='DRAFT', removed_at=NOW() WHERE id='a1'")
    store.upsert_offer(conn, _offer(status="LIVE"))
    store.upsert_article(conn, store.ArticleRecord(
        id="a1", title="t2", slug="a1", excerpt="e", body="b", status="PUBLISHED", is_featured=False,
        featured_rank=100, hero_image="/i.svg", published_at=NOW, category_id="cat-home",
    ))
    assert _row(conn, "SELECT status FROM offers WHERE id='o1'")["status"] == "ENDED"
    assert _row(conn, "SELECT status, title FROM articles WHERE id='a1'") == {"status": "DRAFT", "title": "t2"}


@needs_pg
def test_upsert_product_and_price_observations(conn) -> None:
    product = product_from_scan(_scan())
    store.upsert_product(conn, product)
    store.upsert_offer(conn, _offer(product_id=product.id))
    count = lambda: _row(conn, "SELECT COUNT(*) AS n FROM price_observations")["n"]  # noqa: E731
    assert count() == 1
    # Same price an hour later: no new observation. A day later: one.
    store.upsert_product(conn, product_from_scan(_scan(scanned_at=datetime(2026, 10, 7, 7, 0))))
    assert count() == 1
    store.upsert_product(conn, product_from_scan(_scan(scanned_at=datetime(2026, 10, 8, 7, 0))))
    assert count() == 2
    # Price change: new observation, product updated.
    store.upsert_product(conn, product_from_scan(_scan(price_text="$29.99", scanned_at=datetime(2026, 10, 8, 8, 0))))
    assert count() == 3
    assert _row(conn, "SELECT price_cents FROM products")["price_cents"] == 2999
    # An older scan never overwrites the product or adds an observation.
    store.upsert_product(conn, product_from_scan(_scan(price_text="$99.99", scanned_at=datetime(2026, 10, 1))))
    assert _row(conn, "SELECT price_cents FROM products")["price_cents"] == 2999
    assert count() == 3
    assert _row(conn, "SELECT product_id FROM offers WHERE id='o1'")["product_id"] == product.id
    # Specs keep the page's order (a jsonb object would sort its keys).
    assert _row(conn, "SELECT specs FROM products")["specs"] == [
        {"name": "Capacity", "value": "1.7 L"},
        {"name": "Wattage", "value": "1500 W"},
    ]


@needs_pg
def test_upsert_roundup_keeps_status_and_replaces_picks(conn) -> None:
    for oid in ("o1", "o2", "o3"):
        store.upsert_offer(conn, _offer(id=oid, slug=oid))
    record = assemble_roundup(category=CATEGORY, offers=OFFERS, result=_result(), base_url="https://x")
    store.upsert_roundup(conn, record)
    assert store.set_roundup_status(conn, record.id, "PUBLISHED") == 1
    # Re-run with only two offers: still PUBLISHED, o3 no longer a pick.
    record2 = assemble_roundup(category=CATEGORY, offers=OFFERS[:2], result=_result(), base_url="https://x")
    record2.id = record.id
    store.upsert_roundup(conn, record2)
    assert _row(conn, "SELECT status FROM articles WHERE id=%s", record.id)["status"] == "PUBLISHED"
    with conn.cursor() as cursor:
        cursor.execute("SELECT offer_id, position, pick_label FROM article_offers WHERE article_id=%s ORDER BY position", (record.id,))
        rows = cursor.fetchall()
    assert rows == [
        {"offer_id": "o2", "position": 1, "pick_label": "Best value"},
        {"offer_id": "o1", "position": 2, "pick_label": None},
    ]


@needs_pg
def test_search_tsv_is_generated(conn) -> None:
    store.upsert_offer(conn, _offer(title="Stainless steel electric kettle"))
    row = _row(conn, "SELECT id FROM offers WHERE search_tsv @@ websearch_to_tsquery('english', 'kettles')")
    assert row == {"id": "o1"}
