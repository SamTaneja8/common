"""Turns an Amazon product-page scan into a Dealvant product record.

The scan fields (asin, brand, price_text, availability_text, rating_text,
review_count_text, bullets_json, details_json, image_urls_json, scanned_at,
final_url) are amazonnew's amzn_affiliate_page_scans columns, which
reviewgate also reads through amzn_page_snapshots. JSON columns may arrive
as strings (MySQL) or already decoded.

Also home of the foreign-price rule shared by amazonnew's scanner and
publish guard and reviewgate's publish guard (deal-pipeline KNOWN_ISSUES
#11/#29).
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any, Mapping
from urllib.parse import urlsplit

from common_utils.dealvant.store import ProductRecord

# A currency code (EUR6.45), a code-prefixed dollar (C$, A$, R$, MX$) or a
# non-dollar symbol, at the start of the price text.
_FOREIGN_PRICE = re.compile(r"^(?:[A-Z]{3}|[A-Z]{1,2}\$|[€£¥₹₩₽])")
_US_PRICE = re.compile(r"^(?:USD|US\$|\$)")
_AMOUNT = re.compile(r"\d[\d,]*(?:\.\d{1,2})?")
_ASIN = re.compile(r"^[A-Z0-9]{10}$")

MAX_SPECS = 12
MAX_BULLETS = 8
MAX_IMAGES = 8


def is_foreign_price(price_text: object) -> bool:
    """True if the price starts with a non-US currency: a code (EUR, ARS),
    a code-prefixed dollar (C$, S$, A$, R$, MX$) or a symbol (€, £, ...)."""
    if not isinstance(price_text, str):
        return False
    price = price_text.strip()
    return bool(price and _FOREIGN_PRICE.match(price) and not _US_PRICE.match(price))


def parse_usd_cents(price_text: object) -> int | None:
    """Cents for a US-dollar price ("$1,299.99", "USD 12.50", "12.99"),
    taking the low end of a range ("$19.99 - $29.99"). None for a foreign,
    empty or unparseable price, so it is never shown as USD."""
    if not isinstance(price_text, str) or is_foreign_price(price_text):
        return None
    match = _AMOUNT.search(price_text)
    if match is None:
        return None
    try:
        cents = int((Decimal(match.group(0).replace(",", "")) * 100).to_integral_value())
    except InvalidOperation:
        return None
    return cents if cents > 0 else None


def _decoded(value: Any, default: Any) -> Any:
    if value is None:
        return default
    if isinstance(value, (bytes, bytearray)):
        value = value.decode("utf-8", errors="replace")
    if isinstance(value, str):
        try:
            return json.loads(value) if value.strip() else default
        except ValueError:
            return default
    return value


def _text(value: Any) -> str | None:
    text = str(value).strip() if value is not None else ""
    return text or None


def marketplace_from_url(url: object, fallback: str = "amazon.com") -> str:
    host = (urlsplit(str(url or "")).hostname or "").lower()
    if host.startswith("www."):
        host = host[4:]
    return host if host.startswith("amazon.") else fallback


def product_id(marketplace: str, asin: str) -> str:
    return f"amazon:{marketplace}:{asin}"


def product_from_scan(scan: Mapping[str, Any], *, fallback_title: str = "", fallback_image: str = "") -> ProductRecord | None:
    """A ProductRecord from one scan row, or None when the scan has no
    usable ASIN or title (a block page)."""
    asin = str(scan.get("asin") or "").strip().upper()
    title = _text(scan.get("product_title")) or _text(fallback_title)
    if not _ASIN.match(asin) or not title:
        return None

    marketplace = marketplace_from_url(scan.get("final_url") or scan.get("canonical_url") or scan.get("product_url"))
    images = [str(url).strip() for url in _decoded(scan.get("image_urls_json"), []) or [] if str(url or "").strip()]
    primary_image = _text(scan.get("primary_image_large_url")) or (images[0] if images else None) or _text(fallback_image)
    bullets = [str(b).strip() for b in _decoded(scan.get("bullets_json"), []) or [] if str(b or "").strip()]
    details = _decoded(scan.get("details_json"), {}) or {}
    specs = (
        {str(k).strip(): str(v).strip() for k, v in list(details.items())[:MAX_SPECS] if str(k or "").strip() and str(v or "").strip()}
        if isinstance(details, dict)
        else {}
    )
    price_text = _text(scan.get("price_text"))
    price_cents = parse_usd_cents(price_text)
    scanned_at = scan.get("scanned_at")
    if isinstance(scanned_at, datetime) and scanned_at.tzinfo is None:
        # MySQL DATETIME columns come back naive; the scanner writes UTC.
        scanned_at = scanned_at.replace(tzinfo=timezone.utc)

    return ProductRecord(
        id=product_id(marketplace, asin),
        marketplace=marketplace,
        asin=asin,
        title=title,
        brand=_text(scan.get("brand")) or _text(scan.get("byline")),
        image_url=primary_image,
        image_urls=images[:MAX_IMAGES],
        bullets=bullets[:MAX_BULLETS],
        specs=specs,
        rating_text=_text(scan.get("rating_text")),
        review_count_text=_text(scan.get("review_count_text")),
        price_cents=price_cents,
        currency="USD" if price_cents is not None else None,
        price_text=price_text,
        availability_text=_text(scan.get("availability_text")),
        price_checked_at=scanned_at if isinstance(scanned_at, datetime) else None,
        price_source="scan",
    )
