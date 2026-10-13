"""RemoteRocketship scraper tests. Playwright faked at the BrowserManager
boundary (same approach as test_linkedin)."""

from __future__ import annotations

import random
from urllib.parse import parse_qs, urlsplit

from playwright.async_api import TimeoutError as PlaywrightTimeoutError

from config import Config
from html_parser import HTMLParser
from normalizer import Normalizer
from rate_limiter import RateLimiter
from retry import RetryPolicy
from scrapers.remoterocketship import RemoteRocketshipScraper
from stats import StatisticsCollector

# -- fixture HTML (mirrors the live card structure) ---------------------------


def card(
    n: int = 1,
    *,
    title: str | None = "Senior Product Designer",
    company: str | None = "Tabby",
    location_pill: str | None = "🇩🇪 Germany – Remote",
    posted: str | None = "🔥 8 minutes ago",
    language_pill: str | None = None,
    href: str | None = None,
) -> str:
    link = href if href is not None else f"/publicjobs/company/acme/jobs/job-{n}/"
    pills = []
    if location_pill is not None:
        pills.append(f'<div class="pill"><p>{location_pill}</p></div>')
    if language_pill is not None:
        pills.append(f'<div class="pill"><p>{language_pill}</p></div>')
    pills.append('<div class="pill"><p>☁️ SaaS</p></div>')
    title_html = (
        f'<h3><a target="_blank" href="{link}">{title}</a></h3>' if title is not None else ""
    )
    company_html = (
        f'<h4><a target="_blank" href="/company/acme/">{company}</a></h4>'
        if company is not None
        else ""
    )
    posted_html = f'<p class="notranslate">{posted}</p>' if posted is not None else ""
    return (
        '<div class="relative cursor-pointer" role="button" tabindex="0">'
        f"<div>{title_html}{posted_html}{company_html}{''.join(pills)}</div>"
        "</div>"
    )


def page_html(*cards: str) -> str:
    return f"<html><body><div>{''.join(cards)}</div></body></html>"


EMPTY = page_html()

# -- fake Playwright layer ----------------------------------------------------


class FakePage:
    def __init__(self) -> None:
        self._html = ""
        self.closed = False

    def load(self, content: str) -> None:
        self._html = content

    async def wait_for_timeout(self, ms: float) -> None:
        return None

    async def wait_for_selector(self, selector: str, timeout: float = 0) -> None:
        return None

    async def content(self) -> str:
        return self._html

    async def close(self) -> None:
        self.closed = True


class FakeContext:
    def __init__(self) -> None:
        self.closed = False

    async def close(self) -> None:
        self.closed = True


class FakeBrowserManager:
    """Routes navigations by (jobTitle, page) query params."""

    def __init__(self, routes: dict[tuple[str, int], object]) -> None:
        self._routes = dict(routes)
        self.started = False
        self.start_calls = 0
        self.stop_calls = 0
        self.navigations: list[tuple[str, int]] = []

    async def start(self) -> None:
        self.started = True
        self.start_calls += 1

    async def stop(self) -> None:
        self.started = False
        self.stop_calls += 1

    async def restart(self) -> None:
        self.started = True

    async def new_context(self) -> FakeContext:
        return FakeContext()

    async def new_page(self, context: FakeContext) -> FakePage:
        return FakePage()

    async def navigate(self, page: FakePage, url: str) -> None:
        params = parse_qs(urlsplit(url).query)
        key = (params["jobTitle"][0], int(params.get("page", ["1"])[0]))
        self.navigations.append(key)
        entry = self._routes.get(key, EMPTY)
        if isinstance(entry, Exception):
            raise entry
        page.load(str(entry))


def make_scraper(
    routes: dict[tuple[str, int], object],
    keywords: str = "Product Designer",
    max_pages: int = 20,
) -> tuple[RemoteRocketshipScraper, FakeBrowserManager, StatisticsCollector]:
    config = Config(
        telegram_token="t",
        chat_id="c",
        keywords=keywords,
        max_pages=max_pages,
        retry_jitter=False,
        rotate_user_agent=False,
    )
    statistics = StatisticsCollector()

    async def instant_sleep(seconds: float) -> None:
        return None

    manager = FakeBrowserManager(routes)
    scraper = RemoteRocketshipScraper(
        config,
        statistics,
        manager,  # type: ignore[arg-type]
        RateLimiter(
            config, statistics, rng=random.Random(42), clock=lambda: 0.0, sleep=instant_sleep
        ),
        RetryPolicy(config, statistics, sleep=instant_sleep),
        HTMLParser(),
        Normalizer(),
    )
    return scraper, manager, statistics


K = "Product Designer"


class TestUrlBuilder:
    def test_basic(self) -> None:
        url = RemoteRocketshipScraper.build_search_url("Product Designer")
        assert url.startswith("https://www.remoterocketship.com/?")
        assert "jobTitle=Product+Designer" in url
        assert "locations=Germany" in url
        assert "page=1" in url

    def test_page(self) -> None:
        assert "page=3" in RemoteRocketshipScraper.build_search_url("UX Designer", page=3)


class TestExtraction:
    async def test_valid_card(self) -> None:
        scraper, _, _ = make_scraper({(K, 1): page_html(card(1))})
        jobs = await scraper.scrape()
        assert len(jobs) == 1
        job = jobs[0]
        assert job.title == "Senior Product Designer"
        assert job.company == "Tabby"
        assert job.location == "Germany – Remote"  # emoji stripped
        assert job.source == "RemoteRocketShip"
        assert job.posted_time == "8 minutes ago"
        assert job.language_note is None
        assert job.url == "https://www.remoterocketship.com/publicjobs/company/acme/jobs/job-1"

    async def test_language_pill_captured(self) -> None:
        scraper, _, statistics = make_scraper(
            {(K, 1): page_html(card(1, language_pill="🗣️🇩🇪 German Required"))}
        )
        jobs = await scraper.scrape()
        assert jobs[0].language_note == "German Required"
        assert statistics.snapshot()["counters"]["remoterocketship.language_pills"] == 1

    async def test_non_language_required_pill_ignored(self) -> None:
        scraper, _, _ = make_scraper(
            {(K, 1): page_html(card(1, language_pill="Experience Required"))}
        )
        jobs = await scraper.scrape()
        assert jobs[0].language_note is None

    async def test_worldwide_location_kept(self) -> None:
        scraper, _, _ = make_scraper(
            {(K, 1): page_html(card(1, location_pill="🌏 Anywhere in the World"))}
        )
        jobs = await scraper.scrape()
        assert jobs[0].location == "Anywhere in the World"

    async def test_card_without_location_skipped(self) -> None:
        scraper, _, statistics = make_scraper(
            {(K, 1): page_html(card(1, location_pill=None), card(2))}
        )
        jobs = await scraper.scrape()
        assert len(jobs) == 1
        assert statistics.snapshot()["counters"]["remoterocketship.cards_skipped"] == 1

    async def test_card_without_title_anchor_skipped(self) -> None:
        scraper, _, _ = make_scraper({(K, 1): page_html(card(1, title=None), card(2))})
        jobs = await scraper.scrape()
        assert len(jobs) == 1

    async def test_multi_job_container_not_treated_as_card(self) -> None:
        """Regression: a wrapper spanning several jobs must not become a card —
        it would attribute one job's "German Required" pill to its neighbor."""
        inner_a = (
            '<h3><a href="/publicjobs/company/acme/jobs/job-1/">Product Designer</a></h3>'
            '<div class="pill"><p>🗣️🇩🇪 German Required</p></div>'
        )
        inner_b = '<h3><a href="/publicjobs/company/acme/jobs/job-2/">UX Designer</a></h3>'
        blob = (
            '<div class="hydrating">'  # no role="button" wrappers
            f'<h4><a href="/company/acme/">Acme</a></h4>{inner_a}{inner_b}'
            "</div>"
        )
        scraper, _, statistics = make_scraper({(K, 1): page_html(blob)})
        jobs = await scraper.scrape()
        assert jobs == []  # ambiguous container skipped entirely
        counters = statistics.snapshot()["counters"]
        assert counters["remoterocketship.ambiguous_cards_skipped"] == 2


class TestPagination:
    async def test_repeated_first_url_stops(self) -> None:
        # Out-of-range pages render page 1 again.
        scraper, manager, _ = make_scraper(
            {
                (K, 1): page_html(card(1), card(2)),
                (K, 2): page_html(card(1), card(2)),
            },
            max_pages=5,
        )
        jobs = await scraper.scrape()
        assert len(jobs) == 2
        assert manager.navigations == [(K, 1), (K, 2)]

    async def test_empty_page_stops(self) -> None:
        scraper, manager, _ = make_scraper({(K, 1): page_html(card(1))}, max_pages=5)
        jobs = await scraper.scrape()
        assert len(jobs) == 1
        assert manager.navigations == [(K, 1), (K, 2)]

    async def test_duplicate_across_keywords_deduplicated(self) -> None:
        scraper, _, statistics = make_scraper(
            {
                ("A", 1): page_html(card(1)),
                ("B", 1): page_html(card(1)),
            },
            keywords="A,B",
        )
        jobs = await scraper.scrape()
        assert len(jobs) == 1
        assert statistics.snapshot()["counters"]["remoterocketship.duplicate_cards"] == 1


class TestResilience:
    async def test_timeout_moves_to_next_keyword(self) -> None:
        scraper, _, statistics = make_scraper(
            {
                ("A", 1): PlaywrightTimeoutError("timeout"),
                ("B", 1): page_html(card(2)),
            },
            keywords="A,B",
        )
        jobs = await scraper.scrape()
        assert len(jobs) == 1
        assert statistics.snapshot()["counters"]["remoterocketship.navigation_failures"] == 1

    async def test_browser_started_and_stopped_when_started_here(self) -> None:
        scraper, manager, _ = make_scraper({(K, 1): page_html(card(1))})
        await scraper.scrape()
        assert manager.start_calls == 1
        assert manager.stop_calls == 1
