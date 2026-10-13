"""Browser Manager (BrowserManager.md, Core/Specifications/Browser Lifecycle.md).

Only this module creates Playwright browsers. Async API. One browser per
scraper execution; fresh context per execution; cleanup guaranteed in reverse
order (page → context → browser). Crash recovery restarts the browser.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from playwright.async_api import (
    Browser,
    BrowserContext,
    Page,
    Playwright,
    async_playwright,
)
from playwright.async_api import TimeoutError as PlaywrightTimeoutError

from config import Config
from logger import logger
from stats import StatisticsCollector
from user_agent import UserAgentProvider

_LAUNCH_TIMEOUT_MS = 120_000
_NAVIGATION_TIMEOUT_MS = 30_000
_DEFAULT_TIMEOUT_MS = 30_000
_VIEWPORT = {"width": 1920, "height": 1080}
_LAUNCH_ARGS = ["--disable-gpu", "--disable-extensions", "--disable-notifications"]


class BrowserManager:
    """Owns the Playwright browser lifecycle. Scrapers never launch browsers."""

    def __init__(
        self,
        config: Config,
        user_agent_provider: UserAgentProvider,
        statistics: StatisticsCollector | None = None,
    ) -> None:
        self._config = config
        self._user_agent_provider = user_agent_provider
        self._statistics = statistics
        self._playwright: Playwright | None = None
        self._browser: Browser | None = None

    @property
    def started(self) -> bool:
        return self._browser is not None and self._browser.is_connected()

    async def start(self) -> None:
        """Launch Chromium (headless per config)."""
        self._playwright = await async_playwright().start()
        self._browser = await self._playwright.chromium.launch(
            headless=self._config.headless,
            timeout=_LAUNCH_TIMEOUT_MS,
            args=_LAUNCH_ARGS,
        )
        logger.info("Browser launched (chromium, headless={})", self._config.headless)
        if self._statistics is not None:
            self._statistics.increment("browser.launches")

    async def stop(self) -> None:
        """Close browser and Playwright. Safe to call repeatedly."""
        if self._browser is not None:
            try:
                await self._browser.close()
            except Exception as exc:
                logger.warning("Browser close failed: {}", exc)
            self._browser = None
        if self._playwright is not None:
            try:
                await self._playwright.stop()
            except Exception as exc:
                logger.warning("Playwright stop failed: {}", exc)
            self._playwright = None
        logger.info("Browser closed")

    async def restart(self) -> None:
        """Crash recovery: full stop, then fresh launch (Error Matrix)."""
        logger.warning("Restarting browser")
        await self.stop()
        await self.start()
        if self._statistics is not None:
            self._statistics.increment("browser.restarts")

    async def new_context(self) -> BrowserContext:
        """Fresh isolated context with a session User-Agent applied."""
        if self._browser is None:
            raise RuntimeError("BrowserManager not started")
        context = await self._browser.new_context(
            user_agent=self._user_agent_provider.new_session(),
            viewport=_VIEWPORT,
            locale="en-US",
            timezone_id="Europe/Berlin",
            color_scheme="light",
        )
        context.set_default_timeout(_DEFAULT_TIMEOUT_MS)
        logger.debug("Browser context created")
        if self._statistics is not None:
            self._statistics.increment("browser.contexts_created")
        return context

    async def new_page(self, context: BrowserContext) -> Page:
        page = await context.new_page()
        logger.debug("Page created")
        if self._statistics is not None:
            self._statistics.increment("browser.pages_created")
        return page

    async def navigate(self, page: Page, url: str) -> None:
        """Navigate with networkidle wait, domcontentloaded fallback."""
        logger.debug("Navigating to {}", url)
        try:
            await page.goto(url, wait_until="networkidle", timeout=_NAVIGATION_TIMEOUT_MS)
        except PlaywrightTimeoutError:
            logger.warning("networkidle timed out for {}, retrying domcontentloaded", url)
            await page.goto(
                url, wait_until="domcontentloaded", timeout=_NAVIGATION_TIMEOUT_MS
            )
        if self._statistics is not None:
            self._statistics.increment("browser.navigations")

    @asynccontextmanager
    async def page_session(self) -> AsyncIterator[Page]:
        """Context + page with guaranteed reverse-order cleanup."""
        context = await self.new_context()
        page = await self.new_page(context)
        try:
            yield page
        finally:
            try:
                await page.close()
            except Exception as exc:
                logger.warning("Page close failed: {}", exc)
            try:
                await context.close()
            except Exception as exc:
                logger.warning("Context close failed: {}", exc)

    async def health_check(self) -> bool:
        """Verify browser can serve a page (Health Checks.md)."""
        if not self.started:
            return False
        try:
            async with self.page_session() as page:
                await page.goto("about:blank", timeout=_NAVIGATION_TIMEOUT_MS)
            return True
        except Exception as exc:
            logger.warning("Browser health check failed: {}", exc)
            return False
