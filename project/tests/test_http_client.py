"""HTTP client tests (HTTPClient.md unit-test checklist).

Uses httpx.MockTransport — no live network. Rate limiter runs with a fake
clock; retry policy with a fake sleep, so tests are fast and deterministic.
"""

from __future__ import annotations

import random
from collections.abc import Callable

import httpx
import pytest

from config import Config
from http_client import EmptyResponseError, HTTPClient
from rate_limiter import RateLimiter
from retry import RetryPolicy
from session_manager import SessionManager
from stats import StatisticsCollector
from user_agent import UserAgentProvider


class FakeTime:
    def __init__(self) -> None:
        self.now = 0.0
        self.sleeps: list[float] = []

    def clock(self) -> float:
        return self.now

    async def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


def make_client(
    config: Config,
    statistics: StatisticsCollector,
    handler: Callable[[httpx.Request], httpx.Response],
) -> tuple[HTTPClient, RateLimiter, FakeTime]:
    fake_time = FakeTime()
    rate_limiter = RateLimiter(
        config,
        statistics,
        rng=random.Random(42),
        clock=fake_time.clock,
        sleep=fake_time.sleep,
    )
    client = HTTPClient(
        config=config,
        session_manager=SessionManager(config, statistics),
        rate_limiter=rate_limiter,
        retry_policy=RetryPolicy(config, statistics, sleep=fake_time.sleep),
        user_agent_provider=UserAgentProvider(config, rng=random.Random(42)),
        statistics=statistics,
        transport=httpx.MockTransport(handler),
    )
    return client, rate_limiter, fake_time


HTML = "<html><body>ok</body></html>"


class TestGet:
    async def test_success(self, config: Config, statistics: StatisticsCollector) -> None:
        client, _, _ = make_client(
            config, statistics, lambda request: httpx.Response(200, text=HTML)
        )
        async with client:
            response = await client.get("https://example.com/jobs")
        assert response.status_code == 200
        assert response.text == HTML

    async def test_user_agent_header_sent(
        self, config: Config, statistics: StatisticsCollector
    ) -> None:
        seen: dict[str, str] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            seen["ua"] = request.headers["User-Agent"]
            return httpx.Response(200, text=HTML)

        client, _, _ = make_client(config, statistics, handler)
        async with client:
            await client.get("https://example.com")
        assert seen["ua"].startswith("Mozilla/5.0")

    async def test_not_started_raises(
        self, config: Config, statistics: StatisticsCollector
    ) -> None:
        client, _, _ = make_client(config, statistics, lambda r: httpx.Response(200, text=HTML))
        with pytest.raises(RuntimeError, match="not started"):
            await client.get("https://example.com")


class TestRetryIntegration:
    async def test_500_retried_then_succeeds(
        self, config: Config, statistics: StatisticsCollector
    ) -> None:
        calls = {"n": 0}

        def handler(request: httpx.Request) -> httpx.Response:
            calls["n"] += 1
            if calls["n"] < 3:
                return httpx.Response(500)
            return httpx.Response(200, text=HTML)

        client, _, _ = make_client(config, statistics, handler)
        async with client:
            response = await client.get("https://example.com")
        assert response.status_code == 200
        assert calls["n"] == 3

    async def test_404_not_retried(self, config: Config, statistics: StatisticsCollector) -> None:
        calls = {"n": 0}

        def handler(request: httpx.Request) -> httpx.Response:
            calls["n"] += 1
            return httpx.Response(404)

        client, _, _ = make_client(config, statistics, handler)
        async with client:
            with pytest.raises(httpx.HTTPStatusError):
                await client.get("https://example.com")
        assert calls["n"] == 1
        assert statistics.snapshot()["counters"]["http.requests_failed"] == 1

    async def test_empty_html_retried(
        self, config: Config, statistics: StatisticsCollector
    ) -> None:
        calls = {"n": 0}

        def handler(request: httpx.Request) -> httpx.Response:
            calls["n"] += 1
            body = "" if calls["n"] == 1 else HTML
            return httpx.Response(200, text=body, headers={"content-type": "text/html"})

        client, _, _ = make_client(config, statistics, handler)
        async with client:
            response = await client.get("https://example.com")
        assert calls["n"] == 2
        assert response.text == HTML

    async def test_empty_html_exhausts_retries(
        self, config: Config, statistics: StatisticsCollector
    ) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, text="  ", headers={"content-type": "text/html"})

        client, _, _ = make_client(config, statistics, handler)
        async with client:
            with pytest.raises(EmptyResponseError):
                await client.get("https://example.com")


class TestRateLimitIntegration:
    async def test_429_penalizes_domain(
        self, config: Config, statistics: StatisticsCollector
    ) -> None:
        calls = {"n": 0}

        def handler(request: httpx.Request) -> httpx.Response:
            calls["n"] += 1
            if calls["n"] == 1:
                return httpx.Response(429)
            return httpx.Response(200, text=HTML)

        client, rate_limiter, _ = make_client(config, statistics, handler)
        async with client:
            response = await client.get("https://example.com")
        assert response.status_code == 200
        # penalty applied on 429, halved by reward after success
        assert rate_limiter._penalties.get("example.com") == 2.5
        assert statistics.snapshot()["counters"]["http.429_responses"] == 1

    async def test_consecutive_requests_rate_limited(
        self, config: Config, statistics: StatisticsCollector
    ) -> None:
        client, _, fake_time = make_client(
            config, statistics, lambda request: httpx.Response(200, text=HTML)
        )
        async with client:
            await client.get("https://example.com/a")
            await client.get("https://example.com/b")
        waits = [s for s in fake_time.sleeps if 0 < s <= config.rate_max_delay]
        assert waits  # second request waited


class TestStatistics:
    async def test_counters(self, config: Config, statistics: StatisticsCollector) -> None:
        client, _, _ = make_client(
            config, statistics, lambda request: httpx.Response(200, text=HTML)
        )
        async with client:
            await client.get("https://example.com")
        counters = statistics.snapshot()["counters"]
        assert counters["http.requests_sent"] == 1
        assert counters["http.requests_succeeded"] == 1
