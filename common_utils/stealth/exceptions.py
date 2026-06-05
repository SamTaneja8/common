from __future__ import annotations


class StealthError(RuntimeError):
    """Base error for shared stealth crawling."""


class ProxyFailure(StealthError):
    """Raised when a proxy/provider-level failure should rotate providers."""


class BotBlockedError(ProxyFailure):
    """Raised when a page looks like a captcha or bot challenge."""


class NavigationFailure(StealthError):
    """Raised when page navigation fails without a provider-specific diagnosis."""
