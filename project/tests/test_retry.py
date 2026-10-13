"""Retry policy tests (RetryPolicy.md unit-test checklist)."""

from __future__ import annotations

import httpx
import pytest

from config import Config
from retry import NonRetryableError, RetryableError, RetryPolicy, is_retryable
from stats import StatisticsCollector


def http_status_error(status: int) -> httpx.HTTPStatusError:
    request = httpx.Request("GET", "https://example.com")
    response = httpx.Response(status, request=request)
    return httpx.HTTPStatusError("err", request=request, response=response)


class TestClassification:
    @pytest.mark.parametrize("status", [429, 500, 502, 503, 504])
    def test_retryable_statuses(self, status: int) -> None:
        assert is_retryable(http_status_error(status))

    @pytest.mark.parametrize("status", [400, 401, 403, 404])
    def test_non_retryable_statuses(self, status: int) -> None:
        assert not is_retryable(http_status_error(status))

    def test_timeout_retryable(self) -> None:
        assert is_retryable(httpx.ConnectTimeout("t"))

    def test_transport_error_retryable(self) -> None:
        assert is_retryable(httpx.ConnectError("t"))

    def test_marker_classes(self) -> None:
        assert is_retryable(RetryableError("x"))
        assert not is_retryable(NonRetryableError("x"))

    def test_programming_error_not_retryable(self) -> None:
        assert not is_retryable(ValueError("bug"))

    def test_sqlite_locked_retryable(self) -> None:
        from sqlalchemy.exc import OperationalError

        locked = OperationalError("INSERT", {}, Exception("database is locked"))
        assert is_retryable(locked)

    def test_sqlite_schema_error_not_retryable(self) -> None:
        from sqlalchemy.exc import OperationalError

        broken = OperationalError("INSERT", {}, Exception("no such table: jobs"))
        assert not is_retryable(broken)


@pytest.fixture
def sleeps() -> list[float]:
    return []


@pytest.fixture
def policy(config: Config, statistics: StatisticsCollector, sleeps: list[float]) -> RetryPolicy:
    async def fake_sleep(seconds: float) -> None:
        sleeps.append(seconds)

    return RetryPolicy(config, statistics, sleep=fake_sleep)


class TestExecute:
    async def test_success_first_attempt(self, policy: RetryPolicy, sleeps: list[float]) -> None:
        async def op() -> str:
            return "ok"

        assert await policy.execute(op) == "ok"
        assert sleeps == []

    async def test_recovers_after_transient_failures(
        self, policy: RetryPolicy, sleeps: list[float]
    ) -> None:
        calls = {"n": 0}

        async def op() -> str:
            calls["n"] += 1
            if calls["n"] < 3:
                raise RetryableError("transient")
            return "ok"

        assert await policy.execute(op) == "ok"
        assert calls["n"] == 3
        # Exponential backoff without jitter: 1s then 2s
        assert sleeps == [1.0, 2.0]

    async def test_exhausted_raises_original(self, policy: RetryPolicy, sleeps: list[float]) -> None:
        async def op() -> None:
            raise RetryableError("always")

        with pytest.raises(RetryableError):
            await policy.execute(op)
        # initial + 3 retries = 4 attempts, 3 waits: 1, 2, 4
        assert sleeps == [1.0, 2.0, 4.0]

    async def test_non_retryable_fails_immediately(
        self, policy: RetryPolicy, sleeps: list[float]
    ) -> None:
        calls = {"n": 0}

        async def op() -> None:
            calls["n"] += 1
            raise ValueError("bug")

        with pytest.raises(ValueError):
            await policy.execute(op)
        assert calls["n"] == 1
        assert sleeps == []

    async def test_statistics_recorded(
        self, policy: RetryPolicy, statistics: StatisticsCollector
    ) -> None:
        calls = {"n": 0}

        async def op() -> str:
            calls["n"] += 1
            if calls["n"] == 1:
                raise RetryableError("once")
            return "ok"

        await policy.execute(op)
        assert statistics.snapshot()["counters"]["retry.attempts"] == 1

    async def test_max_delay_cap(self, statistics: StatisticsCollector) -> None:
        config = Config(
            telegram_token="t",
            chat_id="c",
            max_retries=6,
            retry_jitter=False,
            retry_max_delay=5,
        )
        sleeps: list[float] = []

        async def fake_sleep(seconds: float) -> None:
            sleeps.append(seconds)

        policy = RetryPolicy(config, statistics, sleep=fake_sleep)

        async def op() -> None:
            raise RetryableError("always")

        with pytest.raises(RetryableError):
            await policy.execute(op)
        assert max(sleeps) <= 5
