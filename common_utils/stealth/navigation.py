from __future__ import annotations

import asyncio
import random


async def scroll_and_wait(page, *, min_scrolls: int = 2, max_scrolls: int = 5, min_pause_ms: int = 600, max_pause_ms: int = 1800) -> None:
    scroll_count = random.randint(min_scrolls, max(min_scrolls, max_scrolls))
    for _ in range(scroll_count):
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
