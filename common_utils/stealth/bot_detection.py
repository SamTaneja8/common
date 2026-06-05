from __future__ import annotations


DEFAULT_BOT_PATTERNS = (
    "enter the characters you see below",
    "sorry, we just need to make sure you're not a robot",
    "sorry, we need to make sure you're not a robot",
    "/errors/validatecaptcha",
    "captcha",
    "access denied",
    "you don't have permission to access",
    "akamai",
    "reference #",
)


def detect_bot_challenge(*, page_title: str | None = None, body_text: str | None = None, html: str | None = None) -> str | None:
    haystack = "\n".join([page_title or "", body_text or "", html or ""]).lower()
    for pattern in DEFAULT_BOT_PATTERNS:
        if pattern in haystack:
            return pattern
    return None
