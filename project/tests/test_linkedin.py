"""LinkedIn scraper tests (Scrapers/LinkedIn/Test Cases.md).

Playwright is faked at the BrowserManager boundary: FakePage parses its HTML
with BeautifulSoup so locator counts, scroll-driven lazy loading and content()
behave like the real page. Real Chromium is exercised in
test_linkedin_integration.py.
"""

from __future__ import annotations

import random
from urllib.parse import parse_qs, urlsplit

import pytest
from bs4 import BeautifulSoup
from playwright.async_api import Error as PlaywrightError
from playwright.async_api import TimeoutError as PlaywrightTimeoutError

from config import Config
from html_parser import HTMLParser
from normalizer import Normalizer
from rate_limiter import RateLimiter
from retry import RetryPolicy
from scrapers.linkedin import LinkedInScraper
from stats import StatisticsCollector

# -- fixture HTML ------------------------------------------------------------


def card(
    n: int = 1,
    *,
    title: str | None = "Senior Product Designer (m/w/d)",
    company: str | None = "Miro",
    location: str | None = "Berlin",
    posted: str | None = "2 hours ago",
    href: str | None = None,
    data_job_id: str | None = None,
    variant: str = "primary",
) -> str:
    if variant == "primary":
        link = href if href is not None else f"/jobs/view/{1000 + n}"
        job_id = data_job_id if data_job_id is not None else str(1000 + n)
        parts = [f'<div data-job-id="{job_id}">']
        if title is not None:
            parts.append(f'<a class="job-card-list__title" href="{link}">{title}</a>')
        if company is not None:
            parts.append(f'<span class="job-card-container__company-name">{company}</span>')
        if location is not None:
            parts.append(f'<span class="job-card-container__metadata-item">{location}</span>')
        if posted is not None:
            parts.append(f"<time>{posted}</time>")
        parts.append("</div>")
        return "".join(parts)
    # guest-page fallback variant
    link = href if href is not None else f"/jobs/view/{1000 + n}"
    return (
        '<div class="base-card">'
        f'<a class="base-card__full-link" href="{link}"></a>'
        f'<h3 class="base-search-card__title">{title}</h3>'
        f'<h4 class="base-search-card__subtitle">{company}</h4>'
        f'<span class="job-search-card__location">{location}</span>'
        f"<time>{posted}</time>"
        "</div>"
    )


def page_html(
    *cards: str, next_page: bool = False, authwall: bool = False, captcha: bool = False
) -> str:
    if authwall:
        return '<html><body><div class="authwall">Sign in</div></body></html>'
    if captcha:
        return '<html><body><div id="captcha-internal"></div></body></html>'
    nav = (
        '<button aria-label="View next page">Next</button>' if next_page else ""
    )
    return f"<html><body><ul>{''.join(cards)}</ul>{nav}</body></html>"


EMPTY = page_html()

# -- fake Playwright layer ---------------------------------------------------


class FakeLocator:
    def __init__(self, n: int) -> None:
        self._n = n

    async def count(self) -> int:
        return self._n


class FakePage:
    """Parses its HTML so selector counts and content() behave realistically.

    `stages` (list of HTML) simulates lazy loading: evaluate() advances one
    stage per scroll.
    """

    def __init__(self) -> None:
        self._stages: list[str] = [""]
        self._stage = 0
        self.scrolls = 0
        self.closed = False

    def load(self, content: str | list[str]) -> None:
        self._stages = content if isinstance(content, list) else [content]
        self._stage = 0

    @property
    def _html(self) -> str:
        return self._stages[min(self._stage, len(self._stages) - 1)]

    def locator(self, selector: str) -> FakeLocator:
        return FakeLocator(len(BeautifulSoup(self._html, "lxml").select(selector)))

    async def evaluate(self, script: str) -> None:
        self.scrolls += 1
        if self._stage < len(self._stages) - 1:
            self._stage += 1

    async def wait_for_timeout(self, ms: float) -> None:
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
    """Routes navigations to canned responses.

    routes: {(keyword, offset): entry} where entry is HTML, a list of HTML
    stages (lazy loading), an Exception instance, or a list of entries
    consumed one per navigation attempt (for retry scenarios).
    """

    def __init__(self, routes: dict[tuple[str, int], object]) -> None:
        self._routes = dict(routes)
        self.started = False
        self.start_calls = 0
        self.stop_calls = 0
        self.restart_calls = 0
        self.navigations: list[tuple[str, int]] = []

    async def start(self) -> None:
        self.started = True
        self.start_calls += 1

    async def stop(self) -> None:
        self.started = False
        self.stop_calls += 1

    async def restart(self) -> None:
        self.restart_calls += 1
        self.started = True

    async def new_context(self) -> FakeContext:
        return FakeContext()

    async def new_page(self, context: FakeContext) -> FakePage:
        return FakePage()

    async def navigate(self, page: FakePage, url: str) -> None:
        params = parse_qs(urlsplit(url).query)
        key = (params["keywords"][0], int(params.get("start", ["0"])[0]))
        self.navigations.append(key)
        entry = self._routes.get(key, EMPTY)
        if isinstance(entry, list):
            if entry and not isinstance(entry[0], str):
                entry = entry.pop(0)  # attempt sequence (exceptions then HTML)
            elif not entry:
                entry = EMPTY
        if isinstance(entry, Exception):
            raise entry
        page.load(entry)  # type: ignore[arg-type]


def make_scraper(
    routes: dict[tuple[str, int], object],
    keywords: str = "Product Designer",
    max_pages: int = 20,
) -> tuple[LinkedInScraper, FakeBrowserManager, StatisticsCollector]:
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
    scraper = LinkedInScraper(
        config,
        statistics,
        manager,  # type: ignore[arg-type]
        RateLimiter(config, statistics, rng=random.Random(42), clock=lambda: 0.0, sleep=instant_sleep),
        RetryPolicy(config, statistics, sleep=instant_sleep),
        HTMLParser(),
        Normalizer(),
    )
    return scraper, manager, statistics


K = "Product Designer"


class TestUrlBuilder:
    def test_basic(self) -> None:
        url = LinkedInScraper.build_search_url("Product Designer")
        assert url.startswith("https://www.linkedin.com/jobs/search/?")
        assert "keywords=Product+Designer" in url
        assert "location=Germany" in url
        assert "start=" not in url

    def test_offset(self) -> None:
        assert "start=25" in LinkedInScraper.build_search_url("UX Designer", offset=25)


class TestExtraction:
    async def test_valid_card(self) -> None:
        scraper, _, _ = make_scraper({(K, 0): page_html(card(1))})
        jobs = await scraper.scrape()
        assert len(jobs) == 1
        job = jobs[0]
        assert job.title == "Senior Product Designer"
        assert job.company == "Miro"
        assert job.location == "Berlin"
        assert job.source == "LinkedIn"
        assert job.posted_time == "2 hours ago"
        assert job.url == "https://www.linkedin.com/jobs/view/1001"

    async def test_missing_location_defaults_to_unknown(self) -> None:
        scraper, _, _ = make_scraper({(K, 0): page_html(card(1, location=None))})
        jobs = await scraper.scrape()
        assert jobs[0].location == "Unknown"  # Field Mapping default; pipeline decides

    @pytest.mark.parametrize("missing", ["title", "company"])
    async def test_missing_required_field_skips(self, missing: str) -> None:
        bad = card(1, **{missing: None})
        scraper, _, statistics = make_scraper({(K, 0): page_html(bad, card(2))})
        jobs = await scraper.scrape()
        assert len(jobs) == 1
        assert statistics.snapshot()["counters"]["linkedin.cards_skipped"] == 1

    async def test_data_job_id_url_fallback(self) -> None:
        html = (
            '<div data-job-id="777">'
            '<span class="job-card-list__title">UX Designer</span>'
            '<span class="job-card-container__company-name">SAP SE</span>'
            '<span class="job-card-container__metadata-item">Berlin</span>'
            "</div>"
        )
        scraper, _, _ = make_scraper({(K, 0): page_html(html)})
        jobs = await scraper.scrape()
        assert jobs[0].url == "https://www.linkedin.com/jobs/view/777"

    async def test_non_linkedin_url_rejected(self) -> None:
        scraper, _, _ = make_scraper(
            {(K, 0): page_html(card(1, href="https://evil.example.com/jobs/view/1"))}
        )
        jobs = await scraper.scrape()
        assert jobs == []

    async def test_tracking_params_stripped(self) -> None:
        scraper, _, _ = make_scraper(
            {(K, 0): page_html(card(1, href="/jobs/view/1001?trk=feed&refId=abc"))}
        )
        jobs = await scraper.scrape()
        assert jobs[0].url == "https://www.linkedin.com/jobs/view/1001"

    async def test_volatile_result_params_stripped(self) -> None:
        """Regression: position/pageNum change per render and must not defeat dedup."""
        scraper, _, _ = make_scraper(
            {(K, 0): page_html(card(1, href="/jobs/view/1001?position=32&pageNum=0"))}
        )
        jobs = await scraper.scrape()
        assert jobs[0].url == "https://www.linkedin.com/jobs/view/1001"

    async def test_slug_url_canonicalized_to_posting_id(self) -> None:
        """Regression: the numeric posting ID is the job's real identifier —
        /jobs/view/<id> resolves, the slug without it 404s. Slug and query
        must be stripped, the ID kept."""
        scraper, _, _ = make_scraper(
            {
                (K, 0): page_html(
                    card(1, href="/jobs/view/senior-designer-at-acme-4435826450?position=1"),
                    card(2, href="/jobs/view/senior-designer-m-w-d-at-acme-4435826450"),
                )
            }
        )
        jobs = await scraper.scrape()
        assert len(jobs) == 1  # same ID, differing slugs/params → one job
        assert jobs[0].url == "https://www.linkedin.com/jobs/view/4435826450"

    async def test_slug_url_without_id_kept_as_is(self) -> None:
        scraper, _, _ = make_scraper(
            {(K, 0): page_html(card(1, href="/jobs/view/senior-designer-at-acme"))}
        )
        jobs = await scraper.scrape()
        assert jobs[0].url == "https://www.linkedin.com/jobs/view/senior-designer-at-acme"

    async def test_country_host_normalized(self) -> None:
        scraper, _, _ = make_scraper(
            {(K, 0): page_html(card(1, href="https://de.linkedin.com/jobs/view/ux-at-sap-4441064894"))}
        )
        jobs = await scraper.scrape()
        assert jobs[0].url == "https://www.linkedin.com/jobs/view/4441064894"

    @pytest.mark.parametrize("bad_title", ["Loading", "Sponsored", "Promoted"])
    async def test_invalid_card_titles_skipped(self, bad_title: str) -> None:
        scraper, _, _ = make_scraper({(K, 0): page_html(card(1, title=bad_title))})
        jobs = await scraper.scrape()
        assert jobs == []

    async def test_duplicate_cards_deduplicated(self) -> None:
        scraper, _, statistics = make_scraper({(K, 0): page_html(card(1), card(1))})
        jobs = await scraper.scrape()
        assert len(jobs) == 1
        assert statistics.snapshot()["counters"]["linkedin.duplicate_cards"] == 1


class TestSelectorFallbacks:
    async def test_guest_page_variant_extracts(self) -> None:
        scraper, _, statistics = make_scraper(
            {(K, 0): page_html(card(1, title="UX Designer", variant="fallback"))}
        )
        jobs = await scraper.scrape()
        assert len(jobs) == 1
        assert jobs[0].title == "UX Designer"
        assert jobs[0].company == "Miro"
        assert statistics.snapshot()["counters"]["linkedin.selector_fallbacks"] > 0


class TestInfiniteScroll:
    async def test_lazy_loaded_cards_collected(self) -> None:
        stage1 = page_html(*[card(n) for n in range(5)])
        stage2 = page_html(*[card(n) for n in range(12)])
        # list-of-str = lazy stages; each scroll advances one stage
        scraper, manager, _ = make_scraper({(K, 0): [stage1, stage2, stage2]})
        jobs = await scraper.scrape()
        assert len(jobs) == 12

    async def test_stable_page_stops_scrolling(self) -> None:
        scraper, _, statistics = make_scraper({(K, 0): page_html(card(1))})
        await scraper.scrape()
        # count stabilizes after the second observation
        assert statistics.snapshot()["counters"]["linkedin.scroll_attempts"] <= 2


class TestPagination:
    async def test_follows_offsets(self) -> None:
        full = page_html(*[card(n) for n in range(25)], next_page=True)
        scraper, manager, _ = make_scraper(
            {(K, 0): full, (K, 25): page_html(card(100))}
        )
        jobs = await scraper.scrape()
        assert len(jobs) == 26
        assert manager.navigations == [(K, 0), (K, 25)]

    async def test_partial_page_stops(self) -> None:
        scraper, manager, _ = make_scraper({(K, 0): page_html(card(1), card(2))})
        jobs = await scraper.scrape()
        assert len(jobs) == 2
        assert manager.navigations == [(K, 0)]

    async def test_empty_page_stops(self) -> None:
        scraper, _, statistics = make_scraper({(K, 0): EMPTY})
        jobs = await scraper.scrape()
        assert jobs == []
        assert statistics.snapshot()["counters"]["linkedin.empty_pages"] == 1

    async def test_duplicate_page_signature_stops(self) -> None:
        same = page_html(*[card(n) for n in range(25)], next_page=True)
        scraper, manager, statistics = make_scraper({(K, 0): same, (K, 25): same})
        jobs = await scraper.scrape()
        assert len(jobs) == 25
        assert statistics.snapshot()["counters"]["linkedin.duplicate_pages"] == 1
        assert manager.navigations == [(K, 0), (K, 25)]

    async def test_max_pages_cap(self) -> None:
        pages = {
            (K, offset): page_html(
                *[card(offset + n) for n in range(25)], next_page=True
            )
            for offset in range(0, 250, 25)
        }
        scraper, manager, _ = make_scraper(pages, max_pages=3)
        jobs = await scraper.scrape()
        assert len(manager.navigations) == 3
        assert len(jobs) == 75


class TestAccessBlocking:
    async def test_login_wall_stops_run_returns_collected(self) -> None:
        scraper, manager, statistics = make_scraper(
            {
                ("Product Designer", 0): page_html(card(1)),
                ("UX Designer", 0): page_html(authwall=True),
            },
            keywords="Product Designer,UX Designer,UI Designer",
        )
        jobs = await scraper.scrape()
        assert len(jobs) == 1
        # third keyword never attempted — the run stopped
        assert ("UI Designer", 0) not in manager.navigations
        assert statistics.snapshot()["counters"]["linkedin.access_blocked"] == 1

    async def test_captcha_stops_run(self) -> None:
        scraper, manager, _ = make_scraper(
            {
                ("Product Designer", 0): page_html(captcha=True),
                ("UX Designer", 0): page_html(card(2)),
            },
            keywords="Product Designer,UX Designer",
        )
        jobs = await scraper.scrape()
        assert jobs == []
        assert ("UX Designer", 0) not in manager.navigations


class TestFailureRecovery:
    async def test_navigation_timeout_retried_then_succeeds(self) -> None:
        attempts: list[object] = [
            PlaywrightTimeoutError("timeout"),
            PlaywrightTimeoutError("timeout"),
            page_html(card(1)),
        ]
        scraper, manager, _ = make_scraper({(K, 0): attempts})
        jobs = await scraper.scrape()
        assert len(jobs) == 1
        assert manager.navigations.count((K, 0)) == 3

    async def test_persistent_timeout_moves_to_next_keyword(self) -> None:
        scraper, manager, statistics = make_scraper(
            {
                ("Product Designer", 0): [PlaywrightTimeoutError("t")] * 10,
                ("UX Designer", 0): page_html(card(2)),
            },
            keywords="Product Designer,UX Designer",
        )
        jobs = await scraper.scrape()
        assert len(jobs) == 1
        assert statistics.snapshot()["counters"]["linkedin.navigation_failures"] == 1

    async def test_browser_crash_restarts_and_resumes(self) -> None:
        scraper, manager, _ = make_scraper(
            {
                ("Product Designer", 0): PlaywrightError("browser has crashed"),
                ("UX Designer", 0): page_html(card(2)),
            },
            keywords="Product Designer,UX Designer",
        )
        jobs = await scraper.scrape()
        assert manager.restart_calls == 1
        assert len(jobs) == 1

    async def test_run_never_raises(self) -> None:
        scraper, _, _ = make_scraper({(K, 0): PlaywrightError("dead")})
        jobs = await scraper.run()
        assert jobs == []


class TestBrowserLifecycle:
    async def test_lazy_start_and_stop(self) -> None:
        scraper, manager, _ = make_scraper({(K, 0): page_html(card(1))})
        await scraper.scrape()
        assert manager.start_calls == 1
        assert manager.stop_calls == 1
        assert not manager.started

    async def test_pre_started_browser_not_stopped(self) -> None:
        scraper, manager, _ = make_scraper({(K, 0): page_html(card(1))})
        await manager.start()
        await scraper.scrape()
        assert manager.stop_calls == 0
        assert manager.started
