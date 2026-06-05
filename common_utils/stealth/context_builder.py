from __future__ import annotations

import random
from dataclasses import dataclass, field
from typing import Any

from common_utils.stealth.resource_blocking import ResourceBlockPolicy


DESKTOP_USER_AGENTS = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36",
)

STEALTH_INIT_SCRIPT = """
Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
Object.defineProperty(navigator, 'languages', { get: () => ['en-US', 'en'] });
Object.defineProperty(navigator, 'plugins', { get: () => [1, 2, 3, 4, 5] });
Object.defineProperty(navigator, 'platform', { get: () => 'Win32' });
Object.defineProperty(window, 'chrome', { get: () => ({ runtime: {} }) });
Object.defineProperty(navigator, 'pdfViewerEnabled', { get: () => true });
"""


def random_desktop_viewport(base_width: int = 1440, base_height: int = 2200) -> dict[str, int]:
    return {
        "width": max(1200, base_width + random.randint(-120, 160)),
        "height": max(1600, base_height + random.randint(-220, 220)),
    }


@dataclass(frozen=True, slots=True)
class BrowserProfile:
    user_agent: str | None = None
    locale: str = "en-US"
    timezone_id: str = "America/New_York"
    color_scheme: str = "light"
    viewport: dict[str, int] = field(default_factory=random_desktop_viewport)
    extra_http_headers: dict[str, str] = field(default_factory=dict)

    def resolved_user_agent(self) -> str:
        return self.user_agent or random.choice(DESKTOP_USER_AGENTS)


class ContextBuilder:
    def __init__(
        self,
        *,
        profile: BrowserProfile | None = None,
        resource_policy: ResourceBlockPolicy | None = None,
        apply_playwright_stealth: bool = True,
    ) -> None:
        self.profile = profile or BrowserProfile()
        self.resource_policy = resource_policy or ResourceBlockPolicy()
        self.apply_playwright_stealth = apply_playwright_stealth

    def launch_kwargs(self, *, proxy: dict[str, str] | None = None, headless: bool = True) -> dict[str, Any]:
        kwargs: dict[str, Any] = {
            "headless": headless,
            "args": [
                "--disable-blink-features=AutomationControlled",
                "--disable-dev-shm-usage",
                "--disable-features=IsolateOrigins,site-per-process,Translate",
                "--disable-infobars",
                "--disable-popup-blocking",
                "--no-default-browser-check",
                "--no-first-run",
            ],
        }
        if proxy:
            kwargs["proxy"] = proxy
        return kwargs

    async def new_async_context(self, browser):
        viewport = self.profile.viewport
        context = await browser.new_context(
            user_agent=self.profile.resolved_user_agent(),
            locale=self.profile.locale,
            timezone_id=self.profile.timezone_id,
            viewport=viewport,
            screen=viewport,
            device_scale_factor=1,
            color_scheme=self.profile.color_scheme,
            extra_http_headers=self.profile.extra_http_headers,
        )
        await context.add_init_script(STEALTH_INIT_SCRIPT)
        return context

    async def new_async_page(self, context):
        page = await context.new_page()
        await page.route("**/*", self.resource_policy.handle_async_route)
        if self.apply_playwright_stealth:
            try:
                from playwright_stealth import Stealth

                stealth = Stealth()
                apply_page = getattr(stealth, "apply_stealth_async", None)
                if callable(apply_page):
                    await apply_page(page)
            except ImportError:
                pass
        return page

    def new_sync_context(self, browser):
        viewport = self.profile.viewport
        context = browser.new_context(
            user_agent=self.profile.resolved_user_agent(),
            locale=self.profile.locale,
            timezone_id=self.profile.timezone_id,
            viewport=viewport,
            screen=viewport,
            device_scale_factor=1,
            color_scheme=self.profile.color_scheme,
            extra_http_headers=self.profile.extra_http_headers,
        )
        context.add_init_script(STEALTH_INIT_SCRIPT)
        return context

    def new_sync_page(self, context):
        page = context.new_page()
        page.route("**/*", self.resource_policy.handle_sync_route)
        if self.apply_playwright_stealth:
            try:
                from playwright_stealth import stealth_sync

                stealth_sync(page)
            except ImportError:
                pass
        return page
