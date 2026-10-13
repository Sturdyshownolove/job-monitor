"""Telegram Notification Service (Telegram.md).

Only responsibility: deliver notifications about validated Job objects.
Never filters, never touches the database, never schedules.

Owns a dedicated httpx.AsyncClient (the scraper HTTPClient applies scraper
rate limiting, which must not delay notifications). Every transport/API error
is wrapped into a sanitized exception before RetryPolicy can log it — the bot
token never appears in logs or error messages.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable, Iterable

import httpx

from config import Config
from logger import logger
from models import Job
from retry import RETRYABLE_HTTP_STATUS, NonRetryableError, RetryableError, RetryPolicy
from stats import StatisticsCollector

_API_BASE = "https://api.telegram.org/bot{token}"

# Telegram MarkdownV2 characters requiring escape (Telegram.md)
_MARKDOWN_ESCAPE = "_*[]()~`>#+-=|{}.!"

DEFAULT_TEMPLATE = (
    "🚀 New Product Design Job\n"
    "\n"
    "Title\n"
    "{title}\n"
    "\n"
    "Company\n"
    "{company}\n"
    "\n"
    "Location\n"
    "{location}\n"
    "\n"
    "Source\n"
    "{source}\n"
    "\n"
    "Posted\n"
    "{posted_time}\n"
    "\n"
    "Open Job\n"
    "\n"
    "{url}"
)


class TelegramError(NonRetryableError):
    """Permanent Telegram failure (invalid token, bad request, ok=false)."""


class TelegramTransientError(RetryableError):
    """Recoverable Telegram failure (network, 429, 5xx)."""


def escape_markdown(text: str) -> str:
    """Escape every MarkdownV2 special character in user-generated text."""
    return "".join(f"\\{ch}" if ch in _MARKDOWN_ESCAPE else ch for ch in text)


class TelegramService:
    """Sends job notifications through the Telegram Bot API.

    `template` is a format string with {title} {company} {location} {source}
    {posted_time} {url} placeholders. Interpolated values are MarkdownV2-
    escaped; static template text must itself be MarkdownV2-safe.
    """

    def __init__(
        self,
        config: Config,
        retry_policy: RetryPolicy,
        statistics: StatisticsCollector | None = None,
        template: str = DEFAULT_TEMPLATE,
        transport: httpx.AsyncBaseTransport | None = None,
        send_interval: float = 1.0,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._token = config.telegram_token
        self._chat_id = config.chat_id
        self._timeout = config.request_timeout
        self._retry_policy = retry_policy
        self._statistics = statistics
        self._template = template
        self._transport = transport
        self._send_interval = send_interval
        self._sleep = sleep
        self._clock = clock
        self._last_send: float | None = None
        self._client: httpx.AsyncClient | None = None

    # -- lifecycle -----------------------------------------------------------

    async def __aenter__(self) -> "TelegramService":
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        await self.close()

    async def close(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    def _get_client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(
                timeout=httpx.Timeout(self._timeout), transport=self._transport
            )
        return self._client

    # -- public API ----------------------------------------------------------

    def build_message(self, job: Job) -> str:
        return self._template.format(
            title=escape_markdown(job.title),
            company=escape_markdown(job.company),
            location=escape_markdown(job.location),
            source=escape_markdown(job.source),
            posted_time=escape_markdown(job.posted_time or "Unknown"),
            url=escape_markdown(job.url),
        )

    async def send(self, job: Job) -> bool:
        """Deliver one notification. Returns False on final failure — never raises.

        Self-throttles: consecutive sends respect `send_interval` (Telegram.md
        rate limiting) regardless of the caller.
        """
        await self._throttle()
        message = self.build_message(job)

        async def _attempt() -> None:
            await self._call_api(
                "sendMessage",
                {"chat_id": self._chat_id, "text": message, "parse_mode": "MarkdownV2"},
            )

        try:
            await self._retry_policy.execute(_attempt, operation_name="telegram.send")
        except Exception as exc:
            self._last_send = self._clock()
            logger.error("Telegram notification failed: {}", self._sanitize(str(exc)))
            if self._statistics is not None:
                self._statistics.increment("telegram.messages_failed")
            return False
        self._last_send = self._clock()
        logger.info("Telegram notification sent: {} @ {}", job.title, job.company)
        if self._statistics is not None:
            self._statistics.increment("telegram.messages_sent")
        return True

    async def send_many(self, jobs: Iterable[Job]) -> int:
        """Send sequentially. Throttling happens inside send(). Returns success count."""
        sent = 0
        for job in jobs:
            if await self.send(job):
                sent += 1
        return sent

    async def _throttle(self) -> None:
        if self._send_interval <= 0 or self._last_send is None:
            return
        remaining = self._send_interval - (self._clock() - self._last_send)
        if remaining > 0:
            await self._sleep(remaining)

    async def health_check(self) -> bool:
        """Verify the bot token via getMe (Health Checks.md: no visible message)."""
        async def _attempt() -> None:
            await self._call_api("getMe", None)

        try:
            await self._retry_policy.execute(_attempt, operation_name="telegram.health")
            return True
        except Exception as exc:
            logger.warning("Telegram health check failed: {}", self._sanitize(str(exc)))
            return False

    # -- internals -----------------------------------------------------------

    async def _call_api(self, method: str, payload: dict[str, str] | None) -> None:
        """One API call. Raises only sanitized exceptions (no token leakage)."""
        url = f"{_API_BASE.format(token=self._token)}/{method}"
        client = self._get_client()
        try:
            if payload is None:
                response = await client.get(url)
            else:
                response = await client.post(url, json=payload)
        except httpx.RequestError as exc:
            raise TelegramTransientError(
                f"{method}: network error ({type(exc).__name__})"
            ) from None

        if response.status_code in RETRYABLE_HTTP_STATUS:
            raise TelegramTransientError(f"{method}: HTTP {response.status_code}")
        if response.status_code != 200:
            raise TelegramError(
                f"{method}: HTTP {response.status_code}: "
                f"{self._sanitize(response.text[:200])}"
            )
        body = response.json()
        if not body.get("ok"):
            raise TelegramError(
                f"{method}: API error: {self._sanitize(str(body.get('description')))}"
            )

    def _sanitize(self, text: str) -> str:
        return text.replace(self._token, "***")
