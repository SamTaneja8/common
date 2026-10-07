# Category roundup articles

Long-form articles that bundle 2 or more live Dealvant offers from one
category ("The best kettle deals this week"), with a pick label and verdict
per offer and a link to each offer's page. They strengthen search and
answer-engine visibility beyond the one-deal-per-article pages the deal
pipeline publishes.

This replaces `autoblog/ROUNDUP_ARTICLES.md` (autoblog, the original
prototype, was retired on 2026-10-07).

## Who does what

| Step | Where |
|---|---|
| Pick a category and its offers, unattended | autopub `blogger.py` (`category_selection`, `deal_selection`: most embedded offers, similar offers by pgvector) |
| Pick a category and its offers, by a person | reviewgate `/roundups`: tick 2-8 live offers, "Request roundup" |
| Everything after that: keywords, outline, draft, SEO pass, compliance pass, gate, save | autopub, for both. Requests reach it through `reviewgate_roundup_requests` (dealstage MySQL, owned by reviewgate), claimed by autopub's worker |
| Review held roundups, publish or discard | reviewgate `/roundups` (drafts list) |
| Render | dealvant `/articles/[slug]`: "Our picks" block, comparison table, body, ItemList + FAQPage JSON-LD |

The code both repos share lives here in `common_utils.dealvant`:
`roundups.py` (prompt, assembly, structural checks, JSON-LD),
`checks.py` (the SEO/grammar/spelling checks), `store.py` (writes),
`requests.py` (the request queue).

```
reviewgate /roundups ──enqueue──▶ reviewgate_roundup_requests (MySQL) ◀──claim── autopub worker.py ─┐
autopub blogger.py (scheduled) ── selection ──────────────────────────────────────┤
                                                                                   ▼
      keyword_research → outline_generation → draft_writing → seo_aeo_pass → compliance_humanizer_pass → publish (gate)
                                                                                   ▼
                     articles (is_roundup) + article_offers: PUBLISHED, or DRAFT for reviewgate
```

## The gate

Unattended runs and reviewer requests use the same rule: a roundup is
published only if every selected offer has its own section with exactly
one link, and `checks.build_checks()` scores it at least
`BLOGGER_MIN_SEO_SCORE` (default 70). Anything else is saved as a DRAFT and
shows up on reviewgate's `/roundups` with the gate notes.

## Why the offer link can't be wrong

The model never writes a URL:

1. The prompt gives it a `{{OFFER_LINK}}` token to place, once, in the last
   paragraph of each offer's section.
2. `roundups.assemble_roundup()` HTML-escapes all model text, then replaces
   the token with a link built from the offer's own `slug` column.
3. A section whose token count isn't exactly one has its tokens dropped and
   one link appended; an offer the model skipped gets a short fallback
   section. Every selected offer is covered and linked exactly once.
4. Pick labels outside `store.PICK_LABELS` are dropped in code, and single-
   use labels ("Best overall", ...) can't repeat.

autopub's compliance pass may rewrite a section only if its rewrite still
has exactly one token; otherwise that section keeps the draft text.

## Idempotent regeneration

`roundups.stable_article_id()` derives the article id from
`(category_slug, sorted(offer_ids))`, so regenerating the same offers
updates the same article. `store.upsert_roundup()` never changes the status
of an existing article: a published roundup is refreshed, not unpublished.

## Upkeep

Before each scheduled blogger run, autopub's `upkeep.py` unpublishes
roundups that have fewer than 2 live offers left (reason recorded, so they
can be restored) and fills in missing offer embeddings.

## AI provider and cost

autopub's `ai_client.py` calls the provider directly through litellm:

| `CONTENT_AI_PROVIDER` | Model env var | Key env var |
|---|---|---|
| `deepseek` (default) | `DEEPSEEK_MODEL` (default `deepseek/deepseek-chat`) | `DEEPSEEK_API_KEY` |
| `claude` | `CLAUDE_MODEL` | `ANTHROPIC_API_KEY` |
| `openai` | `OPENAI_MODEL` | `OPENAI_API_KEY` |

Every call is logged to dealstage's `llm_usage_events`
(`source_system='autopub_roundup'`, one row per stage) through an
insert-only MySQL login, so roundup costs appear in Grafana's "AI usage and
cost" dashboard. Logging is best-effort: a failed write never fails a run.

Why not through aistage: aistage's `/v1/offers` is validated for exactly one
offer per request, and a roundup needs several offers in one call with
cross-offer awareness (comparison, FAQ). Extending the shared private
service for this wasn't worth the risk to the live deal pipeline.
