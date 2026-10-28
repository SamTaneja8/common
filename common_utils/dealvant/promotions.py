"""Publishing an affiliate network's promotion (a store-wide deal or coupon,
not a single product) as a Dealvant offer.

The affiliate repo collects promotions from Awin, Rakuten and CJ and drafts
their copy; an editor approves each one on reviewgate's /affiliate page,
which publishes it through here (common_utils.dealvant.promotion_queue).
Both use this module, so a promotion maps to the same offer, merchant and
slug whoever publishes it.

- The offer id comes from the network and promotion id, so republishing
  updates the same offer, and it keeps its first slug (links stay valid).
- The merchant is the advertiser, by name: an existing Dealvant merchant
  with the same slug is reused (one amazonnew or another network created),
  otherwise one is created with the advertiser's site, network link and logo.
- The offer links out through its own tracking link (offers.affiliate_url,
  see dealvant's /go), never through a merchant-wide one.

Nothing here commits; the caller owns the transaction.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from datetime import datetime

from common_utils.dealvant import store

DEFAULT_BRAND_COLOR = "#1D8EF5"


@dataclass
class PromotionDraft:
    network: str  # "awin" | "rakuten" | "cj"; the offer's source_name
    promotion_id: str
    merchant_name: str
    merchant_website_url: str
    merchant_tracking_url: str
    merchant_logo_url: str | None
    title: str
    short_description: str
    long_description: str  # Markdown
    terms: str
    category_id: str
    affiliate_url: str
    image_url: str | None = None
    coupon_code: str | None = None
    expires_at: datetime | None = None
    meta_title: str | None = None
    meta_description: str | None = None
    best_for: str | None = None


@dataclass
class PublishedPromotion:
    offer_id: str
    slug: str
    merchant_id: str


def slugify(value: str, fallback: str = "deal") -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")
    return slug[:80].strip("-") or fallback


def offer_id_for(network: str, promotion_id: str) -> str:
    return hashlib.md5(f"{network}:promotion:{promotion_id}".encode("utf-8")).hexdigest()


def merchant_id_for(slug: str) -> str:
    # amazonnew's convention, so both find the same merchant for a store.
    return hashlib.md5(f"merchant:{slug}".encode("utf-8")).hexdigest()


def _offer_slug(conn, offer_id: str, title: str) -> str:
    with conn.cursor() as cursor:
        cursor.execute("SELECT slug FROM offers WHERE id = %s", (offer_id,))
        row = cursor.fetchone()
        if row is not None:
            return row[0] if isinstance(row, tuple) else row["slug"]
        base = slugify(title)
        candidate, n = base, 2
        while True:
            cursor.execute("SELECT 1 FROM offers WHERE slug = %s", (candidate,))
            if cursor.fetchone() is None:
                return candidate
            candidate, n = f"{base}-{n}", n + 1


def publish_promotion(conn, draft: PromotionDraft, *, status: str = "LIVE") -> PublishedPromotion:
    """Creates or updates the offer for `draft` (and its merchant if new)."""
    merchant_slug = slugify(draft.merchant_name, fallback="store")
    merchant_id = store.ensure_merchant(
        conn,
        store.MerchantRecord(
            id=merchant_id_for(merchant_slug),
            name=draft.merchant_name,
            slug=merchant_slug,
            tagline=f"Deals and coupons from {draft.merchant_name}.",
            description=f"Current deals, sales and coupon codes from {draft.merchant_name}.",
            website_url=draft.merchant_website_url,
            tracking_url=draft.merchant_tracking_url,
            hero_image=draft.merchant_logo_url or "",
            brand_color=DEFAULT_BRAND_COLOR,
            category_id=draft.category_id,
        ),
    )
    offer_id = offer_id_for(draft.network, draft.promotion_id)
    slug = _offer_slug(conn, offer_id, draft.title)
    store.upsert_offer(
        conn,
        store.OfferRecord(
            id=offer_id,
            title=draft.title,
            slug=slug,
            short_description=draft.short_description,
            long_description=draft.long_description,
            terms=draft.terms,
            cashback_percent=0,
            status=status,
            is_featured=False,
            merchant_id=merchant_id,
            category_id=draft.category_id,
            coupon_code=draft.coupon_code or None,
            source_name=draft.network,
            meta_title=draft.meta_title,
            meta_description=draft.meta_description,
            best_for=draft.best_for,
            affiliate_url=draft.affiliate_url,
            image_url=draft.image_url,
            expires_at=draft.expires_at,
        ),
    )
    return PublishedPromotion(offer_id=offer_id, slug=slug, merchant_id=merchant_id)
