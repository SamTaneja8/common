from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any
import logging

from common_utils.playwright_exceptions import PlaywrightExceptionContext, resolve_scheduled_slot
from common_utils.proxy_settings import build_proxy_configs
from common_utils.proxy_telemetry import record_proxy_attempt, record_proxy_failure, record_proxy_success
from common_utils.stealth.bot_detection import detect_bot_challenge
from common_utils.stealth.context_builder import ContextBuilder
from common_utils.stealth.exceptions import BotBlockedError
from common_utils.stealth.navigation import scroll_and_wait, warm_session

logger = logging.getLogger("common_utils.stealth.proxy_runner")


@dataclass(frozen=True, slots=True)
class StealthFetchResult:
    html: str
    final_url: str
    page_title: str | None
    http_status: int | None
    proxy_provider: str


class StealthProxyRunner:
    def __init__(
        self,
        *,
        repo_name: str,
        job_name: str,
        service_name: str,
        context_builder: ContextBuilder | None = None,
        exception_pipeline: Any | None = None,
        headless: bool = True,
    ) -> None:
        self.repo_name = repo_name
        self.job_name = job_name
        self.service_name = service_name
        self.context_builder = context_builder or ContextBuilder()
        self.exception_pipeline = exception_pipeline
        self.headless = headless

    async def fetch(
        self,
        *,
        url: str,
        step_name: str,
        wait_until: str = "domcontentloaded",
        timeout_ms: int = 60000,
        warmup_urls: list[str] | tuple[str, ...] | None = None,
        scroll: bool = False,
        min_scrolls: int = 2,
        max_scrolls: int = 5,
        selector: str | None = None,
        proxy_configs: list[dict[str, Any]] | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> StealthFetchResult:
        from playwright.async_api import async_playwright

        configs = proxy_configs or build_proxy_configs()
        provider_errors: list[str] = []
        async with async_playwright() as playwright:
            for proxy_config in configs:
                provider_name = str(proxy_config["name"])
                playwright_proxy = proxy_config.get("playwright")
                record_proxy_attempt(provider_name, step_name)
                try:
                    return await self._fetch_with_provider(
                        playwright=playwright,
                        provider_name=provider_name,
                        playwright_proxy=playwright_proxy,
                        url=url,
                        step_name=step_name,
                        wait_until=wait_until,
                        timeout_ms=timeout_ms,
                        warmup_urls=warmup_urls,
                        scroll=scroll,
                        min_scrolls=min_scrolls,
                        max_scrolls=max_scrolls,
                        selector=selector,
                        metadata=metadata or {},
                    )
                except Exception as exc:
                    provider_errors.append(f"{provider_name}: {exc}")
                    record_proxy_failure(provider_name, step_name, type(exc).__name__, str(exc))
        raise RuntimeError(f"{self.job_name} failed for all providers: {' | '.join(provider_errors)}")

    async def _fetch_with_provider(
        self,
        *,
        playwright,
        provider_name: str,
        playwright_proxy: dict[str, str] | None,
        url: str,
        step_name: str,
        wait_until: str,
        timeout_ms: int,
        warmup_urls: list[str] | tuple[str, ...] | None,
        scroll: bool,
        min_scrolls: int,
        max_scrolls: int,
        selector: str | None,
        metadata: dict[str, Any],
    ) -> StealthFetchResult:
        stage = "launch"
        browser = None
        context = None
        page = None
        event_buffer = None
        logger.info(
            "Stealth fetch starting provider=%s step=%s proxy_enabled=%s url=%s",
            provider_name,
            step_name,
            bool(playwright_proxy),
            url,
        )
        try:
            logger.info("Stealth fetch provider=%s stage=%s", provider_name, stage)
            browser = await playwright.chromium.launch(**self.context_builder.launch_kwargs(proxy=playwright_proxy, headless=self.headless))
            stage = "new_context"
            logger.info("Stealth fetch provider=%s stage=%s", provider_name, stage)
            context = await self.context_builder.new_async_context(browser)
            stage = "new_page"
            logger.info(
                "Stealth fetch provider=%s stage=%s apply_playwright_stealth=%s",
                provider_name,
                stage,
                self.context_builder.apply_playwright_stealth,
            )
            page = await self.context_builder.new_async_page(context)
            event_buffer = self.exception_pipeline.new_event_buffer() if self.exception_pipeline else None
            if event_buffer:
                stage = "attach_event_buffer"
                logger.info("Stealth fetch provider=%s stage=%s", provider_name, stage)
                event_buffer.attach_async_page(page)
            stage = "warm_session"
            logger.info("Stealth fetch provider=%s stage=%s", provider_name, stage)
            await warm_session(page, warmup_urls, timeout_ms=timeout_ms)
            stage = "goto"
            logger.info("Stealth fetch provider=%s stage=%s wait_until=%s timeout_ms=%s", provider_name, stage, wait_until, timeout_ms)
            response = await page.goto(url, wait_until=wait_until, timeout=timeout_ms)
            if selector:
                stage = "wait_for_selector"
                logger.info("Stealth fetch provider=%s stage=%s selector=%s", provider_name, stage, selector)
                await page.wait_for_selector(selector, timeout=timeout_ms)
            if scroll:
                stage = "scroll"
                logger.info("Stealth fetch provider=%s stage=%s min_scrolls=%s max_scrolls=%s", provider_name, stage, min_scrolls, max_scrolls)
                await scroll_and_wait(page, min_scrolls=min_scrolls, max_scrolls=max_scrolls)
            stage = "content"
            logger.info("Stealth fetch provider=%s stage=%s", provider_name, stage)
            html = await page.content()
            try:
                body_text = await page.locator("body").inner_text(timeout=5000)
            except Exception:
                body_text = ""
            page_title = await page.title()
            bot_marker = detect_bot_challenge(page_title=page_title, body_text=body_text, html=html)
            if bot_marker:
                raise BotBlockedError(f"Bot challenge detected: {bot_marker}")
            record_proxy_success(provider_name, step_name)
            return StealthFetchResult(
                html=html,
                final_url=page.url,
                page_title=page_title,
                http_status=response.status if response is not None else None,
                proxy_provider=provider_name,
            )
        except Exception as exc:
            logger.exception("Stealth fetch provider=%s failed at stage=%s", provider_name, stage)
            if self.exception_pipeline:
                await self.exception_pipeline.capture_async_exception(
                    context=PlaywrightExceptionContext(
                        repo_name=self.repo_name,
                        job_name=self.job_name,
                        step_name=step_name,
                        service_name=self.service_name,
                        run_uuid=str(os.getenv("JOB_RUN_ID", "")).strip() or None,
                        sch_start=resolve_scheduled_slot(),
                        item_key="url",
                        item_value=url,
                        target_url=url,
                        final_url=page.url if not page.is_closed() else None,
                        proxy_provider=provider_name,
                        proxy_endpoint=(playwright_proxy or {}).get("server"),
                        metadata={**metadata, "proxy_enabled": bool(playwright_proxy)},
                    ),
                    exception=exc,
                    event_buffer=event_buffer,
                    page=page,
                )
            raise
        finally:
            if context is not None:
                await context.close()
            if browser is not None:
                await browser.close()
