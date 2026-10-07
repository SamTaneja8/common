"""Automated SEO, grammar and spelling checks on publishable copy.

Shown to reviewers in reviewgate, and used by autopub's blogger as the gate
that downgrades an --auto-publish roundup to DRAFT. Moved here from
reviewgate/app/review_checks.py so both use one rule set.

Needs pyspellchecker (imported on first use, so modules that never call
build_checks don't need it installed).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

WORD_RE = re.compile(r"[A-Za-z]{3,}")
_SPELLCHECKER = None


def _spellchecker():
    global _SPELLCHECKER
    if _SPELLCHECKER is None:
        from spellchecker import SpellChecker

        _SPELLCHECKER = SpellChecker()
    return _SPELLCHECKER


@dataclass(slots=True)
class ReviewChecks:
    seo_score: int
    seo_notes: list[str] = field(default_factory=list)
    grammar_notes: list[str] = field(default_factory=list)
    spelling_notes: list[str] = field(default_factory=list)


def build_checks(
    *,
    display_title: str,
    short_summary: str,
    feature_bullets: list[str],
    merged_draft: str,
    search_keywords: list[str],
    links: list[dict[str, str]],
) -> ReviewChecks:
    seo_score = 100
    seo_notes: list[str] = []
    grammar_notes: list[str] = []
    spelling_notes: list[str] = []

    title_len = len(display_title.strip())
    if title_len < 35 or title_len > 70:
        seo_score -= 10
        seo_notes.append("Title length is outside the typical 35-70 character SEO band.")
    if len(short_summary.strip()) < 110:
        seo_score -= 10
        seo_notes.append("Summary is short; aim for 110+ characters for stronger excerpt coverage.")
    if len(feature_bullets) < 3:
        seo_score -= 8
        seo_notes.append("Add at least three bullets for scannability.")
    if not search_keywords:
        seo_score -= 10
        seo_notes.append("No search keywords were saved.")
    if not links:
        seo_score -= 7
        seo_notes.append("No related links are attached yet.")
    if not re.search(r"\b(what|how|why|when|does|is|can)\b", merged_draft.lower()):
        seo_score -= 6
        seo_notes.append("Draft has no obvious question language; FAQ coverage may be weak.")

    if "  " in merged_draft:
        grammar_notes.append("Double spaces detected in the draft.")
    if re.search(r"\b(\w+)\s+\1\b", merged_draft, re.IGNORECASE):
        grammar_notes.append("Repeated adjacent words detected.")
    for sentence in re.split(r"(?<=[.!?])\s+", merged_draft.strip()):
        if len(sentence) > 260:
            grammar_notes.append("At least one sentence is very long and may read awkwardly.")
            break
    for line in merged_draft.splitlines():
        stripped = line.strip()
        if stripped and stripped[0].islower() and len(stripped) > 20:
            grammar_notes.append("A sentence or paragraph appears to start with lowercase text.")
            break

    words = WORD_RE.findall(" ".join([display_title, short_summary, merged_draft]))
    spellchecker = _spellchecker()
    unknown = sorted({word.lower() for word in words if word.lower() not in spellchecker})
    for word in unknown[:12]:
        candidates = spellchecker.candidates(word)
        if not candidates:
            continue
        suggestion = next(iter(sorted(candidates)))
        if suggestion != word:
            spelling_notes.append(f"Possible misspelling: '{word}' -> '{suggestion}'")

    return ReviewChecks(
        seo_score=max(0, seo_score),
        seo_notes=seo_notes,
        grammar_notes=grammar_notes,
        spelling_notes=spelling_notes,
    )
