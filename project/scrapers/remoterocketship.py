"""RemoteRocketship scraper.

Playwright-only: the job list is client-rendered (plain HTTP returns the
shell without cards). Anonymous access, no login wall observed. Cards carry
structured pills — including a language-requirement pill ("German Required")
that feeds the language filter directly, and a region pill used to keep only
Germany-eligible remote roles (Germany / Europe / EMEA / worldwide; the
pipeline's country stage enforces this via Normalizer.is_germany_eligible).

Output: list[Job] with source="RemoteRocketShip".
"""

from __future__ import annotations

import re
from urllib.parse import urlencode, urljoin

from bs4 import Tag
from playwright.async_api import Error as PlaywrightError
from playwright.async_api import Page
from playwright.async_api import TimeoutError as PlaywrightTimeoutError

from base_scraper import BaseScraper
from browser import BrowserManager
from config import Config
from html_parser import HTMLParser, ParsedHTML
from logger import logger
from models import Job
from normalizer import Normalizer
from rate_limiter import RateLimiter
from retry import RetryPolicy
from stats import StatisticsCollector

_BASE = "https://www.remoterocketship.com"
_DOMAIN = "www.remoterocketship.com"
_LOCATION_PARAM = "Germany"
_RENDER_WAIT_MS = 3_000

# Job links are the card anchor: /publicjobs/company/<company>/jobs/<slug>/
_JOB_LINK_SELECTOR = "h3 a[href*='/publicjobs/']"
_COMPANY_SELECTOR = "h4 a[href^='/company/']"
_POSTED_SELECTOR = "p.notranslate"
# Pills are small <p> tags; the interesting ones are recognized by content.
_PILL_SELECTOR = "p"
# Region pills look like "🇩🇪 Germany – Remote", "🇪🇺 Europe – Remote",
# "🌏 Anywhere in the World". Anchored so prose mentioning "remote" in other
# card paragraphs cannot masquerade as the location.
_LOCATION_PILL_RE = re.compile(
    r"(?:–|-)\s*remote\s*$|anywhere\s+in\s+the\s+world", re.IGNORECASE
)
_LANGUAGE_PILL_RE = re.compile(r"\b(\w+)\s+required\b", re.IGNORECASE)
# Strip emoji/symbols the pills are decorated with.
_PILL_CLEAN_RE = re.compile(r"[^\w\s()/&,.\-–]+")


class RemoteRocketshipScraper(BaseScraper):
    """Discovers Product Design remote jobs on RemoteRocketship."""

    name = "RemoteRocketShip"

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

    # -- main flow -----------------------------------------------------------

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
                except PlaywrightTimeoutError:
                    logger.warning(
                        "RemoteRocketship navigation kept timing out for '{}' — next keyword",
                        keyword,
                    )
                    self._count("navigation_failures")
                except PlaywrightError as exc:
                    logger.warning(
                        "RemoteRocketship browser error ({}) — restarting browser", exc
                    )
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
        previous_first_url: str | None = None
        for page_number in range(1, self._config.max_pages + 1):
            url = self.build_search_url(keyword, page=page_number)
            await self._open_page(page, url)
            await self._wait_for_cards(page)
            document = self._html_parser.parse(await page.content())
            cards = self._find_cards(document)
            self._count("pages_visited")
            if not cards:
                self._count("empty_pages")
                break
            first_url = self._extract_url(cards[0])
            if first_url is not None and first_url == previous_first_url:
                # Out-of-range pages render page 1 again — pagination done.
                self._count("duplicate_pages")
                break
            previous_first_url = first_url
            new_on_page = 0
            for card in cards:
                job = self._extract_card(document, card)
                if job is None:
                    continue
                if job.url in seen_urls:
                    self._count("duplicate_cards")
                    continue
                seen_urls.add(job.url)
                jobs.append(job)
                new_on_page += 1
            if new_on_page == 0:
                break

    @staticmethod
    def build_search_url(keyword: str, page: int = 1) -> str:
        params = {
            "page": str(page),
            "sort": "DateAdded",
            "jobTitle": keyword,
            "locations": _LOCATION_PARAM,
        }
        return f"{_BASE}/?{urlencode(params)}"

    async def _wait_for_cards(self, page: Page) -> None:
        """Next.js hydration: cards appear after client render. A page with no
        results never satisfies the selector — fall back to a fixed wait so
        the empty-page detection still runs on settled DOM."""
        try:
            await page.wait_for_selector(_JOB_LINK_SELECTOR, timeout=15_000)
        except Exception:
            await page.wait_for_timeout(_RENDER_WAIT_MS)

    async def _open_page(self, page: Page, url: str) -> None:
        async def _attempt() -> None:
            await self._rate_limiter.wait(_DOMAIN)
            await self._browser_manager.navigate(page, url)

        await self._retry_policy.execute(_attempt, operation_name=f"RemoteRocketship {url}")

    # -- extraction -----------------------------------------------------------

    def _find_cards(self, document: ParsedHTML) -> list[Tag]:
        """A card is the pill-carrying container around each job-title anchor.

        A container qualifies only if it holds exactly one job anchor. A
        looser ancestor (mid-hydration DOM without the role="button" wrapper)
        can span several cards — attributing a neighbor's "German Required"
        pill to the wrong job and suppressing a perfectly fine listing.
        """
        cards: list[Tag] = []
        for anchor in document.root.select(_JOB_LINK_SELECTOR):
            card = anchor.find_parent(attrs={"role": "button"})
            if card is None:
                # Fallback: nearest ancestor that also contains the company link
                card = next(
                    (
                        parent
                        for parent in anchor.parents
                        if isinstance(parent, Tag) and parent.select_one(_COMPANY_SELECTOR)
                    ),
                    None,
                )
            if card is None:
                continue
            if len(card.select(_JOB_LINK_SELECTOR)) != 1:
                self._count("ambiguous_cards_skipped")
                continue
            cards.append(card)
        self._statistics.increment("remoterocketship.cards_seen", len(cards))
        return cards

    def _extract_card(self, document: ParsedHTML, card: Tag) -> Job | None:
        url = self._extract_url(card)
        if url is None:
            return self._skip("missing url")
        anchor = card.select_one(_JOB_LINK_SELECTOR)
        title = self._normalizer.normalize_title(document.text(anchor) if anchor else None)
        if title is None:
            return self._skip("missing title")
        company_el = card.select_one(_COMPANY_SELECTOR)
        company = self._normalizer.normalize_company(
            document.text(company_el) if company_el else None
        )
        if company is None:
            return self._skip("missing company")
        location = self._extract_location(document, card)
        if location is None:
            return self._skip("missing location")
        posted_el = card.select_one(_POSTED_SELECTOR)
        posted_time = self._normalizer.normalize_text(
            self._clean_pill(document.text(posted_el)) if posted_el else None
        )
        language_note = self._extract_language_note(document, card)
        try:
            job = Job.create(
                title=title,
                company=company,
                location=location,
                url=url,
                source="RemoteRocketShip",
                posted_time=posted_time,
                language_note=language_note,
            )
        except Exception as exc:
            return self._skip(f"validation failed: {exc}")
        self._count("jobs_extracted")
        return job

    def _extract_url(self, card: Tag) -> str | None:
        anchor = card.select_one(_JOB_LINK_SELECTOR)
        if anchor is None:
            return None
        href = anchor.get("href")
        if not href:
            return None
        absolute = urljoin(_BASE + "/", str(href))
        return self._normalizer.normalize_url(absolute)

    def _extract_location(self, document: ParsedHTML, card: Tag) -> str | None:
        for pill in card.select(_PILL_SELECTOR):
            text = document.text(pill)
            if text and _LOCATION_PILL_RE.search(text) and "ago" not in text.lower():
                cleaned = self._clean_pill(text)
                if cleaned:
                    return self._normalizer.normalize_text(cleaned)
        return None

    def _extract_language_note(self, document: ParsedHTML, card: Tag) -> str | None:
        """Card pill like "🗣️🇩🇪 German Required" → "German Required".

        The speech emoji marks language pills; the textual regex alone would
        also hit pills like "Experience Required".
        """
        for pill in card.select(_PILL_SELECTOR):
            text = document.text(pill)
            if text and "🗣" in text and _LANGUAGE_PILL_RE.search(text):
                cleaned = self._clean_pill(text)
                if cleaned:
                    self._count("language_pills")
                    return cleaned
        return None

    @staticmethod
    def _clean_pill(text: str | None) -> str | None:
        if text is None:
            return None
        cleaned = _PILL_CLEAN_RE.sub("", text).strip()
        return cleaned or None

    # -- helpers -------------------------------------------------------------

    @staticmethod
    async def _close_quietly(page: Page, context: object) -> None:
        for closer in (page.close, context.close):  # type: ignore[union-attr]
            try:
                await closer()
            except Exception:
                pass

    def _skip(self, reason: str) -> None:
        logger.debug("RemoteRocketship card skipped ({})", reason)
        self._count("cards_skipped")
        return None

    def _count(self, name: str) -> None:
        self._statistics.increment(f"remoterocketship.{name}")
