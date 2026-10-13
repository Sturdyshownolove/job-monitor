"""LinkedIn end-to-end: Scheduler → LinkedInScraper (fake browser) →
Pipeline → real Database → Telegram (MockTransport), plus a real-Chromium
render test driving the actual Playwright stack over a data: URL.
"""

from __future__ import annotations

import json
import random
from collections.abc import AsyncIterator
from pathlib import Path

import httpx
import pytest

from browser import BrowserManager
from config import Config
from database import Database
from html_parser import HTMLParser
from normalizer import Normalizer
from pipeline import FilteringPipeline
from rate_limiter import RateLimiter
from retry import RetryPolicy
from scheduler import Scheduler
from scrapers.linkedin import LinkedInScraper
from stats import StatisticsCollector
from telegram import TelegramService
from tests.test_linkedin import FakeBrowserManager, card, page_html
from user_agent import UserAgentProvider

K = "Product Designer"


@pytest.fixture
async def stack(tmp_path: Path) -> AsyncIterator[dict[str, object]]:
    config = Config(
        telegram_token="123:tok",
        chat_id="42",
        keywords=K,
        retry_jitter=False,
        rotate_user_agent=False,
        database_url=f"sqlite+aiosqlite:///{(tmp_path / 'jobs.db').as_posix()}",
    )
    statistics = StatisticsCollector()

    async def instant_sleep(seconds: float) -> None:
        return None

    database = Database(config, statistics)
    await database.start()

    telegram_messages: list[dict[str, str]] = []

    def telegram_handler(request: httpx.Request) -> httpx.Response:
        telegram_messages.append(json.loads(request.content))
        return httpx.Response(200, json={"ok": True})

    telegram = TelegramService(
        config,
        RetryPolicy(config, statistics, sleep=instant_sleep),
        statistics,
        transport=httpx.MockTransport(telegram_handler),
        send_interval=0.0,
        sleep=instant_sleep,
    )
    pipeline = FilteringPipeline(config, Normalizer(), database, telegram, statistics)
    scheduler = Scheduler(config, pipeline, statistics)

    routes = {
        (K, 0): page_html(
            card(1),                          # valid Berlin job
            card(2, location="Paris"),        # pipeline rejects: country
            card(3, location=None),           # location Unknown → pipeline rejects
        )
    }
    scraper = LinkedInScraper(
        config,
        statistics,
        FakeBrowserManager(routes),  # type: ignore[arg-type]
        RateLimiter(config, statistics, rng=random.Random(42), clock=lambda: 0.0, sleep=instant_sleep),
        RetryPolicy(config, statistics, sleep=instant_sleep),
        HTMLParser(),
        Normalizer(),
    )
    scheduler.register_scraper(scraper)
    yield {
        "scheduler": scheduler,
        "database": database,
        "telegram_messages": telegram_messages,
    }
    await telegram.close()
    await database.close()


class TestEndToEnd:
    async def test_full_cycle(self, stack: dict[str, object]) -> None:
        scheduler: Scheduler = stack["scheduler"]  # type: ignore[assignment]
        database: Database = stack["database"]  # type: ignore[assignment]
        messages = stack["telegram_messages"]

        report = await scheduler.run_cycle()
        assert report is not None
        assert report.jobs_found == 3
        assert report.summary.inserted == 1     # Berlin job only
        assert report.summary.notified == 1

        db_stats = await database.get_statistics()
        assert db_stats["total_jobs"] == 1
        assert db_stats["jobs_per_source"] == {"LinkedIn": 1}
        assert len(messages) == 1
        assert "LinkedIn" in messages[0]["text"]

    async def test_second_cycle_no_new_notifications(self, stack: dict[str, object]) -> None:
        scheduler: Scheduler = stack["scheduler"]  # type: ignore[assignment]
        messages = stack["telegram_messages"]
        await scheduler.run_cycle()
        await scheduler.run_cycle()
        assert len(messages) == 1  # exactly-once guarantee across cycles


@pytest.mark.browser
class TestRealPlaywrightRender:
    async def test_render_and_extract_with_real_browser(self) -> None:
        config = Config(
            telegram_token="t", chat_id="c", rotate_user_agent=False, retry_jitter=False
        )
        statistics = StatisticsCollector()

        async def instant_sleep(seconds: float) -> None:
            return None

        manager = BrowserManager(
            config, UserAgentProvider(config, rng=random.Random(42)), statistics
        )
        scraper = LinkedInScraper(
            config,
            statistics,
            manager,
            RateLimiter(config, statistics, clock=lambda: 0.0, sleep=instant_sleep),
            RetryPolicy(config, statistics, sleep=instant_sleep),
            HTMLParser(),
            Normalizer(),
        )
        await manager.start()
        try:
            async with manager.page_session() as page:
                await page.goto("data:text/html," + page_html(card(1), card(2)))
                html = await scraper._render_page(page)
                document = HTMLParser().parse(html)
                cards = scraper._find_cards(document)
                jobs = [scraper._extract_card(document, c) for c in cards]
        finally:
            await manager.stop()
        jobs = [job for job in jobs if job is not None]
        assert len(jobs) == 2
        assert jobs[0].title == "Senior Product Designer"
        assert jobs[0].url == "https://www.linkedin.com/jobs/view/1001"
