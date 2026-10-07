"""Category roundup articles: the AI prompt, and turning the model's JSON
into safe article HTML, picks and JSON-LD.

Shared by reviewgate's /roundups page (human-triggered) and autopub's
blogger (unattended), so both publish the same shape through
dealvant_store.upsert_roundup.

Links are never written by the model: each section's last paragraph carries
a literal {{OFFER_LINK}} token that is replaced here with a link built from
the offer's own slug (see autoblog/ROUNDUP_ARTICLES.md). All model text is
HTML-escaped before the link is inserted.
"""

from __future__ import annotations

import hashlib
import html
import re
from typing import Any, Mapping, Sequence

from common_utils.dealvant.store import PICK_LABELS, RoundupPick, RoundupRecord

LINK_TOKEN = "{{OFFER_LINK}}"
DEFAULT_HERO_IMAGE = "/images/merchant-generic.svg"

SYSTEM_PROMPT = (
    "You are a shopping content writer producing a long-form roundup blog article for "
    "an affiliate deals site (Dealvant), optimized for both human shoppers and AI answer "
    "engines. Use ONLY the facts given about each offer -- never invent prices, ratings, "
    "availability, or claims not present in the input. Write naturally and helpfully; "
    "avoid superlatives that are not supported by the data. Return valid JSON only, "
    "matching the requested output_schema exactly -- no markdown, no commentary outside "
    "the JSON object."
)

PICK_RULE = (
    "Give each section a pick_label from exactly this list: "
    + ", ".join(f'"{label}"' for label in PICK_LABELS)
    + '. Use each label at most once, except "Also great", which any number of offers may share. '
    "Choose the label from the supplied facts only (price, features, best_for); if nothing in the "
    'data supports a distinction, use "Also great". Write verdict as one sentence, at most 160 '
    "characters, saying who the offer suits and why, from the supplied facts only."
)


def slugify(value: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")
    return slug or "roundup"


def stable_article_id(category_slug: str, offer_ids: Sequence[str]) -> str:
    """Same offers in the same category -> same article id, so re-running a
    roundup updates it instead of creating a duplicate."""
    digest = hashlib.sha256("|".join([category_slug, *sorted(offer_ids)]).encode("utf-8")).hexdigest()[:12]
    return f"roundup-{digest}"


def _offer_facts(offer: Mapping[str, Any]) -> dict[str, Any]:
    facts: dict[str, Any] = {
        "id": offer["id"],
        "title": offer["title"],
        "merchant": offer.get("merchant_name") or "",
        "cashback_percent": float(offer.get("cashback_percent") or 0),
        "short_description": offer.get("short_description") or "",
        "long_description": offer.get("long_description") or "",
    }
    for key in ("price_text", "brand", "best_for", "verdict"):
        if offer.get(key):
            facts[key] = offer[key]
    for key in ("pros", "cons"):
        if offer.get(key):
            facts[key] = list(offer[key])
    return facts


def build_user_payload(
    category_name: str,
    offers: Sequence[Mapping[str, Any]],
    *,
    target_keywords: Sequence[str] = (),
    outline: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "task": (
            f"Write a roundup article covering {len(offers)} current {category_name} deals. "
            "For each offer, write one dedicated section (heading + 2-4 paragraphs, ~200-300 "
            "words total) that naturally discusses the merchant and deal using only the "
            "supplied facts. The LAST paragraph of each offer's section must end with a short "
            f"call-to-action sentence containing the literal token {LINK_TOKEN} exactly once, "
            "positioned where a natural mention of the offer/merchant name would go -- do not "
            "write a URL yourself, and do not use the token anywhere else."
        ),
        "pick_rule": PICK_RULE,
        "category": category_name,
        "offers": [_offer_facts(offer) for offer in offers],
        "output_schema": {
            "title": "string, 50-70 chars, should mention the category and imply multiple deals",
            "meta_title": "string, target 50-65 chars, hard cap 80",
            "meta_description": "string, target 120-160 chars, hard cap 220",
            "excerpt": "string, 1-2 sentences, hard cap 300 chars",
            "intro_paragraphs": [
                "string -- 1 to 3 short paragraphs framing the shopping need/context; no offer-specific facts here"
            ],
            "sections": [
                {
                    "offer_id": "must exactly match one of the input offer ids; one section per offer, same order as input",
                    "pick_label": "one of " + " | ".join(PICK_LABELS),
                    "verdict": "string, one sentence, at most 160 chars",
                    "heading": "string, specific to that offer -- not a generic heading",
                    "paragraphs": [
                        f"string -- 2 to 4 paragraphs; the LAST paragraph must contain the literal token {LINK_TOKEN} exactly once"
                    ],
                }
            ],
            "comparison_summary": "string, 100-200 words comparing the offers factually, no invented differentiators",
            "faq": [
                {"question": "string", "answer": "string, grounded only in the given offer data, 2-3 sentences"}
            ],
        },
    }
    if target_keywords:
        payload["target_keywords"] = list(target_keywords)[:12]
        payload["keyword_rule"] = (
            "Work the target keywords and question-style queries in where they read naturally "
            "(title, headings, FAQ). Never force one in or change a fact to fit it."
        )
    if outline:
        payload["outline"] = dict(outline)
    return payload


def _link_html(offer: Mapping[str, Any]) -> str:
    anchor_text = f"{offer.get('merchant_name') or ''} {offer['title']}".strip()
    return f'<a href="/offers/{html.escape(offer["slug"])}">{html.escape(anchor_text)}</a>'


def structural_problems(result: Mapping[str, Any], offers: Sequence[Mapping[str, Any]]) -> list[str]:
    """What's wrong with the model's sections before assembly: an offer
    without its own section, or a section whose link token isn't there
    exactly once. Empty when the structure is sound."""
    problems: list[str] = []
    by_id = {offer["id"]: offer for offer in offers}
    seen: set[str] = set()
    for section in result.get("sections") or []:
        offer_id = section.get("offer_id")
        if offer_id not in by_id:
            problems.append(f"section for unknown offer id {offer_id!r}")
            continue
        if offer_id in seen:
            problems.append(f"more than one section for offer {offer_id}")
        seen.add(offer_id)
        token_count = sum(str(p).count(LINK_TOKEN) for p in section.get("paragraphs") or [])
        if token_count != 1:
            problems.append(f"offer {offer_id}: link token appears {token_count} times, expected 1")
    for offer_id in by_id:
        if offer_id not in seen:
            problems.append(f"no section for offer {offer_id}")
    return problems


def build_roundup_json_ld(
    *,
    title: str,
    description: str,
    url: str,
    offers: Sequence[Mapping[str, Any]],
    faq: Sequence[Mapping[str, str]],
) -> dict[str, Any]:
    """ItemList (the roundup itself) + optional FAQPage, schema.org JSON-LD.
    Individual offer pages carry their own Product/Offer markup -- this
    describes the roundup as a curated list linking to them."""
    graph: list[dict[str, Any]] = [
        {
            "@type": "ItemList",
            "name": title,
            "description": description,
            "url": url,
            "itemListElement": [
                {"@type": "ListItem", "position": position, "url": offer["url"], "name": offer["title"]}
                for position, offer in enumerate(offers, start=1)
            ],
        }
    ]
    if faq:
        graph.append(
            {
                "@type": "FAQPage",
                "mainEntity": [
                    {
                        "@type": "Question",
                        "name": item["question"],
                        "acceptedAnswer": {"@type": "Answer", "text": item["answer"]},
                    }
                    for item in faq
                ],
            }
        )
    return {"@context": "https://schema.org", "@graph": graph}


def assemble_roundup(
    *,
    category: Mapping[str, Any],
    offers: Sequence[Mapping[str, Any]],
    result: Mapping[str, Any],
    base_url: str,
    status: str = "DRAFT",
) -> RoundupRecord:
    """Builds the article from the model's JSON. Offers the model skipped
    still get a short fallback section, so every selected offer is covered
    and linked exactly once. Picks keep the model's section order."""
    if len(offers) < 2:
        raise ValueError("Need at least 2 live offers in this category to build a roundup")
    base_url = base_url.rstrip("/")
    offers_by_id = {offer["id"]: offer for offer in offers}
    section_html_parts: list[str] = []
    picks: list[RoundupPick] = []

    for section in result.get("sections") or []:
        offer = offers_by_id.get(section.get("offer_id"))
        if offer is None or any(pick.offer_id == offer["id"] for pick in picks):
            continue
        link_html = _link_html(offer)
        paragraphs = [str(p) for p in section.get("paragraphs") or [] if str(p).strip()]
        if not paragraphs:
            continue
        if sum(p.count(LINK_TOKEN) for p in paragraphs) != 1:
            # Never trust a wrong token count: drop every token and put the
            # one link at the end of the section.
            paragraphs = [p.replace(LINK_TOKEN, "").strip() for p in paragraphs]
            paragraphs[-1] = f"{paragraphs[-1]} {LINK_TOKEN}"
        paragraphs_html = [f"<p>{html.escape(p).replace(html.escape(LINK_TOKEN), link_html)}</p>" for p in paragraphs]
        heading = html.escape(str(section.get("heading") or offer["title"]).strip())
        section_html_parts.append(f"<h2>{heading}</h2>\n" + "\n".join(paragraphs_html))
        picks.append(
            RoundupPick(
                offer_id=offer["id"],
                pick_label=str(section.get("pick_label") or "") or None,
                verdict=str(section.get("verdict") or "")[:200] or None,
            )
        )

    for offer in offers:
        if any(pick.offer_id == offer["id"] for pick in picks):
            continue
        fallback = html.escape(offer.get("short_description") or offer.get("long_description") or "")
        section_html_parts.append(f"<h2>{html.escape(offer['title'])}</h2>\n<p>{fallback} {_link_html(offer)}</p>")
        picks.append(RoundupPick(offer_id=offer["id"], pick_label="Also great", verdict=offer.get("verdict") or None))

    intro_html = "\n".join(f"<p>{html.escape(str(p))}</p>" for p in result.get("intro_paragraphs") or [] if str(p).strip())
    comparison_summary = str(result.get("comparison_summary") or "").strip()
    comparison_html = (
        f"<h2>How These Deals Compare</h2>\n<p>{html.escape(comparison_summary)}</p>" if comparison_summary else ""
    )
    faq_items = [
        {"question": str(item.get("question")).strip(), "answer": str(item.get("answer")).strip()}
        for item in result.get("faq") or []
        if item.get("question") and item.get("answer")
    ]
    faq_html = ""
    if faq_items:
        faq_html = "\n".join(
            ["<h2>Frequently Asked Questions</h2>"]
            + [f"<h3>{html.escape(item['question'])}</h3>\n<p>{html.escape(item['answer'])}</p>" for item in faq_items]
        )
    body_html = "\n\n".join(part for part in [intro_html, *section_html_parts, comparison_html, faq_html] if part)

    ordered_ids = [pick.offer_id for pick in picks]
    title = str(result.get("title") or f"{category['name']} Deals Worth a Look").strip()
    article_id = stable_article_id(category["slug"], ordered_ids)
    slug = f"{slugify(title)}-{article_id.rsplit('-', 1)[-1]}"[:220].strip("-")
    excerpt = str(result.get("excerpt") or comparison_summary or "").strip()
    meta_description = str(result.get("meta_description") or excerpt).strip()
    hero_image = next(
        (offers_by_id[i].get("image_url") or offers_by_id[i].get("merchant_hero_image") for i in ordered_ids
         if offers_by_id[i].get("image_url") or offers_by_id[i].get("merchant_hero_image")),
        DEFAULT_HERO_IMAGE,
    )
    structured_data = build_roundup_json_ld(
        title=title,
        description=meta_description,
        url=f"{base_url}/articles/{slug}",
        offers=[{"title": offers_by_id[i]["title"], "url": f"{base_url}/offers/{offers_by_id[i]['slug']}"} for i in ordered_ids],
        faq=faq_items,
    )
    return RoundupRecord(
        id=article_id,
        title=title[:500],
        slug=slug,
        excerpt=excerpt[:1000],
        body_html=body_html,
        hero_image=hero_image,
        category_id=category["id"],
        meta_title=str(result.get("meta_title") or title)[:500],
        meta_description=meta_description[:1000],
        structured_data=structured_data,
        picks=picks,
        status=status,
    )
