"""Session Manager (SessionManager.md).

Owns the lifecycle of every httpx.AsyncClient. One client per scraper
execution, reused for all requests, always closed on cleanup. Scrapers never
create or destroy clients directly.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import httpx

from config import Config
from logger import logger
from stats import StatisticsCollector

_MAX_REDIRECTS = 5

_BASE_HEADERS = {
    "Accept": (
        "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,"
        "image/webp,*/*;q=0.8"
    ),
    "Accept-Language": "en-US,en;q=0.9,de;q=0.8",
    "Upgrade-Insecure-Requests": "1",
}


class SessionManager:
    """Creates, tracks and closes httpx.AsyncClient sessions."""

    def __init__(self, config: Config, statistics: StatisticsCollector | None = None) -> None:
        self._config = config
        self._statistics = statistics

    def create_session(
        self, user_agent: str, transport: httpx.AsyncBaseTransport | None = None
    ) -> httpx.AsyncClient:
        """New client: persistent connections, cookies, redirects, SSL verify on.

        `transport` exists for tests (httpx.MockTransport); production always
        uses the default transport.
        """
        client = httpx.AsyncClient(
            headers={**_BASE_HEADERS, "User-Agent": user_agent},
            timeout=httpx.Timeout(self._config.request_timeout),
            follow_redirects=True,
            max_redirects=_MAX_REDIRECTS,
            transport=transport,
        )
        logger.debug("HTTP session created")
        if self._statistics is not None:
            self._statistics.increment("session.created")
        return client

    async def close_session(self, client: httpx.AsyncClient) -> None:
        await client.aclose()
        logger.debug("HTTP session closed")
        if self._statistics is not None:
            self._statistics.increment("session.closed")

    @asynccontextmanager
    async def session(
        self, user_agent: str, transport: httpx.AsyncBaseTransport | None = None
    ) -> AsyncIterator[httpx.AsyncClient]:
        """Session with guaranteed cleanup (try/finally per SessionManager.md)."""
        client = self.create_session(user_agent, transport)
        try:
            yield client
        finally:
            await self.close_session(client)
