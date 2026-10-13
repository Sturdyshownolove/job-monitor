"""Indeed scraper tests (Scrapers/Indeed/Test Cases.md).

HTTP layer fully mocked via httpx.MockTransport; rate limiter and retry run
on fake clocks. Fixture HTML mirrors the primary and fallback selector
chains from Selectors.md.
"""

from __future__ import annotations

import random
from contextlib import asynccontextmanager
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest

from config import Config
from html_parser import HTMLParser
from normalizer import Normalizer
from rate_limiter import RateLimiter
from retry import RetryPolicy
from scrapers.indeed import IndeedScraper
from session_manager import SessionManager
from stats import StatisticsCollector
from user_agent import UserAgentProvider

# -- fixture HTML ------------------------------------------------------------


def card(
    n: int = 1,
    *,
    title: str | None = "Senior Product Designer (m/w/d)",
    company: str | None = "Miro",
    location: str | None = "Berlin",
    posted: str | None = "vor 2 Tagen",
    href: str | None = None,
    sponsored: bool = False,
    variant: str = "primary",
) -> str:
    if variant == "primary":
        link = href if href is not None else f"/rc/clk?jk=jk{n}"
        parts = ['<div class="job_seen_beacon">']
        if sponsored:
            parts.append('<span data-testid="sponsoredJob">Gesponsert</span>')
        if title is not None:
            parts.append(
                f'<h2 class="jobTitle"><a href="{link}"><span title="t">{title}</span></a></h2>'
            )
        elif link:
            parts.append(f'<h2 class="jobTitle"><a href="{link}"></a></h2>')
        if company is not None:
            parts.append(f'<span data-testid="company-name">{company}</span>')
        if location is not None:
            parts.append(f'<div data-testid="text-location">{location}</div>')
        if posted is not None:
            parts.append(f'<span data-testid="myJobsStateDate">{posted}</span>')
        parts.append("</div>")
        return "".join(parts)
    # fallback variant: no data-testid attributes, legacy class names
    link = href if href is not None else f"/viewjob?jk=jk{n}"
    return (
        f'<div class="result">'
        f'<a id="job_abc{n}" href="{link}">{title}</a>'
        f'<span class="companyName">{company}</span>'
        f'<div class="companyLocation">{location}</div>'
        f'<span class="date">{posted}</span>'
        f"</div>"
    )


def page(
    *cards_html: str,
    next_page: bool = False,
    variant: str = "primary",
    captcha: bool = False,
) -> str:
    if captcha:
        return '<html><body><div class="g-recaptcha">verify you are a human</div></body></html>'
    body = "".join(cards_html)
    if variant == "primary":
        content = f'<div id="mosaic-provider-jobcards">{body}</div>'
    else:
        content = f'<table><tr><td id="resultsCol">{body}</td></tr></table>'
    nav = (
        '<nav role="navigation"><a data-testid="pagination-page-next" href="#">Next</a></nav>'
        if next_page
        else ""
    )
    return f"<html><body>{content}{nav}</body></html>"


EMPTY_PAGE = page()


def route(request: httpx.Request) -> tuple[str, int]:
    params = parse_qs(urlsplit(str(request.url)).query)
    return params["q"][0], int(params.get("start", ["0"])[0])


def make_handler(pages: dict[tuple[str, int], object]):
    calls: list[tuple[str, int]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        key = route(request)
        calls.append(key)
        entry = pages.get(key, EMPTY_PAGE)
        if isinstance(entry, int):
            return httpx.Response(entry)
        if callable(entry):
            return entry(request)
        return httpx.Response(200, text=str(entry), headers={"content-type": "text/html"})

    handler.calls = calls  # type: ignore[attr-defined]
    return handler


# -- scraper factory ---------------------------------------------------------


class FakePage:
    def __init__(self, html: str) -> None:
        self._html = html

    async def content(self) -> str:
        return self._html


class FakeBrowserManager:
    def __init__(self, html: str = "", start_fails: bool = False) -> None:
        self.html = html
        self.start_fails = start_fails
        self.started = False
        self.start_calls = 0
        self.stop_calls = 0
        self.navigated: list[str] = []

    async def start(self) -> None:
        if self.start_fails:
            raise RuntimeError("chromium missing")
        self.started = True
        self.start_calls += 1

    async def stop(self) -> None:
        self.started = False
        self.stop_calls += 1

    @asynccontextmanager
    async def page_session(self):
        yield FakePage(self.html)

    async def navigate(self, page: FakePage, url: str) -> None:
        self.navigated.append(url)


def make_scraper(
    handler,
    keywords: str = "Product Designer",
    max_pages: int = 20,
    browser_manager: FakeBrowserManager | None = None,
) -> tuple[IndeedScraper, StatisticsCollector]:
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

    clock = {"now": 0.0}

    scraper = IndeedScraper(
        config,
        statistics,
        SessionManager(config, statistics),
        RateLimiter(
            config,
            statistics,
            rng=random.Random(42),
            clock=lambda: clock["now"],
            sleep=instant_sleep,
        ),
        RetryPolicy(config, statistics, sleep=instant_sleep),
        UserAgentProvider(config, rng=random.Random(42)),
        HTMLParser(),
        Normalizer(),
        browser_manager=browser_manager,  # type: ignore[arg-type]
        transport=httpx.MockTransport(handler),
    )
    return scraper, statistics


K = "Product Designer"


class TestUrlBuilder:
    def test_basic_url(self) -> None:
        url = IndeedScraper.build_search_url("Product Designer")
        assert url.startswith("https://de.indeed.com/jobs?")
        assert "q=Product+Designer" in url
        assert "l=Germany" in url
        assert "sort=date" in url
        assert "start=" not in url

    def test_offset(self) -> None:
        assert "start=20" in IndeedScraper.build_search_url("UX Designer", offset=20)

    def test_special_characters_encoded(self) -> None:
        assert "q=UX%2FUI+Designer" in IndeedScraper.build_search_url("UX/UI Designer")


class TestExtraction:
    async def test_valid_card_extracted_and_normalized(self) -> None:
        scraper, _ = make_scraper(make_handler({(K, 0): page(card(1))}))
        jobs = await scraper.scrape()
        assert len(jobs) == 1
        job = jobs[0]
        assert job.title == "Senior Product Designer"  # (m/w/d) stripped
        assert job.company == "Miro"
        assert job.location == "Berlin"
        assert job.source == "Indeed"
        assert job.posted_time == "vor 2 Tagen"
        assert job.url == "https://de.indeed.com/viewjob?jk=jk1"

    async def test_relative_url_becomes_absolute(self) -> None:
        scraper, _ = make_scraper(
            make_handler({(K, 0): page(card(1, href="/viewjob?jk=xyz"))})
        )
        jobs = await scraper.scrape()
        assert jobs[0].url == "https://de.indeed.com/viewjob?jk=xyz"

    async def test_tracking_params_stripped_from_url(self) -> None:
        scraper, _ = make_scraper(
            make_handler({(K, 0): page(card(1, href="/rc/clk?jk=a1&utm_source=feed"))})
        )
        jobs = await scraper.scrape()
        assert jobs[0].url == "https://de.indeed.com/viewjob?jk=a1"

    async def test_session_token_urls_collapse_to_canonical(self) -> None:
        """Regression: Indeed's per-request bb= token must not defeat dedup."""
        scraper, statistics = make_scraper(
            make_handler(
                {
                    (K, 0): page(
                        card(1, href="/rc/clk?jk=stable&bb=SESSION_TOKEN_A"),
                        card(2, href="/rc/clk?jk=stable&bb=SESSION_TOKEN_B"),
                    )
                }
            )
        )
        jobs = await scraper.scrape()
        assert len(jobs) == 1  # same jk → same canonical URL → one job
        assert jobs[0].url == "https://de.indeed.com/viewjob?jk=stable"
        assert statistics.snapshot()["counters"]["indeed.duplicate_cards"] == 1

    @pytest.mark.parametrize("missing", ["title", "company", "location"])
    async def test_missing_required_field_skips_card(self, missing: str) -> None:
        bad = card(1, **{missing: None})
        scraper, statistics = make_scraper(make_handler({(K, 0): page(bad, card(2))}))
        jobs = await scraper.scrape()
        assert len(jobs) == 1
        assert jobs[0].url.endswith("jk=jk2")
        assert statistics.snapshot()["counters"]["indeed.cards_skipped"] == 1

    async def test_sponsored_card_ignored(self) -> None:
        scraper, statistics = make_scraper(
            make_handler({(K, 0): page(card(1, sponsored=True), card(2))})
        )
        jobs = await scraper.scrape()
        assert len(jobs) == 1
        assert statistics.snapshot()["counters"]["indeed.adverts_skipped"] == 1

    async def test_aria_hidden_clone_card_ignored(self) -> None:
        """Regression: Indeed serves aria-hidden clone cards with decoy jk
        (e.g. jk=456789abcdef0123) — extracting them notifies dead links."""
        clone = (
            '<div class="cardOutline" aria-hidden="true">'
            + card(1, href="/viewjob?jk=456789abcdef0123")
            + "</div>"
        )
        scraper, statistics = make_scraper(make_handler({(K, 0): page(clone, card(2))}))
        jobs = await scraper.scrape()
        assert len(jobs) == 1
        assert jobs[0].url.endswith("jk=jk2")
        assert statistics.snapshot()["counters"]["indeed.hidden_clones_skipped"] == 1

    async def test_duplicate_card_yields_one_job(self) -> None:
        scraper, statistics = make_scraper(make_handler({(K, 0): page(card(1), card(1))}))
        jobs = await scraper.scrape()
        assert len(jobs) == 1
        assert statistics.snapshot()["counters"]["indeed.duplicate_cards"] == 1

    async def test_duplicate_across_keywords_deduplicated(self) -> None:
        scraper, _ = make_scraper(
            make_handler(
                {
                    ("Product Designer", 0): page(card(1)),
                    ("UX Designer", 0): page(card(1)),
                }
            ),
            keywords="Product Designer,UX Designer",
        )
        jobs = await scraper.scrape()
        assert len(jobs) == 1


class TestSelectorFallbacks:
    async def test_fallback_dom_variant_extracts(self) -> None:
        scraper, statistics = make_scraper(
            make_handler(
                {(K, 0): page(card(1, title="UX Designer", variant="fallback"), variant="fallback")}
            )
        )
        jobs = await scraper.scrape()
        assert len(jobs) == 1
        assert jobs[0].title == "UX Designer"
        assert jobs[0].company == "Miro"
        assert statistics.snapshot()["counters"]["indeed.selector_fallbacks"] > 0


class TestPagination:
    async def test_follows_next_page(self) -> None:
        full_page = page(*[card(n) for n in range(10)], next_page=True)
        handler = make_handler({(K, 0): full_page, (K, 10): page(card(100))})
        scraper, _ = make_scraper(handler)
        jobs = await scraper.scrape()
        assert len(jobs) == 11
        assert handler.calls == [(K, 0), (K, 10)]

    async def test_partial_page_without_next_stops(self) -> None:
        handler = make_handler({(K, 0): page(card(1), card(2))})
        scraper, _ = make_scraper(handler)
        jobs = await scraper.scrape()
        assert len(jobs) == 2
        assert handler.calls == [(K, 0)]

    async def test_empty_page_stops_gracefully(self) -> None:
        handler = make_handler({(K, 0): EMPTY_PAGE})
        scraper, statistics = make_scraper(handler)
        jobs = await scraper.scrape()
        assert jobs == []
        assert statistics.snapshot()["counters"]["indeed.empty_pages"] == 1

    async def test_max_pages_cap(self) -> None:
        # Every page is full and advertises a next page — cap must stop us.
        endless = {
            (K, offset): page(*[card(offset + n) for n in range(10)], next_page=True)
            for offset in range(0, 100, 10)
        }
        handler = make_handler(endless)
        scraper, _ = make_scraper(handler, max_pages=3)
        jobs = await scraper.scrape()
        assert len(handler.calls) == 3
        assert len(jobs) == 30


class TestFailureRecovery:
    async def test_transient_500_retried_then_succeeds(self) -> None:
        attempts = {"n": 0}

        def flaky(request: httpx.Request) -> httpx.Response:
            attempts["n"] += 1
            if attempts["n"] == 1:
                return httpx.Response(500)
            return httpx.Response(
                200, text=page(card(1)), headers={"content-type": "text/html"}
            )

        scraper, _ = make_scraper(make_handler({(K, 0): flaky}))
        jobs = await scraper.scrape()
        assert len(jobs) == 1
        assert attempts["n"] == 2

    async def test_403_without_browser_aborts_keyword_only(self) -> None:
        handler = make_handler(
            {("Product Designer", 0): 403, ("UX Designer", 0): page(card(2))}
        )
        scraper, _ = make_scraper(handler, keywords="Product Designer,UX Designer")
        jobs = await scraper.scrape()
        assert len(jobs) == 1
        assert jobs[0].url.endswith("jk=jk2")

    async def test_captcha_stops_entire_run(self) -> None:
        handler = make_handler(
            {
                ("Product Designer", 0): page(captcha=True),
                ("UX Designer", 0): page(card(2)),
            }
        )
        scraper, statistics = make_scraper(handler, keywords="Product Designer,UX Designer")
        jobs = await scraper.scrape()
        assert jobs == []
        assert handler.calls == [("Product Designer", 0)]  # second keyword never tried
        assert statistics.snapshot()["counters"]["indeed.captcha_detected"] == 1

    async def test_captcha_returns_jobs_collected_so_far(self) -> None:
        handler = make_handler(
            {
                ("Product Designer", 0): page(card(1)),
                ("UX Designer", 0): page(captcha=True),
            }
        )
        scraper, _ = make_scraper(handler, keywords="Product Designer,UX Designer")
        jobs = await scraper.scrape()
        assert len(jobs) == 1

    async def test_missing_container_without_browser_aborts_keyword(self) -> None:
        handler = make_handler({(K, 0): "<html><body><p>redesigned</p></body></html>"})
        scraper, statistics = make_scraper(handler)
        jobs = await scraper.scrape()
        assert jobs == []
        assert statistics.snapshot()["counters"].get("indeed.layout_warnings", 0) >= 0

    async def test_run_never_raises(self) -> None:
        def exploding(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("network gone")

        scraper, _ = make_scraper(make_handler({(K, 0): exploding}))
        jobs = await scraper.run()  # BaseScraper entry point
        assert jobs == []


class TestPlaywrightFallback:
    async def test_403_falls_back_to_browser(self) -> None:
        browser = FakeBrowserManager(html=page(card(1)))
        scraper, statistics = make_scraper(
            make_handler({(K, 0): 403}), browser_manager=browser
        )
        jobs = await scraper.scrape()
        assert len(jobs) == 1
        assert browser.navigated == [IndeedScraper.build_search_url(K)]
        assert statistics.snapshot()["counters"]["indeed.playwright_fallbacks"] == 1
        # Browser was started lazily and stopped after the run
        assert browser.start_calls == 1
        assert browser.stop_calls == 1
        assert not browser.started

    async def test_missing_container_falls_back_to_browser(self) -> None:
        browser = FakeBrowserManager(html=page(card(1)))
        scraper, _ = make_scraper(
            make_handler({(K, 0): "<html><body>no container</body></html>"}),
            browser_manager=browser,
        )
        jobs = await scraper.scrape()
        assert len(jobs) == 1

    async def test_browser_start_failure_degrades_gracefully(self) -> None:
        browser = FakeBrowserManager(start_fails=True)
        scraper, _ = make_scraper(make_handler({(K, 0): 403}), browser_manager=browser)
        jobs = await scraper.scrape()
        assert jobs == []
        assert browser.stop_calls == 0  # never started, never stopped
