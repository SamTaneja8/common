"""The one place that writes deals into Dealvant's Postgres.

amazonnew's publish pipeline, reviewgate's publish and roundup pages,
autopub's blogger and the affiliate repo's network promotions
(promotions.py) all publish into the same tables (dealvant/db/schema.sql)
over a direct psycopg connection. Before this module each kept its own copy
of the INSERT/UPDATE statements, so a new column meant editing every copy.

Conventions every writer here follows:

- Upserts are keyed on the caller's deterministic id, so re-running a
  publish is safe.
- A deal removed on Dealvant (removed_at set by reviewgate's
  scripts/dealvant_deals.py) keeps its hidden status on republish.
- A deal that ended on its own (end_offers: expired at the source or
  unavailable on Amazon) is ENDED with ended_at set and removed_at NULL,
  so Dealvant can keep its page up with an "ended" notice. Republishing it
  as LIVE clears ended_at.
- Optional fields passed as None leave the stored value alone; an empty
  list or string clears it. That lets one writer (e.g. reviewgate, which
  has no offer meta_title) update a row another writer created without
  wiping fields it doesn't know about.
- Nothing here commits. Callers own the transaction.

Callers pass a psycopg 3 connection; the module needs no other dependency
(common_utils declares none -- see pyproject.toml).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Sequence

from psycopg.types.json import Jsonb

# Dealvant schema_meta.schema_version this module writes against. Raise it
# together with dealvant/db/schema.sql whenever a writer here starts using
# a new column.
REQUIRED_SCHEMA_VERSION = 5

# A price observation is recorded when the price or availability changes, or
# when the last one is at least this old, so a steady price still leaves one
# data point per day.
PRICE_OBSERVATION_INTERVAL = timedelta(hours=24)

# Labels a roundup pick may carry. Anything else from a model is dropped.
PICK_LABELS = ("Best overall", "Best value", "Upgrade pick", "Also great")
_REPEATABLE_PICK_LABELS = {"Also great"}


class DealvantSchemaError(RuntimeError):
    """Dealvant's database is older than this module expects."""


@dataclass
class ProductRecord:
    id: str
    marketplace: str
    asin: str
    title: str
    brand: str | None = None
    image_url: str | None = None
    image_urls: list[str] = field(default_factory=list)
    bullets: list[str] = field(default_factory=list)
    specs: dict[str, str] = field(default_factory=dict)  # stored as ordered [{"name", "value"}]
    rating_text: str | None = None
    review_count_text: str | None = None
    price_cents: int | None = None
    currency: str | None = None
    price_text: str | None = None
    availability_text: str | None = None
    price_checked_at: datetime | None = None
    price_source: str = "scan"


@dataclass
class OfferRecord:
    id: str
    title: str
    slug: str
    short_description: str
    long_description: str
    terms: str
    cashback_percent: Any
    status: str
    is_featured: bool
    merchant_id: str
    category_id: str
    coupon_code: str | None = None
    amazon_url: str | None = None
    structured_data: dict[str, Any] | None = None
    embedding: str | None = None  # pgvector literal, see embeddings.vector_literal
    source_name: str | None = None
    meta_title: str | None = None
    meta_description: str | None = None
    product_id: str | None = None
    pros: list[str] | None = None
    cons: list[str] | None = None
    verdict: str | None = None
    best_for: str | None = None
    # Deals from other affiliate networks (the affiliate repo): the
    # offer's own tracking link, which dealvant's /go uses ahead of the
    # merchant's, and its picture when there's no scanned product. None
    # leaves them alone.
    affiliate_url: str | None = None
    image_url: str | None = None
    expires_at: datetime | None = None


@dataclass
class MerchantRecord:
    id: str
    name: str
    slug: str
    tagline: str
    description: str
    website_url: str
    tracking_url: str
    hero_image: str
    brand_color: str
    category_id: str
    commission_rate: Any = 0


@dataclass
class ArticleRecord:
    id: str
    title: str
    slug: str
    excerpt: str
    body: str
    status: str
    is_featured: bool
    featured_rank: int
    hero_image: str
    published_at: datetime
    category_id: str
    merchant_id: str | None = None
    meta_title: str | None = None
    meta_description: str | None = None
    structured_data: dict[str, Any] | None = None
    # True when an editor reviewed it before publishing (reviewgate). None
    # (automatic publishers) leaves the stored value alone, so a republish
    # never clears an earlier review.
    reviewed_by_editor: bool | None = None


@dataclass
class RoundupPick:
    offer_id: str
    pick_label: str | None = None
    verdict: str | None = None


@dataclass
class RoundupRecord:
    id: str
    title: str
    slug: str
    excerpt: str
    body_html: str
    hero_image: str
    category_id: str
    meta_title: str
    meta_description: str
    structured_data: dict[str, Any] | None
    picks: list[RoundupPick]
    # Only used when the roundup is new. An existing roundup keeps its status,
    # so a re-run over the same offers never silently unpublishes a live one.
    status: str = "DRAFT"


def _jsonb(value: Any) -> Jsonb | None:
    return Jsonb(value) if value is not None else None


def schema_version(conn) -> int:
    """Dealvant's schema_meta.schema_version, or 0 before it existed."""
    with conn.cursor() as cursor:
        cursor.execute("SELECT to_regclass('schema_meta') IS NOT NULL")
        row = cursor.fetchone()
        exists = row[0] if isinstance(row, tuple) else next(iter(row.values()))
        if not exists:
            return 0
        cursor.execute("SELECT value FROM schema_meta WHERE key = 'schema_version'")
        row = cursor.fetchone()
    if row is None:
        return 0
    value = row[0] if isinstance(row, tuple) else row["value"]
    return int(value)


def require_schema(conn, min_version: int = REQUIRED_SCHEMA_VERSION) -> None:
    """Raises DealvantSchemaError unless Dealvant's schema is at least
    min_version. Call once per connection, before writing."""
    found = schema_version(conn)
    if found < min_version:
        raise DealvantSchemaError(
            f"Dealvant's database is at schema version {found}, this publisher needs {min_version}. "
            "Apply dealvant's schema first (on VPS2: deal-pipeline/scripts/deploy.sh dealvant --apply, "
            "which runs db:init), then publish again."
        )


def clean_pick_labels(picks: Sequence[RoundupPick]) -> list[RoundupPick]:
    """Drops labels outside PICK_LABELS and repeats of single-use labels
    (first one wins), keeping the picks themselves and their order."""
    used: set[str] = set()
    cleaned: list[RoundupPick] = []
    for pick in picks:
        label = (pick.pick_label or "").strip()
        canonical = next((known for known in PICK_LABELS if known.lower() == label.lower()), None)
        if canonical is not None and canonical not in _REPEATABLE_PICK_LABELS and canonical in used:
            canonical = None
        if canonical is not None:
            used.add(canonical)
        verdict = (pick.verdict or "").strip() or None
        cleaned.append(RoundupPick(offer_id=pick.offer_id, pick_label=canonical, verdict=verdict))
    return cleaned


def upsert_product(conn, product: ProductRecord) -> None:
    checked_at = product.price_checked_at or datetime.now(timezone.utc)
    with conn.cursor() as cursor:
        cursor.execute(
            """
            INSERT INTO products (
                id, marketplace, asin, title, brand, image_url, image_urls, bullets, specs,
                rating_text, review_count_text, price_cents, currency, price_text,
                availability_text, price_checked_at, price_source
            )
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (id) DO UPDATE
            SET title = EXCLUDED.title,
                brand = COALESCE(EXCLUDED.brand, products.brand),
                image_url = COALESCE(EXCLUDED.image_url, products.image_url),
                image_urls = EXCLUDED.image_urls,
                bullets = EXCLUDED.bullets,
                specs = EXCLUDED.specs,
                rating_text = COALESCE(EXCLUDED.rating_text, products.rating_text),
                review_count_text = COALESCE(EXCLUDED.review_count_text, products.review_count_text),
                price_cents = EXCLUDED.price_cents,
                currency = EXCLUDED.currency,
                price_text = EXCLUDED.price_text,
                availability_text = EXCLUDED.availability_text,
                price_checked_at = EXCLUDED.price_checked_at,
                price_source = EXCLUDED.price_source,
                updated_at = NOW()
            -- An older scan never overwrites a newer one.
            WHERE products.price_checked_at IS NULL OR EXCLUDED.price_checked_at >= products.price_checked_at
            """,
            (
                product.id,
                product.marketplace,
                product.asin,
                product.title,
                product.brand,
                product.image_url,
                Jsonb(list(product.image_urls)),
                Jsonb(list(product.bullets)),
                Jsonb([{"name": name, "value": value} for name, value in product.specs.items()]),
                product.rating_text,
                product.review_count_text,
                product.price_cents,
                product.currency,
                product.price_text,
                product.availability_text,
                checked_at,
                product.price_source,
            ),
        )
        cursor.execute(
            """
            SELECT price_cents, currency, availability_text, observed_at
            FROM price_observations
            WHERE product_id = %s
            ORDER BY observed_at DESC
            LIMIT 1
            """,
            (product.id,),
        )
        last = cursor.fetchone()
        if last is not None and not isinstance(last, dict):
            last = dict(zip(("price_cents", "currency", "availability_text", "observed_at"), last))
        unchanged = last is not None and (
            last["price_cents"] == product.price_cents
            and last["currency"] == product.currency
            and last["availability_text"] == product.availability_text
        )
        if last is not None and checked_at <= last["observed_at"]:
            return
        if unchanged and checked_at - last["observed_at"] < PRICE_OBSERVATION_INTERVAL:
            return
        cursor.execute(
            """
            INSERT INTO price_observations (product_id, observed_at, price_cents, currency, availability_text, source)
            VALUES (%s, %s, %s, %s, %s, %s)
            ON CONFLICT (product_id, observed_at) DO NOTHING
            """,
            (product.id, checked_at, product.price_cents, product.currency, product.availability_text, product.price_source),
        )


def upsert_offer(conn, offer: OfferRecord) -> None:
    with conn.cursor() as cursor:
        cursor.execute(
            """
            INSERT INTO offers (
                id, title, slug, short_description, long_description, terms,
                cashback_percent, coupon_code, status, is_featured, merchant_id, category_id,
                amazon_url, structured_data_json, embedding, source_name,
                meta_title, meta_description, product_id, pros, cons, verdict, best_for,
                affiliate_url, image_url, expires_at
            )
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s::vector, %s, %s, %s, %s, %s, %s, %s, %s,
                    %s, %s, %s)
            ON CONFLICT (id) DO UPDATE
            SET title = EXCLUDED.title,
                slug = EXCLUDED.slug,
                short_description = EXCLUDED.short_description,
                long_description = EXCLUDED.long_description,
                terms = EXCLUDED.terms,
                cashback_percent = EXCLUDED.cashback_percent,
                coupon_code = EXCLUDED.coupon_code,
                -- A deal removed on Dealvant keeps its hidden status on republish.
                status = CASE WHEN offers.removed_at IS NULL THEN EXCLUDED.status ELSE offers.status END,
                ended_at = CASE WHEN offers.removed_at IS NULL AND EXCLUDED.status = 'LIVE' THEN NULL ELSE offers.ended_at END,
                ended_reason = CASE WHEN offers.removed_at IS NULL AND EXCLUDED.status = 'LIVE' THEN NULL ELSE offers.ended_reason END,
                is_featured = EXCLUDED.is_featured,
                merchant_id = EXCLUDED.merchant_id,
                category_id = EXCLUDED.category_id,
                amazon_url = EXCLUDED.amazon_url,
                structured_data_json = EXCLUDED.structured_data_json,
                embedding = COALESCE(EXCLUDED.embedding, offers.embedding),
                source_name = COALESCE(EXCLUDED.source_name, offers.source_name),
                meta_title = COALESCE(EXCLUDED.meta_title, offers.meta_title),
                meta_description = COALESCE(EXCLUDED.meta_description, offers.meta_description),
                product_id = COALESCE(EXCLUDED.product_id, offers.product_id),
                pros = COALESCE(EXCLUDED.pros, offers.pros),
                cons = COALESCE(EXCLUDED.cons, offers.cons),
                verdict = COALESCE(EXCLUDED.verdict, offers.verdict),
                best_for = COALESCE(EXCLUDED.best_for, offers.best_for),
                affiliate_url = COALESCE(EXCLUDED.affiliate_url, offers.affiliate_url),
                image_url = COALESCE(EXCLUDED.image_url, offers.image_url),
                expires_at = COALESCE(EXCLUDED.expires_at, offers.expires_at),
                updated_at = NOW()
            """,
            (
                offer.id,
                offer.title,
                offer.slug,
                offer.short_description,
                offer.long_description,
                offer.terms,
                offer.cashback_percent,
                offer.coupon_code,
                offer.status,
                offer.is_featured,
                offer.merchant_id,
                offer.category_id,
                offer.amazon_url,
                _jsonb(offer.structured_data),
                offer.embedding,
                offer.source_name,
                offer.meta_title,
                offer.meta_description,
                offer.product_id,
                _jsonb(offer.pros),
                _jsonb(offer.cons),
                offer.verdict,
                offer.best_for,
                offer.affiliate_url,
                offer.image_url,
                offer.expires_at,
            ),
        )


def ensure_merchant(conn, merchant: MerchantRecord) -> str:
    """The id of the merchant with merchant.slug, creating it from `merchant`
    when there's none. An existing merchant is left as it is (an editor may
    have changed it in reviewgate)."""
    with conn.cursor() as cursor:
        cursor.execute("SELECT id FROM merchants WHERE slug = %s", (merchant.slug,))
        row = cursor.fetchone()
        if row is not None:
            return row[0] if isinstance(row, tuple) else row["id"]
        cursor.execute(
            """
            INSERT INTO merchants (
                id, name, slug, tagline, description, website_url, tracking_url,
                hero_image, brand_color, commission_rate, is_featured, category_id
            )
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, FALSE, %s)
            ON CONFLICT (id) DO NOTHING
            """,
            (
                merchant.id,
                merchant.name,
                merchant.slug,
                merchant.tagline,
                merchant.description,
                merchant.website_url,
                merchant.tracking_url,
                merchant.hero_image,
                merchant.brand_color,
                merchant.commission_rate,
                merchant.category_id,
            ),
        )
    return merchant.id


def end_offers(conn, offer_ids: Sequence[str], reason: str) -> list[str]:
    """Marks live offers as naturally ended (ENDED, ended_at, ended_reason)
    and returns the ids that changed. Removed offers (removed_at set) and
    offers that aren't LIVE are left alone."""
    if not offer_ids:
        return []
    with conn.cursor() as cursor:
        cursor.execute(
            """
            UPDATE offers
            SET status = 'ENDED', ended_at = NOW(), ended_reason = %s, updated_at = NOW()
            WHERE id = ANY(%s) AND status = 'LIVE' AND removed_at IS NULL
            RETURNING id
            """,
            (reason[:200], list(offer_ids)),
        )
        rows = cursor.fetchall()
    return [row[0] if isinstance(row, tuple) else row["id"] for row in rows]


def upsert_article(conn, article: ArticleRecord) -> None:
    with conn.cursor() as cursor:
        cursor.execute(
            """
            INSERT INTO articles (
                id, title, slug, excerpt, body, status, is_featured, featured_rank,
                hero_image, published_at, category_id, merchant_id, meta_title, meta_description,
                structured_data_json, reviewed_by_editor
            )
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, COALESCE(%s, FALSE))
            ON CONFLICT (id) DO UPDATE
            SET title = EXCLUDED.title,
                slug = EXCLUDED.slug,
                excerpt = EXCLUDED.excerpt,
                body = EXCLUDED.body,
                -- A deal removed on Dealvant keeps its hidden status on republish.
                status = CASE WHEN articles.removed_at IS NULL THEN EXCLUDED.status ELSE articles.status END,
                is_featured = EXCLUDED.is_featured,
                featured_rank = EXCLUDED.featured_rank,
                hero_image = EXCLUDED.hero_image,
                published_at = EXCLUDED.published_at,
                category_id = EXCLUDED.category_id,
                merchant_id = EXCLUDED.merchant_id,
                meta_title = COALESCE(EXCLUDED.meta_title, articles.meta_title),
                meta_description = COALESCE(EXCLUDED.meta_description, articles.meta_description),
                structured_data_json = EXCLUDED.structured_data_json,
                reviewed_by_editor = COALESCE(%s, articles.reviewed_by_editor),
                updated_at = NOW()
            """,
            (
                article.id,
                article.title,
                article.slug,
                article.excerpt,
                article.body,
                article.status,
                article.is_featured,
                article.featured_rank,
                article.hero_image,
                article.published_at,
                article.category_id,
                article.merchant_id,
                article.meta_title,
                article.meta_description,
                _jsonb(article.structured_data),
                article.reviewed_by_editor,
                article.reviewed_by_editor,
            ),
        )


def upsert_roundup(conn, roundup: RoundupRecord) -> None:
    """Writes a roundup article and its picks. An existing roundup keeps its
    status; its picks are replaced by this set."""
    picks = clean_pick_labels(roundup.picks)
    with conn.cursor() as cursor:
        cursor.execute(
            """
            INSERT INTO articles (
                id, title, slug, excerpt, body, status, is_featured, featured_rank,
                hero_image, published_at, category_id, merchant_id,
                meta_title, meta_description, structured_data_json, is_roundup
            )
            VALUES (%s, %s, %s, %s, %s, %s, FALSE, 100, %s, NOW(), %s, NULL, %s, %s, %s, TRUE)
            ON CONFLICT (id) DO UPDATE
            SET title = EXCLUDED.title,
                slug = EXCLUDED.slug,
                excerpt = EXCLUDED.excerpt,
                body = EXCLUDED.body,
                hero_image = EXCLUDED.hero_image,
                category_id = EXCLUDED.category_id,
                meta_title = EXCLUDED.meta_title,
                meta_description = EXCLUDED.meta_description,
                structured_data_json = EXCLUDED.structured_data_json,
                updated_at = NOW()
            """,
            (
                roundup.id,
                roundup.title,
                roundup.slug,
                roundup.excerpt,
                roundup.body_html,
                roundup.status,
                roundup.hero_image,
                roundup.category_id,
                roundup.meta_title,
                roundup.meta_description,
                _jsonb(roundup.structured_data),
            ),
        )
        cursor.execute(
            "DELETE FROM article_offers WHERE article_id = %s AND NOT (offer_id = ANY(%s))",
            (roundup.id, [pick.offer_id for pick in picks]),
        )
        for position, pick in enumerate(picks, start=1):
            cursor.execute(
                """
                INSERT INTO article_offers (article_id, offer_id, position, pick_label, verdict)
                VALUES (%s, %s, %s, %s, %s)
                ON CONFLICT (article_id, offer_id) DO UPDATE
                SET position = EXCLUDED.position,
                    pick_label = EXCLUDED.pick_label,
                    verdict = EXCLUDED.verdict
                """,
                (roundup.id, pick.offer_id, position, pick.pick_label, pick.verdict),
            )


def set_roundup_status(conn, article_id: str, status: str, *, reviewed_by_editor: bool = False) -> int:
    """Publishes (stamping published_at) or unpublishes a roundup; returns
    rows changed. reviewed_by_editor=True marks it as reviewed (an editor
    published it on reviewgate). A roundup removed on Dealvant is never
    touched."""
    with conn.cursor() as cursor:
        cursor.execute(
            """
            UPDATE articles
            SET status = %s,
                published_at = CASE WHEN %s = 'PUBLISHED' THEN NOW() ELSE published_at END,
                reviewed_by_editor = reviewed_by_editor OR %s,
                updated_at = NOW()
            WHERE id = %s AND is_roundup AND removed_at IS NULL
            """,
            (status, status, reviewed_by_editor, article_id),
        )
        return cursor.rowcount
