"""Browser manager tests (BrowserManager.md unit-test checklist).

Launches real Chromium (headless). Slowest suite — kept intentionally small.
"""

from __future__ import annotations

import random
from collections.abc import AsyncIterator

import pytest

from browser import BrowserManager
from config import Config
from stats import StatisticsCollector
from user_agent import FALLBACK_USER_AGENT, UserAgentProvider

pytestmark = pytest.mark.browser


@pytest.fixture
async def manager() -> AsyncIterator[BrowserManager]:
    config = Config(
        telegram_token="t",
        chat_id="c",
        rotate_user_agent=False,  # deterministic UA for assertions
    )
    provider = UserAgentProvider(config, rng=random.Random(42))
    stats = StatisticsCollector()
    browser_manager = BrowserManager(config, provider, stats)
    await browser_manager.start()
    yield browser_manager
    await browser_manager.stop()


class TestLifecycle:
    async def test_start_and_health(self, manager: BrowserManager) -> None:
        assert manager.started
        assert await manager.health_check()

    async def test_page_session_cleanup(self, manager: BrowserManager) -> None:
        async with manager.page_session() as page:
            await page.goto("data:text/html,<h1>hello</h1>")
            assert await page.text_content("h1") == "hello"
        assert page.is_closed()

    async def test_user_agent_applied(self, manager: BrowserManager) -> None:
        async with manager.page_session() as page:
            ua = await page.evaluate("navigator.userAgent")
        assert ua == FALLBACK_USER_AGENT

    async def test_navigate(self, manager: BrowserManager) -> None:
        async with manager.page_session() as page:
            await manager.navigate(page, "data:text/html,<title>t1</title><p>body</p>")
            assert await page.title() == "t1"

    async def test_restart(self, manager: BrowserManager) -> None:
        await manager.restart()
        assert manager.started
        assert await manager.health_check()

    async def test_stop_idempotent(self, manager: BrowserManager) -> None:
        await manager.stop()
        await manager.stop()
        assert not manager.started
        assert not await manager.health_check()
