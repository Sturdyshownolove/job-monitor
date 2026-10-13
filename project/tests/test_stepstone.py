"""StepStone scraper tests. HTTP fully mocked via httpx.MockTransport."""

from __future__ import annotations

import random
from urllib.parse import parse_qs, urlsplit

import httpx

from config import Config
from html_parser import HTMLParser
from normalizer import Normalizer
from rate_limiter import RateLimiter
from retry import RetryPolicy
from scrapers.stepstone import StepStoneScraper
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
    posted: str | None = "vor 3 Tagen",
    href: str | None = None,
) -> str:
    link = href if href is not None else f"/stellenangebote--Job-{n}--12345{n}-inline.html"
    parts = ['<article data-testid="job-item">']
    if title is not None:
        parts.append(f'<a data-testid="job-item-title" href="{link}">{title}</a>')
    elif link:
        parts.append(f'<a data-testid="job-item-title" href="{link}"></a>')
    if company is not None:
        parts.append(f'<span data-at="job-item-company-name">{company}</span>')
    if location is not None:
        parts.append(f'<span data-at="job-item-location">{location}</span>')
    if posted is not None:
        parts.append(f'<time data-at="job-item-timestamp">{posted}</time>')
    parts.append("</article>")
    return "".join(parts)


def page(*cards_html: str, next_page: bool = False, blocked: bool = False) -> str:
    if blocked:
        return "<html><head><title>Access Denied</title></head><body>Denied</body></html>"
    nav = (
        '<a data-testid="pagination-link-next" href="#">Weiter</a>' if next_page else ""
    )
    return (
        "<html><body>"
        f'<div data-at="resultlist">{"".join(cards_html)}</div>{nav}'
        "</body></html>"
    )


EMPTY_PAGE = page()


def route(request: httpx.Request) -> tuple[str, int]:
    parts = urlsplit(str(request.url))
    slug = parts.path.split("/jobs/")[-1].split("/")[0]
    params = parse_qs(parts.query)
    return slug, int(params.get("page", ["1"])[0])


def make_handler(pages: dict[tuple[str, int], object]):
    calls: list[tuple[str, int]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        key = route(request)
        calls.append(key)
        entry = pages.get(key, EMPTY_PAGE)
        if isinstance(entry, int):
            return httpx.Response(entry)
        return httpx.Response(200, text=str(entry), headers={"content-type": "text/html"})

    handler.calls = calls  # type: ignore[attr-defined]
    return handler


def make_scraper(
    handler,
    keywords: str = "Product Designer",
    max_pages: int = 20,
) -> tuple[StepStoneScraper, StatisticsCollector]:
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

    scraper = StepStoneScraper(
        config,
        statistics,
        SessionManager(config, statistics),
        RateLimiter(
            config, statistics, rng=random.Random(42), clock=lambda: 0.0, sleep=instant_sleep
        ),
        RetryPolicy(config, statistics, sleep=instant_sleep),
        UserAgentProvider(config, rng=random.Random(42)),
        HTMLParser(),
        Normalizer(),
        transport=httpx.MockTransport(handler),
    )
    return scraper, statistics


K = "product-designer"


class TestUrlBuilder:
    def test_basic(self) -> None:
        url = StepStoneScraper.build_search_url("Product Designer")
        assert url.startswith("https://www.stepstone.de/jobs/product-designer/in-deutschland?")
        assert "sort=2" in url
        assert "page=" not in url

    def test_page(self) -> None:
        assert "page=3" in StepStoneScraper.build_search_url("UX Designer", page=3)

    def test_keyword_slugified(self) -> None:
        assert "/jobs/ux-ui-designer/" in StepStoneScraper.build_search_url("UX/UI Designer")


class TestExtraction:
    async def test_valid_card(self) -> None:
        scraper, _ = make_scraper(make_handler({(K, 1): page(card(1))}))
        jobs = await scraper.scrape()
        assert len(jobs) == 1
        job = jobs[0]
        assert job.title == "Senior Product Designer"
        assert job.company == "Miro"
        assert job.location == "Berlin"
        assert job.source == "StepStone"
        assert job.posted_time == "vor 3 Tagen"
        assert job.url == (
            "https://www.stepstone.de/stellenangebote--Job-1--123451-inline.html"
        )

    async def test_tracking_query_stripped(self) -> None:
        scraper, _ = make_scraper(
            make_handler(
                {(K, 1): page(card(1, href="/stellenangebote--X--99-inline.html?rltr=5_5_25"))}
            )
        )
        jobs = await scraper.scrape()
        assert jobs[0].url == "https://www.stepstone.de/stellenangebote--X--99-inline.html"

    async def test_missing_company_skips_card(self) -> None:
        scraper, statistics = make_scraper(
            make_handler({(K, 1): page(card(1, company=None), card(2))})
        )
        jobs = await scraper.scrape()
        assert len(jobs) == 1
        assert statistics.snapshot()["counters"]["stepstone.cards_skipped"] == 1


class TestBlocking:
    async def test_http_403_without_browser_aborts_run(self) -> None:
        scraper, statistics = make_scraper(make_handler({(K, 1): 403}))
        jobs = await scraper.scrape()
        assert jobs == []
        assert statistics.snapshot()["counters"]["stepstone.access_blocked"] == 1

    async def test_access_denied_page_aborts_run(self) -> None:
        scraper, statistics = make_scraper(make_handler({(K, 1): page(blocked=True)}))
        jobs = await scraper.scrape()
        assert jobs == []
        assert statistics.snapshot()["counters"]["stepstone.access_blocked"] == 1

    async def test_block_on_second_keyword_keeps_first_results(self) -> None:
        handler = make_handler(
            {
                ("product-designer", 1): page(card(1)),
                ("ux-designer", 1): 403,
            }
        )
        scraper, _ = make_scraper(handler, keywords="Product Designer,UX Designer")
        jobs = await scraper.scrape()
        assert len(jobs) == 1


class TestPagination:
    async def test_next_page_followed(self) -> None:
        handler = make_handler(
            {
                (K, 1): page(card(1), next_page=True),
                (K, 2): page(card(2)),
            }
        )
        scraper, _ = make_scraper(handler)
        jobs = await scraper.scrape()
        assert len(jobs) == 2
        assert handler.calls == [(K, 1), (K, 2)]

    async def test_empty_results_stop(self) -> None:
        scraper, statistics = make_scraper(make_handler({(K, 1): EMPTY_PAGE}))
        jobs = await scraper.scrape()
        assert jobs == []
        assert statistics.snapshot()["counters"]["stepstone.empty_pages"] == 1
