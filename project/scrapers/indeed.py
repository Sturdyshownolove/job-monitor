"""Indeed Germany scraper (Scrapers/Indeed/* specifications).

HTTP-first (httpx + BeautifulSoup through the shared framework), Playwright
fallback when the HTTP path is blocked (403) or returns HTML without the
results container. Extraction is deterministic; every selector has a
fallback chain and lives only in the SELECTORS registry.

The scraper extracts and normalizes — it never filters, never touches the
database, never notifies. Output: list[Job] with source="Indeed".
"""

from __future__ import annotations

from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit

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

_BASE = "https://de.indeed.com"
_SEARCH_URL = f"{_BASE}/jobs"
_VIEWJOB_URL = f"{_BASE}/viewjob?jk={{jk}}"
_PAGE_SIZE = 10  # Search URLs.md: ~10-15 jobs per page, offsets step by 10
_LOCATION = "Germany"

# Selector registry (Selectors.md): primary first, fallbacks after.
SELECTORS: dict[str, tuple[str, ...]] = {
    "results_container": (
        "#mosaic-provider-jobcards",
        "ul.jobsearch-ResultsList",
        "td#resultsCol",
    ),
    "job_card": ("div.job_seen_beacon", "li div.cardOutline", "div.result"),
    "title": (
        "h2.jobTitle span[title]",
        "h2.jobTitle",
        "h3.jobTitle span[title]",
        "h3.jobTitle",
        "[data-testid='jobTitle']",
        "a[id^='job_']",
    ),
    "company": ("[data-testid='company-name']", "span.companyName", ".company"),
    "location": ("[data-testid='text-location']", "div.companyLocation", ".location"),
    "link": ("h2.jobTitle a[href]", "h3.jobTitle a[href]", "a[data-jk]", "a[id^='job_']"),
    "posted_time": ("[data-testid='myJobsStateDate']", "span.date", ".date"),
    "next_page": (
        "a[data-testid='pagination-page-next']",
        "a[aria-label='Next Page']",
        "a[aria-label='Nächste Seite']",
        "nav[role='navigation'] a[rel='next']",
    ),
    "sponsored": ("[data-testid='sponsoredJob']", ".sponsoredGray", ".sponTapItem-ad"),
}

_CAPTCHA_SELECTORS: tuple[str, ...] = (
    "iframe[src*='captcha']",
    ".g-recaptcha",
    "#challenge-running",
)
_CAPTCHA_TEXT_MARKERS: tuple[str, ...] = (
    "verify you are a human",
    "verifizieren sie, dass sie ein mensch sind",
)


class CaptchaDetected(Exception):
    """Raised internally to stop the whole run (Failure Cases: CAPTCHA)."""


class IndeedScraper(BaseScraper):
    """Discovers Product Design jobs on Indeed Germany."""

    name = "Indeed"

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
                    except CaptchaDetected:
                        logger.critical(
                            "Indeed CAPTCHA detected — stopping run, returning {} jobs",
                            len(jobs),
                        )
                        self._count("captcha_detected")
                        break
        finally:
            await self._shutdown_browser()
        return jobs

    async def _scrape_keyword(
        self, client: HTTPClient, keyword: str, jobs: list[Job], seen_urls: set[str]
    ) -> None:
        visited: set[str] = set()  # duplicate-page registry (Pagination.md)
        for page_index in range(self._config.max_pages):
            url = self.build_search_url(keyword, offset=page_index * _PAGE_SIZE)
            if url in visited:
                break
            visited.add(url)
            document = await self._get_page_document(client, url)
            if document is None:
                logger.warning("Indeed: aborting keyword '{}' — page unusable", keyword)
                break
            if self._detect_captcha(document):
                raise CaptchaDetected
            container = self._find_container(document)
            if container is None:
                logger.warning(
                    "Indeed results container missing for '{}' — layout change?", keyword
                )
                self._count("layout_warnings")
                break
            self._count("pages_visited")
            cards = self._select_cards(container)
            if not cards:
                logger.info("Indeed: empty result page for '{}' — pagination done", keyword)
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

    # -- URL builder (Search URLs.md) ---------------------------------------

    @staticmethod
    def build_search_url(keyword: str, offset: int = 0) -> str:
        params: dict[str, str] = {"q": keyword, "l": _LOCATION, "sort": "date"}
        if offset > 0:
            params["start"] = str(offset)
        return f"{_SEARCH_URL}?{urlencode(params)}"

    # -- fetching with Playwright fallback ----------------------------------

    async def _get_page_document(self, client: HTTPClient, url: str) -> ParsedHTML | None:
        html = await self._fetch_http(client, url)
        if html is not None:
            document = self._html_parser.parse(html)
            if self._find_container(document) is not None or self._detect_captcha(document):
                return document
            logger.warning("Indeed HTTP response unusable for {} — trying Playwright", url)
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
                logger.warning("Indeed blocked the request (HTTP 403): {}", url)
            else:
                logger.warning("Indeed HTTP {} for {}", status, url)
            return None
        except Exception as exc:
            logger.warning("Indeed request failed for {}: {}", url, exc)
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
            logger.warning("Indeed Playwright fallback failed for {}: {}", url, exc)
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
            logger.warning("Indeed: browser start failed: {}", exc)
            return False

    async def _shutdown_browser(self) -> None:
        if self._stop_browser_after_run and self._browser_manager is not None:
            await self._browser_manager.stop()
            self._stop_browser_after_run = False

    # -- page inspection -----------------------------------------------------

    def _find_container(self, document: ParsedHTML) -> Tag | None:
        for index, selector in enumerate(SELECTORS["results_container"]):
            element = document.select_one(selector)
            if element is not None:
                if index > 0:
                    self._count("selector_fallbacks")
                return element
        return None

    def _select_cards(self, container: Tag) -> list[Tag]:
        found = chain_select_all(container, SELECTORS["job_card"])
        if found is None:
            return []
        cards, index = found
        if index > 0:
            self._count("selector_fallbacks")
        self._statistics.increment("indeed.cards_seen", len(cards))
        return cards

    def _detect_captcha(self, document: ParsedHTML) -> bool:
        if any(document.exists(selector) for selector in _CAPTCHA_SELECTORS):
            return True
        text = (document.text() or "").lower()
        return any(marker in text for marker in _CAPTCHA_TEXT_MARKERS)

    def _has_next_page(self, document: ParsedHTML, cards_on_page: int) -> bool:
        for selector in SELECTORS["next_page"]:
            if document.exists(selector):
                return True
        # No next control: a partial page means the results are exhausted.
        return cards_on_page >= _PAGE_SIZE

    # -- card extraction (Extraction Rules.md) -------------------------------

    def _extract_card(self, document: ParsedHTML, card: Tag) -> Job | None:
        if self._is_hidden_clone(card):
            self._count("hidden_clones_skipped")
            return None
        if self._is_advert(card):
            self._count("adverts_skipped")
            return None
        # URL first: it uniquely identifies the job; without it, skip immediately.
        url = self._extract_url(card)
        if url is None:
            return self._skip(card, "missing url")
        title = self._normalizer.normalize_title(self._chain_text(document, card, "title"))
        if title is None:
            return self._skip(card, "missing title")
        company = self._normalizer.normalize_company(
            self._chain_text(document, card, "company")
        )
        if company is None:
            return self._skip(card, "missing company")
        location = self._normalizer.normalize_text(
            self._chain_text(document, card, "location")
        )
        if location is None:
            return self._skip(card, "missing location")
        posted_time = self._normalizer.normalize_text(
            self._chain_text(document, card, "posted_time")
        )
        try:
            job = Job.create(
                title=title,
                company=company,
                location=location,
                url=url,
                source="Indeed",
                posted_time=posted_time,
            )
        except Exception as exc:
            return self._skip(card, f"validation failed: {exc}")
        self._count("jobs_extracted")
        return job

    def _extract_url(self, card: Tag) -> str | None:
        link = self._query_chain(card, "link")
        if link is None:
            return None
        # data-jk first: pagead hrefs carry no jk query param, only volatile
        # ad/session tokens — canonicalizing them would yield a new URL every
        # scrape and defeat dedup. The data-jk attribute is the stable key.
        job_key = link.get("data-jk")
        if job_key:
            return _VIEWJOB_URL.format(jk=job_key)
        href = link.get("href")
        if not href:
            return None
        return self._canonicalize_url(urljoin(_BASE + "/", str(href)))

    def _canonicalize_url(self, url: str) -> str | None:
        """Canonical Indeed job URL.

        Indeed attaches per-request session tokens (`bb=`, `xkcb=`, …) to
        result links — every scrape yields a different URL for the same job,
        which would defeat hash-based deduplication. The `jk` parameter is the
        stable job key, so any URL carrying one collapses to
        viewjob?jk=<key> (Extraction Rules: remove session IDs).
        """
        params = dict(parse_qsl(urlsplit(url).query))
        job_key = params.get("jk")
        if job_key:
            return _VIEWJOB_URL.format(jk=job_key)
        return self._normalizer.normalize_url(url)

    def _is_advert(self, card: Tag) -> bool:
        return any(card.select_one(selector) for selector in SELECTORS["sponsored"])

    def _is_hidden_clone(self, card: Tag) -> bool:
        """Decoy detection.

        Indeed duplicates some result cards inside an `aria-hidden="true"`
        wrapper whose data-jk/href carry a decoy job key (e.g. rotations of
        "0123456789abcdef"); the visible twin holds the real key. Extracting
        the clone yields a dead viewjob link, and — because the URL differs
        from the real card's — a duplicate notification.
        """
        if card.get("aria-hidden") == "true":
            return True
        return card.find_parent(attrs={"aria-hidden": "true"}) is not None

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

    def _skip(self, card: Tag, reason: str) -> None:
        logger.debug("Indeed card skipped ({})", reason)
        self._count("cards_skipped")
        return None

    def _count(self, name: str) -> None:
        self._statistics.increment(f"indeed.{name}")
