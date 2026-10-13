"""HTTP Client (HTTPClient.md).

Unified interface for all HTTP communication. Composes SessionManager,
RateLimiter, RetryPolicy and UserAgentProvider; scrapers call get()/post()
and never touch httpx directly. Rate limiting applies before every attempt,
retries are delegated to RetryPolicy, HTTP 429 penalizes the domain.
"""

from __future__ import annotations

import time
from urllib.parse import urlsplit

import httpx

from config import Config
from logger import logger
from rate_limiter import RateLimiter
from retry import RetryableError, RetryPolicy
from session_manager import SessionManager
from stats import StatisticsCollector
from user_agent import UserAgentProvider

_ACCEPTED_STATUS: frozenset[int] = frozenset({200, 201, 204})


class EmptyResponseError(RetryableError):
    """A successful status with an empty HTML body — treated as transient."""


class HTTPClient:
    """One HTTP client per scraper execution. Use as async context manager."""

    def __init__(
        self,
        config: Config,
        session_manager: SessionManager,
        rate_limiter: RateLimiter,
        retry_policy: RetryPolicy,
        user_agent_provider: UserAgentProvider,
        statistics: StatisticsCollector | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._config = config
        self._session_manager = session_manager
        self._rate_limiter = rate_limiter
        self._retry_policy = retry_policy
        self._user_agent_provider = user_agent_provider
        self._statistics = statistics
        self._transport = transport
        self._session: httpx.AsyncClient | None = None

    async def __aenter__(self) -> "HTTPClient":
        await self.start()
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        await self.close()

    async def start(self) -> None:
        user_agent = self._user_agent_provider.new_session()
        self._session = self._session_manager.create_session(user_agent, self._transport)

    async def close(self) -> None:
        if self._session is not None:
            await self._session_manager.close_session(self._session)
            self._session = None

    async def get(self, url: str, headers: dict[str, str] | None = None) -> httpx.Response:
        return await self._request("GET", url, headers=headers)

    async def post(
        self,
        url: str,
        headers: dict[str, str] | None = None,
        data: dict[str, str] | None = None,
    ) -> httpx.Response:
        return await self._request("POST", url, headers=headers, data=data)

    async def head(self, url: str, headers: dict[str, str] | None = None) -> httpx.Response:
        return await self._request("HEAD", url, headers=headers)

    async def _request(
        self,
        method: str,
        url: str,
        headers: dict[str, str] | None = None,
        data: dict[str, str] | None = None,
    ) -> httpx.Response:
        if self._session is None:
            raise RuntimeError("HTTPClient not started — use 'async with' or start()")
        domain = urlsplit(url).netloc

        async def _attempt() -> httpx.Response:
            await self._rate_limiter.wait(domain)
            start = time.perf_counter()
            response = await self._session.request(method, url, headers=headers, data=data)
            duration_ms = (time.perf_counter() - start) * 1000
            self._record(method, url, response.status_code, duration_ms)
            if response.status_code == 429:
                self._rate_limiter.penalize(domain)
            response.raise_for_status()
            self._validate(response)
            self._rate_limiter.reward(domain)
            return response

        try:
            result = await self._retry_policy.execute(
                _attempt, operation_name=f"{method} {url}"
            )
        except Exception:
            if self._statistics is not None:
                self._statistics.increment("http.requests_failed")
            raise
        if self._statistics is not None:
            self._statistics.increment("http.requests_succeeded")
        return result

    def _validate(self, response: httpx.Response) -> None:
        """Reject empty HTML bodies (HTTPClient.md response validation)."""
        if response.status_code not in _ACCEPTED_STATUS:
            return
        content_type = response.headers.get("content-type", "")
        if response.request.method != "HEAD" and "text/html" in content_type:
            if not response.text.strip():
                raise EmptyResponseError(f"empty HTML body from {response.request.url}")

    def _record(self, method: str, url: str, status: int, duration_ms: float) -> None:
        logger.debug("{} {} → {} ({:.0f} ms)", method, url, status, duration_ms)
        if self._statistics is not None:
            self._statistics.increment("http.requests_sent")
            self._statistics.record_time("http.response_time", duration_ms)
            if status == 429:
                self._statistics.increment("http.429_responses")
