from __future__ import annotations

import asyncio
import random
import time


async def scroll_and_wait(page, *, min_scrolls: int = 2, max_scrolls: int = 5, min_pause_ms: int = 600, max_pause_ms: int = 1800) -> None:
    # Drive scrolling through real mouse move + wheel input events (as a
    # human would) rather than a purely programmatic window.scrollBy() call.
    # Some anti-bot fingerprinting checks for the presence of pointer/wheel
    # input events correlated with scroll position changes; a page that only
    # ever scrolls via JS with no mouse activity at all is itself a signal.
    try:
        viewport = page.viewport_size or {"width": 1280, "height": 900}
    except Exception:
        viewport = {"width": 1280, "height": 900}
    width = max(int(viewport.get("width", 1280)), 200)
    height = max(int(viewport.get("height", 900)), 200)

    scroll_count = random.randint(min_scrolls, max(min_scrolls, max_scrolls))
    for _ in range(scroll_count):
        try:
            await page.mouse.move(
                random.randint(50, max(51, width - 50)),
                random.randint(120, max(121, height - 100)),
                steps=random.randint(4, 12),
            )
            delta_y = random.randint(600, 1200)
            await page.mouse.wheel(0, delta_y)
        except Exception:
            # Fall back to a programmatic scroll if mouse input isn't
            # available for some reason (e.g. a non-standard page/frame).
            await page.evaluate(
                """
                () => {
                  const amount = Math.floor(window.innerHeight * (0.65 + Math.random() * 0.7));
                  window.scrollBy({ top: amount, behavior: 'smooth' });
                }
                """
            )
        await page.wait_for_timeout(random.randint(min_pause_ms, max_pause_ms))


async def warm_session(page, warmup_urls: list[str] | tuple[str, ...] | None, *, timeout_ms: int = 30000) -> None:
    for warmup_url in warmup_urls or []:
        await page.goto(warmup_url, wait_until="domcontentloaded", timeout=timeout_ms)
        await asyncio.sleep(random.uniform(0.8, 2.0))


def warm_session_sync(page, warmup_urls: list[str] | tuple[str, ...] | None, *, timeout_ms: int = 30000) -> None:
    # Sync counterpart to warm_session(), for the sync-API consumers
    # (redirect_resolver.py's follow_with_playwright_sync) that don't run
    # under asyncio.
    for warmup_url in warmup_urls or []:
        page.goto(warmup_url, wait_until="domcontentloaded", timeout=timeout_ms)
        time.sleep(random.uniform(0.8, 2.0))


def scroll_and_wait_sync(page, *, min_scrolls: int = 2, max_scrolls: int = 5, min_pause_ms: int = 600, max_pause_ms: int = 1800) -> None:
    # Sync counterpart to scroll_and_wait() -- same mouse-driven scroll
    # rationale, for sync-API consumers.
    try:
        viewport = page.viewport_size or {"width": 1280, "height": 900}
    except Exception:
        viewport = {"width": 1280, "height": 900}
    width = max(int(viewport.get("width", 1280)), 200)
    height = max(int(viewport.get("height", 900)), 200)

    scroll_count = random.randint(min_scrolls, max(min_scrolls, max_scrolls))
    for _ in range(scroll_count):
        try:
            page.mouse.move(
                random.randint(50, max(51, width - 50)),
                random.randint(120, max(121, height - 100)),
                steps=random.randint(4, 12),
            )
            delta_y = random.randint(600, 1200)
            page.mouse.wheel(0, delta_y)
        except Exception:
            page.evaluate(
                """
                () => {
                  const amount = Math.floor(window.innerHeight * (0.65 + Math.random() * 0.7));
                  window.scrollBy({ top: amount, behavior: 'smooth' });
                }
                """
            )
        page.wait_for_timeout(random.randint(min_pause_ms, max_pause_ms))
