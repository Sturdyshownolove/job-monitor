"""LinkedIn scraper (Scrapers/LinkedIn/* + Core/Specifications/*).

Playwright-only (JS-heavy site), anonymous access, driven through
BrowserManager. Execution follows the documented state machine: navigate →
wait/stabilize (infinite scroll) → verify page (no login wall / CAPTCHA) →
extract cards → next page via offset with page-signature dedup.

Recovery per Error Matrix: navigation timeouts retry (RetryPolicy), browser
crashes restart the browser and resume with the next keyword, login wall or
CAPTCHA aborts the whole run returning jobs collected so far. Never attempts
CAPTCHA bypass.

The scraper extracts and normalizes only — no filtering, no database, no
Telegram. Output: list[Job] with source="LinkedIn".
"""

from __future__ import annotations

import hashlib
import re
from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit, urlunsplit

from bs4 import Tag
from playwright.async_api import Error as PlaywrightError
from playwright.async_api import Page
from playwright.async_api import TimeoutError as PlaywrightTimeoutError

from base_scraper import BaseScraper
from browser import BrowserManager
from config import Config
from html_parser import HTMLParser, ParsedHTML, chain_select_all, chain_select_one
from logger import logger
from models import Job
from normalizer import Normalizer
from rate_limiter import RateLimiter
from retry import RetryPolicy
from stats import StatisticsCollector

_BASE = "https://www.linkedin.com"
_SEARCH_URL = f"{_BASE}/jobs/search/"
_LOCATION = "Germany"
_PAGE_SIZE_HINT = 25  # offsets step by 25; actual card count is always detected
_SCROLL_WAIT_MS = 500
_DOMAIN = "www.linkedin.com"

# Selector registry (Selectors.md + DOM Change Strategy priority order).
# Primary selectors from the spec, guest-page variants as fallbacks.
SELECTORS: dict[str, tuple[str, ...]] = {
    "job_card": ("[data-job-id]", ".jobs-search-results__list-item", ".base-card"),
    "title": (
        ".job-card-list__title",
        ".base-search-card__title",
        "a[href*='/jobs/view/']",
    ),
    "company": (
        ".job-card-container__company-name",
        ".base-search-card__subtitle",
        ".subtitle",
    ),
    "location": (
        ".job-card-container__metadata-item",
        ".job-search-card__location",
        ".base-search-card__metadata",
    ),
    "posted_time": ("time",),
    "link": ("a[href*='/jobs/view/']", "a.base-card__full-link", "a[href]"),
    "next_page": (
        "button[aria-label='View next page']:not([disabled])",
        "button[aria-label='Next']:not([disabled])",
        ".artdeco-pagination__button--next:not([disabled])",
    ),
    "login_wall": (
        ".authwall",
        "form.join-form",
        ".sign-in-modal",
        "[data-tracking-control-name*='auth-wall']",
    ),
    "captcha": ("#captcha-internal", ".g-recaptcha", "iframe[src*='captcha']", "#challenge"),
}

# Field Mapping: titles that mark invalid/advert cards
_INVALID_TITLE_MARKERS: tuple[str, ...] = ("loading", "advertisement", "sponsored", "promoted")

_LOCATION_DEFAULT = "Unknown"  # Field Mapping default; the pipeline decides

# Trailing numeric posting ID on guest slug URLs, e.g.
# /jobs/view/senior-product-designer-at-acme-4435826450. The ID is the job's
# real, stable identifier: /jobs/view/<id> resolves on its own, while the
# slug without the ID returns 404.
_SLUG_POSTING_ID_RE = re.compile(r"-(\d{6,})$")


class AccessBlocked(Exception):
    """Login wall or CAPTCHA — stop the run, return what was collected."""


class LinkedInScraper(BaseScraper):
    """Discovers Product Design jobs on LinkedIn Jobs (anonymous)."""

    name = "LinkedIn"

    def __init__(
        self,
        config: Config,
        statistics: StatisticsCollector,
        browser_manager: BrowserManager,
        rate_limiter: RateLimiter,
        retry_policy: RetryPolicy,
        html_parser: HTMLParser,
        normalizer: Normalizer,
    ) -> None:
        super().__init__(config, statistics)
        self._browser_manager = browser_manager
        self._rate_limiter = rate_limiter
        self._retry_policy = retry_policy
        self._html_parser = html_parser
        self._normalizer = normalizer

    # -- main flow (State Machine.md) ----------------------------------------

    async def scrape(self) -> list[Job]:
        jobs: list[Job] = []
        seen_urls: set[str] = set()
        manager = self._browser_manager
        started_here = False
        if not manager.started:
            await manager.start()
            started_here = True
        context = await manager.new_context()
        page = await manager.new_page(context)
        try:
            for keyword in self._config.keywords:
                try:
                    await self._scrape_keyword(page, keyword, jobs, seen_urls)
                except AccessBlocked as exc:
                    logger.warning(
                        "LinkedIn access blocked ({}) — stopping run with {} jobs",
                        exc,
                        len(jobs),
                    )
                    self._count("access_blocked")
                    break
                except PlaywrightTimeoutError:
                    logger.warning(
                        "LinkedIn navigation kept timing out for '{}' — next keyword",
                        keyword,
                    )
                    self._count("navigation_failures")
                except PlaywrightError as exc:
                    # Browser/context crash: restart and resume (Error Matrix)
                    logger.warning("LinkedIn browser error ({}) — restarting browser", exc)
                    await self._close_quietly(page, context)
                    await manager.restart()
                    context = await manager.new_context()
                    page = await manager.new_page(context)
        finally:
            await self._close_quietly(page, context)
            if started_here:
                await manager.stop()
        return jobs

    async def _scrape_keyword(
        self, page: Page, keyword: str, jobs: list[Job], seen_urls: set[str]
    ) -> None:
        signatures: set[str] = set()
        for page_index in range(self._config.max_pages):
            url = self.build_search_url(keyword, offset=page_index * _PAGE_SIZE_HINT)
            await self._open_page(page, url)
            document = self._html_parser.parse(await self._render_page(page))
            self._verify_page(document)
            cards = self._find_cards(document)
            self._count("pages_visited")
            if not cards:
                logger.info("LinkedIn: no job cards for '{}' — pagination done", keyword)
                self._count("empty_pages")
                break
            signature = self._page_signature(cards)
            if signature in signatures:
                logger.info("LinkedIn: duplicate page signature for '{}' — stopping", keyword)
                self._count("duplicate_pages")
                break
            signatures.add(signature)
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
        params: dict[str, str] = {"keywords": keyword, "location": _LOCATION}
        if offset > 0:
            params["start"] = str(offset)
        return f"{_SEARCH_URL}?{urlencode(params)}"

    # -- navigation & rendering ---------------------------------------------

    async def _open_page(self, page: Page, url: str) -> None:
        """RateLimiter approval before every navigation; timeouts retried."""

        async def _attempt() -> None:
            await self._rate_limiter.wait(_DOMAIN)
            await self._browser_manager.navigate(page, url)

        await self._retry_policy.execute(_attempt, operation_name=f"LinkedIn {url}")

    async def _render_page(self, page: Page) -> str:
        """Infinite Scroll.md: scroll and wait until the card count stabilizes."""
        previous = -1
        for _ in range(self._config.max_scroll_attempts):
            count = await self._count_cards(page)
            if count == previous:
                break
            previous = count
            await page.evaluate("window.scrollBy(0, 1500)")
            await page.wait_for_timeout(_SCROLL_WAIT_MS)
            self._count("scroll_attempts")
        return await page.content()

    async def _count_cards(self, page: Page) -> int:
        for selector in SELECTORS["job_card"]:
            found = await page.locator(selector).count()
            if found:
                return found
        return 0

    # -- page verification (State Machine VERIFY PAGE) ------------------------

    def _verify_page(self, document: ParsedHTML) -> None:
        if any(document.exists(selector) for selector in SELECTORS["login_wall"]):
            raise AccessBlocked("login wall")
        if any(document.exists(selector) for selector in SELECTORS["captcha"]):
            raise AccessBlocked("captcha")

    def _find_cards(self, document: ParsedHTML) -> list[Tag]:
        found = chain_select_all(document.root, SELECTORS["job_card"])
        if found is None:
            return []
        cards, index = found
        if index > 0:
            self._count("selector_fallbacks")
        self._statistics.increment("linkedin.cards_seen", len(cards))
        return cards

    # -- pagination (Pagination.md) -------------------------------------------

    def _page_signature(self, cards: list[Tag]) -> str:
        first = self._extract_url(cards[0]) or ""
        last = self._extract_url(cards[-1]) or ""
        return hashlib.sha256(f"{first}|{last}|{len(cards)}".encode("utf-8")).hexdigest()

    def _has_next_page(self, document: ParsedHTML, cards_on_page: int) -> bool:
        for selector in SELECTORS["next_page"]:
            if document.exists(selector):
                return True
        return cards_on_page >= _PAGE_SIZE_HINT

    # -- card extraction (Field Mapping.md, Extraction Rules.md) --------------

    def _extract_card(self, document: ParsedHTML, card: Tag) -> Job | None:
        # URL first — it uniquely identifies the job (Extraction Rules order)
        url = self._extract_url(card)
        if url is None:
            return self._skip("missing or invalid url")
        title = self._normalizer.normalize_title(self._chain_text(document, card, "title"))
        if title is None:
            return self._skip("missing title")
        if self._is_invalid_title(title):
            return self._skip("invalid card title")
        company = self._normalizer.normalize_company(
            self._chain_text(document, card, "company")
        )
        if company is None:
            return self._skip("missing company")
        location = self._normalizer.normalize_text(
            self._chain_text(document, card, "location")
        )
        posted_time = self._normalizer.normalize_text(
            self._chain_text(document, card, "posted_time")
        )
        try:
            job = Job.create(
                title=title,
                company=company,
                location=location if location is not None else _LOCATION_DEFAULT,
                url=url,
                source="LinkedIn",
                posted_time=posted_time,
            )
        except Exception as exc:
            return self._skip(f"validation failed: {exc}")
        self._count("jobs_extracted")
        return job

    def _extract_url(self, card: Tag) -> str | None:
        found = chain_select_one(card, SELECTORS["link"])
        href: str | None = None
        if found is not None:
            element, index = found
            if index > 0:
                self._count("selector_fallbacks")
            raw = element.get("href")
            href = str(raw) if raw else None
        if href is None:
            job_id = card.get("data-job-id")
            if not job_id:
                return None
            href = f"/jobs/view/{job_id}"
        absolute = urljoin(_BASE + "/", href)
        normalized = self._normalizer.normalize_url(absolute)
        if normalized is None or not self._is_valid_job_url(normalized):
            return None
        return self._canonicalize_url(normalized)

    @staticmethod
    def _canonicalize_url(url: str) -> str:
        """Stable canonical job URL: https://www.linkedin.com/jobs/view/<id>.

        The trailing numeric posting ID on guest slug URLs is the job's real
        identifier — /jobs/view/<id> alone resolves, the slug without it
        404s. The slug is decorative and changes when the employer edits the
        title, so the ID-only form is both the working link and the stable
        dedup key. Host (de.linkedin.com vs www) and volatile query
        parameters (position, pageNum, refId, …) are normalized away. Slug
        URLs carrying no ID are kept as-is (best effort).
        """
        parts = urlsplit(url)
        netloc = "www.linkedin.com" if parts.netloc.endswith("linkedin.com") else parts.netloc
        path = parts.path.rstrip("/")
        if "/jobs/view/" in path:
            slug = path.rsplit("/", 1)[-1]
            if slug.isdigit():
                job_id = slug
            else:
                match = _SLUG_POSTING_ID_RE.search(slug)
                job_id = match.group(1) if match else None
            if job_id is not None:
                path = f"/jobs/view/{job_id}"
            return urlunsplit((parts.scheme, netloc, path, "", ""))
        query = [
            (key, value)
            for key, value in parse_qsl(parts.query, keep_blank_values=True)
            if key.lower() not in {"position", "pagenum", "origin"}
        ]
        return urlunsplit((parts.scheme, netloc, path, urlencode(query), ""))

    @staticmethod
    def _is_valid_job_url(url: str) -> bool:
        """Field Mapping: linkedin.com domain + job identifier required."""
        parts = urlsplit(url)
        if "linkedin.com" not in parts.netloc:
            return False
        return "/jobs/view/" in parts.path or "currentJobId" in parts.query

    @staticmethod
    def _is_invalid_title(title: str) -> bool:
        lowered = title.lower()
        return any(lowered == m or lowered.startswith(m + " ") for m in _INVALID_TITLE_MARKERS)

    def _chain_text(self, document: ParsedHTML, scope: Tag, chain_name: str) -> str | None:
        found = chain_select_one(scope, SELECTORS[chain_name])
        if found is None:
            return None
        element, index = found
        if index > 0:
            self._count("selector_fallbacks")
        return document.text(element)

    # -- helpers -------------------------------------------------------------

    @staticmethod
    async def _close_quietly(page: Page, context: object) -> None:
        for closer in (page.close, context.close):  # type: ignore[union-attr]
            try:
                await closer()
            except Exception:
                pass

    def _skip(self, reason: str) -> None:
        logger.debug("LinkedIn card skipped ({})", reason)
        self._count("cards_skipped")
        return None

    def _count(self, name: str) -> None:
        self._statistics.increment(f"linkedin.{name}")
