"""StepStone Germany scraper.

Same shape as the Indeed scraper: HTTP-first (httpx through the shared
framework), Playwright fallback when the HTTP path is blocked or the results
container is missing.

StepStone sits behind Akamai bot protection and currently answers 403
("Access Denied") to both plain HTTP and headless-browser requests. The
scraper detects that state, counts it, and aborts the run cleanly — the
application keeps working on the other sources, and this scraper starts
producing jobs the moment the block is lifted. No evasion is attempted.

Output: list[Job] with source="StepStone".
"""

from __future__ import annotations

import re
from urllib.parse import urlencode, urljoin, urlsplit, urlunsplit

import httpx
from bs4 import Tag

from base_scraper import BaseScraper
from browser import BrowserManager
from config import Config
from html_parser import HTMLParser, ParsedHTML, chain_select_all, chain_select_one
from http_client import HTTPClient
from logger import logger
from models import Job
from normalizer import Normalizer
from rate_limiter import RateLimiter
from retry import RetryPolicy
from session_manager import SessionManager
from stats import StatisticsCollector
from user_agent import UserAgentProvider

_BASE = "https://www.stepstone.de"
_LOCATION_SLUG = "in-deutschland"
_PAGE_SIZE_HINT = 25

SELECTORS: dict[str, tuple[str, ...]] = {
    "results_container": (
        "[data-testid='job-item-container']",
        "[data-at='resultlist']",
        "main",
    ),
    "job_card": (
        "article[data-testid='job-item']",
        "article[data-at='job-item']",
        "article",
    ),
    "title": (
        "[data-testid='job-item-title']",
        "[data-at='job-item-title']",
        "h2 a",
    ),
    "company": (
        "[data-at='job-item-company-name']",
        "[data-testid='job-item-company-name']",
        ".company",
    ),
    "location": (
        "[data-at='job-item-location']",
        "[data-testid='job-item-location']",
        ".location",
    ),
    "posted_time": (
        "[data-at='job-item-timestamp']",
        "time",
    ),
    "link": (
        "a[data-testid='job-item-title']",
        "a[data-at='job-item-title']",
        "h2 a[href]",
        "a[href*='/stellenangebote--']",
    ),
    "next_page": (
        "a[data-testid='pagination-link-next']:not([aria-disabled='true'])",
        "a[aria-label='Nächste']",
        "a[rel='next']",
    ),
}

_BLOCKED_TITLE_MARKERS: tuple[str, ...] = ("access denied", "zugriff verweigert")
_KEYWORD_SLUG_RE = re.compile(r"[^a-z0-9]+")


class AccessBlocked(Exception):
    """Akamai wall — stop the whole run, other scrapers continue."""


class StepStoneScraper(BaseScraper):
    """Discovers Product Design jobs on StepStone Germany."""

    name = "StepStone"

    def __init__(
        self,
        config: Config,
        statistics: StatisticsCollector,
        session_manager: SessionManager,
        rate_limiter: RateLimiter,
        retry_policy: RetryPolicy,
        user_agent_provider: UserAgentProvider,
        html_parser: HTMLParser,
        normalizer: Normalizer,
        browser_manager: BrowserManager | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        super().__init__(config, statistics)
        self._session_manager = session_manager
        self._rate_limiter = rate_limiter
        self._retry_policy = retry_policy
        self._user_agent_provider = user_agent_provider
        self._html_parser = html_parser
        self._normalizer = normalizer
        self._browser_manager = browser_manager
        self._transport = transport
        self._stop_browser_after_run = False

    # -- main flow -----------------------------------------------------------

    async def scrape(self) -> list[Job]:
        jobs: list[Job] = []
        seen_urls: set[str] = set()
        client = HTTPClient(
            self._config,
            self._session_manager,
            self._rate_limiter,
            self._retry_policy,
            self._user_agent_provider,
            self._statistics,
            transport=self._transport,
        )
        try:
            async with client:
                for keyword in self._config.keywords:
                    try:
                        await self._scrape_keyword(client, keyword, jobs, seen_urls)
                    except AccessBlocked:
                        logger.warning(
                            "StepStone access blocked (bot wall) — stopping run with {} jobs",
                            len(jobs),
                        )
                        self._count("access_blocked")
                        break
        finally:
            await self._shutdown_browser()
        return jobs

    async def _scrape_keyword(
        self, client: HTTPClient, keyword: str, jobs: list[Job], seen_urls: set[str]
    ) -> None:
        for page_number in range(1, self._config.max_pages + 1):
            url = self.build_search_url(keyword, page=page_number)
            document = await self._get_page_document(client, url)
            if document is None:
                raise AccessBlocked
            if self._is_blocked(document):
                raise AccessBlocked
            cards = self._select_cards(document)
            self._count("pages_visited")
            if not cards:
                self._count("empty_pages")
                break
            for card in cards:
                job = self._extract_card(document, card)
                if job is None:
                    continue
                if job.url in seen_urls:
                    self._count("duplicate_cards")
                    continue
                seen_urls.add(job.url)
                jobs.append(job)
            if not self._has_next_page(document, len(cards)):
                break

    # -- URL builder ---------------------------------------------------------

    @staticmethod
    def build_search_url(keyword: str, page: int = 1) -> str:
        slug = _KEYWORD_SLUG_RE.sub("-", keyword.lower()).strip("-")
        params: dict[str, str] = {"sort": "2", "action": "sort_publish"}
        if page > 1:
            params["page"] = str(page)
        return f"{_BASE}/jobs/{slug}/{_LOCATION_SLUG}?{urlencode(params)}"

    # -- fetching with Playwright fallback ------------------------------------

    async def _get_page_document(self, client: HTTPClient, url: str) -> ParsedHTML | None:
        html = await self._fetch_http(client, url)
        if html is not None:
            document = self._html_parser.parse(html)
            if not self._is_blocked(document):
                return document
        browser_html = await self._fetch_browser(url)
        if browser_html is not None:
            self._count("playwright_fallbacks")
            return self._html_parser.parse(browser_html)
        return None

    async def _fetch_http(self, client: HTTPClient, url: str) -> str | None:
        try:
            response = await client.get(url)
            return response.text
        except httpx.HTTPStatusError as exc:
            status = exc.response.status_code
            if status == 403:
                logger.warning("StepStone blocked the request (HTTP 403): {}", url)
            else:
                logger.warning("StepStone HTTP {} for {}", status, url)
            return None
        except Exception as exc:
            logger.warning("StepStone request failed for {}: {}", url, exc)
            return None

    async def _fetch_browser(self, url: str) -> str | None:
        if self._browser_manager is None:
            return None
        if not await self._ensure_browser():
            return None
        try:
            async with self._browser_manager.page_session() as page:
                await self._browser_manager.navigate(page, url)
                return await page.content()
        except Exception as exc:
            logger.warning("StepStone Playwright fallback failed for {}: {}", url, exc)
            return None

    async def _ensure_browser(self) -> bool:
        manager = self._browser_manager
        if manager is None:
            return False
        if manager.started:
            return True
        try:
            await manager.start()
            self._stop_browser_after_run = True
            return True
        except Exception as exc:
            logger.warning("StepStone: browser start failed: {}", exc)
            return False

    async def _shutdown_browser(self) -> None:
        if self._stop_browser_after_run and self._browser_manager is not None:
            await self._browser_manager.stop()
            self._stop_browser_after_run = False

    # -- page inspection -----------------------------------------------------

    def _is_blocked(self, document: ParsedHTML) -> bool:
        title = document.root.find("title")
        text = (title.get_text() if title else "").strip().lower()
        if any(marker in text for marker in _BLOCKED_TITLE_MARKERS):
            return True
        return self._find_container(document) is None

    def _find_container(self, document: ParsedHTML) -> Tag | None:
        for selector in SELECTORS["results_container"]:
            element = document.select_one(selector)
            if element is not None:
                return element
        return None

    def _select_cards(self, document: ParsedHTML) -> list[Tag]:
        container = self._find_container(document)
        if container is None:
            return []
        found = chain_select_all(container, SELECTORS["job_card"])
        if found is None:
            return []
        cards, index = found
        if index > 0:
            self._count("selector_fallbacks")
        self._statistics.increment("stepstone.cards_seen", len(cards))
        return cards

    def _has_next_page(self, document: ParsedHTML, cards_on_page: int) -> bool:
        for selector in SELECTORS["next_page"]:
            if document.exists(selector):
                return True
        return cards_on_page >= _PAGE_SIZE_HINT

    # -- card extraction ------------------------------------------------------

    def _extract_card(self, document: ParsedHTML, card: Tag) -> Job | None:
        url = self._extract_url(card)
        if url is None:
            return self._skip("missing url")
        title = self._normalizer.normalize_title(self._chain_text(document, card, "title"))
        if title is None:
            return self._skip("missing title")
        company = self._normalizer.normalize_company(
            self._chain_text(document, card, "company")
        )
        if company is None:
            return self._skip("missing company")
        location = self._normalizer.normalize_text(
            self._chain_text(document, card, "location")
        )
        if location is None:
            return self._skip("missing location")
        posted_time = self._normalizer.normalize_text(
            self._chain_text(document, card, "posted_time")
        )
        try:
            job = Job.create(
                title=title,
                company=company,
                location=location,
                url=url,
                source="StepStone",
                posted_time=posted_time,
            )
        except Exception as exc:
            return self._skip(f"validation failed: {exc}")
        self._count("jobs_extracted")
        return job

    def _extract_url(self, card: Tag) -> str | None:
        link = self._query_chain(card, "link")
        if link is None:
            return None
        href = link.get("href")
        if not href:
            return None
        absolute = urljoin(_BASE + "/", str(href))
        return self._canonicalize_url(absolute)

    def _canonicalize_url(self, url: str) -> str | None:
        """Job path without query — StepStone appends volatile tracking params."""
        normalized = self._normalizer.normalize_url(url)
        if normalized is None:
            return None
        parts = urlsplit(normalized)
        return urlunsplit((parts.scheme, parts.netloc, parts.path, "", ""))

    def _query_chain(self, scope: Tag, chain_name: str) -> Tag | None:
        found = chain_select_one(scope, SELECTORS[chain_name])
        if found is None:
            return None
        element, index = found
        if index > 0:
            self._count("selector_fallbacks")
        return element

    def _chain_text(self, document: ParsedHTML, scope: Tag, chain_name: str) -> str | None:
        element = self._query_chain(scope, chain_name)
        if element is None:
            return None
        return document.text(element)

    # -- helpers -------------------------------------------------------------

    def _skip(self, reason: str) -> None:
        logger.debug("StepStone card skipped ({})", reason)
        self._count("cards_skipped")
        return None

    def _count(self, name: str) -> None:
        self._statistics.increment(f"stepstone.{name}")
