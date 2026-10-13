"""M5 success criteria: Scheduler → IndeedScraper → Pipeline → Database →
Telegram, full stack. Indeed and Telegram HTTP layers are MockTransports;
database is real SQLite + Alembic. A separate browser-marked test drives the
real Playwright fallback path.
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
from scrapers.indeed import IndeedScraper
from session_manager import SessionManager
from stats import StatisticsCollector
from telegram import TelegramService
from tests.test_indeed import card, make_handler, page
from user_agent import UserAgentProvider

K = "Product Designer"


@pytest.fixture
async def stack(tmp_path: Path) -> AsyncIterator[dict[str, object]]:
    config = Config(
        telegram_token="123:tok",
        chat_id="42",
        keywords="Product Designer,UX Designer",
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

    indeed_pages = {
        (K, 0): page(
            card(1),                                   # valid Berlin job
            card(2, title="Graphic Designer"),         # keyword reject (pipeline)
            card(3, location="Paris"),                 # country reject (pipeline)
            card(4, title="UX Designer", location="Munich"),  # valid
        )
    }

    def build_scraper() -> IndeedScraper:
        return IndeedScraper(
            config,
            statistics,
            SessionManager(config, statistics),
            RateLimiter(
                config,
                statistics,
                rng=random.Random(42),
                clock=lambda: 0.0,
                sleep=instant_sleep,
            ),
            RetryPolicy(config, statistics, sleep=instant_sleep),
            UserAgentProvider(config, rng=random.Random(42)),
            HTMLParser(),
            Normalizer(),
            transport=httpx.MockTransport(make_handler(indeed_pages)),
        )

    scheduler.register_scraper(build_scraper())
    yield {
        "scheduler": scheduler,
        "database": database,
        "telegram_messages": telegram_messages,
        "statistics": statistics,
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
        assert report.jobs_found == 4          # scraper extracts all, filters nothing
        assert report.summary.inserted == 2    # pipeline keeps the two valid ones
        assert report.summary.notified == 2

        db_stats = await database.get_statistics()
        assert db_stats["total_jobs"] == 2
        assert db_stats["jobs_per_source"] == {"Indeed": 2}

        assert len(messages) == 2
        texts = [m["text"] for m in messages]
        assert any("Miro" in text for text in texts)
        assert all(m["parse_mode"] == "MarkdownV2" for m in messages)

    async def test_second_cycle_all_duplicates_no_new_notifications(
        self, stack: dict[str, object]
    ) -> None:
        scheduler: Scheduler = stack["scheduler"]  # type: ignore[assignment]
        messages = stack["telegram_messages"]
        await scheduler.run_cycle()
        assert len(messages) == 2
        report = await scheduler.run_cycle()
        assert report is not None
        assert report.summary.inserted == 0
        assert len(messages) == 2  # exactly-once notification guarantee


@pytest.mark.browser
class TestRealPlaywrightFallback:
    async def test_fetch_browser_returns_rendered_content(self, tmp_path: Path) -> None:
        config = Config(
            telegram_token="t", chat_id="c", rotate_user_agent=False, retry_jitter=False
        )
        statistics = StatisticsCollector()

        async def instant_sleep(seconds: float) -> None:
            return None

        browser_manager = BrowserManager(
            config, UserAgentProvider(config, rng=random.Random(42)), statistics
        )
        scraper = IndeedScraper(
            config,
            statistics,
            SessionManager(config, statistics),
            RateLimiter(config, statistics, clock=lambda: 0.0, sleep=instant_sleep),
            RetryPolicy(config, statistics, sleep=instant_sleep),
            UserAgentProvider(config, rng=random.Random(42)),
            HTMLParser(),
            Normalizer(),
            browser_manager=browser_manager,
        )
        target = "data:text/html," + page(card(1))
        try:
            html = await scraper._fetch_browser(target)
        finally:
            await scraper._shutdown_browser()
        assert html is not None
        assert "job_seen_beacon" in html
        assert not browser_manager.started  # lazy start, guaranteed stop
