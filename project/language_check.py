"""Language requirement filter.

Rejects jobs that demand German without accepting English; everything else
passes. "German is a plus / von Vorteil" is explicitly not a requirement.
Unknown always passes — a job is never rejected on missing evidence.

Evidence sources, in order:
1. `job.language_note` — a card-level label some sources expose
   (RemoteRocketship's "German Required" pill). No network needed.
2. The job detail page, fetched over plain HTTP. Sources behind bot walls
   (Indeed, StepStone) simply yield UNKNOWN → pass.

The verdict is computed by `LanguagePolicy` (pure, regex-based, testable);
`LanguageChecker` adds the fetch plumbing. Sits in the pipeline after the
insert stage: rejected jobs stay stored (never re-checked) but are not
notified.
"""

from __future__ import annotations

import re
from enum import StrEnum

import httpx
from bs4 import BeautifulSoup

from config import Config
from logger import logger
from models import Job
from stats import StatisticsCollector

_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)

# Explicit "German is required" statements (German- and English-language ads).
_GERMAN_REQUIRED = re.compile(
    r"""
    german\s+(?:is\s+)?(?:required|mandatory|a\s+must|essential)
    | (?:fluent|fluency|proficien\w+|business[- ]fluent|native)\s+(?:in\s+)?german
    | german\s+(?:at\s+)?(?:native|[cb][12])
    | german\s*(?:\(|:|\s)\s*[cb][12]
    | deutschkenntnisse
    | deutsch\s+(?:in\s+wort\s+und\s+schrift|auf\s+muttersprachlichem\s+niveau)
    | (?:sehr\s+gute?s?|fließende?s?|verhandlungssichere?s?|gute?s?)\s+deutsch
    | deutsch\s+(?:erforderlich|vorausgesetzt|zwingend)
    | muttersprache\s+deutsch
    """,
    re.IGNORECASE | re.VERBOSE,
)

# "...but only as a plus" — neutralizes a German-required match when it
# appears in the same sentence fragment as the German mention.
_PLUS_MARKERS = re.compile(
    r"""
    (?:is|are)?\s*a\s+plus | nice[- ]to[- ]have | advantageous | an\s+advantage
    | von\s+vorteil | wünschenswert | ein\s+plus | kein\s+muss | optional
    | not\s+required | nicht\s+erforderlich | helpful\s+but
    """,
    re.IGNORECASE | re.VERBOSE,
)

# English acceptance markers — if the ad also works in English, German being
# listed does not exclude the user.
_ENGLISH_OK = re.compile(
    r"""
    (?:fluent|fluency|proficien\w+|business|good|excellent|native)\s+(?:in\s+)?english
    | english\s+(?:is\s+)?(?:required|mandatory|a\s+must|essential|preferred)
    | english\s*(?:\(|:|\s)\s*[cb][12]
    | working\s+language\s+(?:is\s+)?english
    | english[- ]speaking\s+(?:team|environment|company)
    | company\s+language\s+(?:is\s+)?english
    | englischkenntnisse
    | (?:sehr\s+gute?s?|fließende?s?|verhandlungssichere?s?|gute?s?)\s+englisch
    """,
    re.IGNORECASE | re.VERBOSE,
)

# Window (chars) around a German-required match scanned for a plus-marker.
_PLUS_WINDOW = 120


class LanguageVerdict(StrEnum):
    OK = "ok"                    # English accepted, or German only a plus
    GERMAN_ONLY = "german_only"  # German demanded, no sign English is accepted
    UNKNOWN = "unknown"          # No evidence either way — must pass


class LanguagePolicy:
    """Pure text policy — no I/O. One method, deterministic."""

    def evaluate(self, text: str | None) -> LanguageVerdict:
        if text is None or not text.strip():
            return LanguageVerdict.UNKNOWN
        english_ok = _ENGLISH_OK.search(text) is not None
        german_required = False
        for match in _GERMAN_REQUIRED.finditer(text):
            window = text[match.start(): match.end() + _PLUS_WINDOW]
            if _PLUS_MARKERS.search(window):
                continue  # "German is a plus" — not a requirement
            german_required = True
            break
        if german_required and not english_ok:
            return LanguageVerdict.GERMAN_ONLY
        if german_required or english_ok:
            return LanguageVerdict.OK
        return LanguageVerdict.UNKNOWN


class LanguageChecker:
    """Pipeline-facing checker: card note first, detail page as fallback."""

    def __init__(
        self,
        config: Config,
        statistics: StatisticsCollector | None = None,
        policy: LanguagePolicy | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._enabled = config.language_filter
        self._timeout = config.request_timeout
        self._statistics = statistics
        self._policy = policy if policy is not None else LanguagePolicy()
        self._transport = transport
        self._client: httpx.AsyncClient | None = None

    @property
    def enabled(self) -> bool:
        return self._enabled

    async def close(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    async def check(self, job: Job) -> LanguageVerdict:
        """Verdict for one job. Never raises; fetch failures yield UNKNOWN."""
        if not self._enabled:
            return LanguageVerdict.UNKNOWN
        if job.language_note:
            verdict = self._policy.evaluate(job.language_note)
            if verdict != LanguageVerdict.UNKNOWN:
                self._count(f"language.card_{verdict.value}")
                return verdict
        text = await self._fetch_detail(job.url)
        verdict = self._policy.evaluate(text)
        self._count(f"language.detail_{verdict.value}")
        return verdict

    async def _fetch_detail(self, url: str) -> str | None:
        try:
            client = self._get_client()
            response = await client.get(url)
            if response.status_code != 200:
                logger.debug("Language check: HTTP {} for {}", response.status_code, url)
                return None
            # Visible text only — markers inside scripts/markup must not match.
            return BeautifulSoup(response.text, "lxml").get_text(separator=" ")
        except Exception as exc:
            logger.debug("Language check fetch failed for {}: {}", url, exc)
            return None

    def _get_client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(
                headers={"User-Agent": _UA, "Accept-Language": "de-DE,de;q=0.9,en;q=0.8"},
                timeout=httpx.Timeout(self._timeout),
                follow_redirects=True,
                transport=self._transport,
            )
        return self._client

    def _count(self, name: str) -> None:
        if self._statistics is not None:
            self._statistics.increment(name)
