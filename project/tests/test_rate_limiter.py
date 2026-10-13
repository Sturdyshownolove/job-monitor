"""Rate limiter tests (RateLimiter.md unit-test checklist)."""

from __future__ import annotations

import random

import pytest

from config import Config
from rate_limiter import RateLimiter
from stats import StatisticsCollector


class FakeTime:
    """Deterministic clock + sleep: sleeping advances the clock."""

    def __init__(self) -> None:
        self.now = 0.0
        self.sleeps: list[float] = []

    def clock(self) -> float:
        return self.now

    async def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds

    def advance(self, seconds: float) -> None:
        self.now += seconds


@pytest.fixture
def fake_time() -> FakeTime:
    return FakeTime()


def make_limiter(
    config: Config,
    fake_time: FakeTime,
    statistics: StatisticsCollector | None = None,
    **kwargs: object,
) -> RateLimiter:
    return RateLimiter(
        config,
        statistics,
        rng=random.Random(42),
        clock=fake_time.clock,
        sleep=fake_time.sleep,
        **kwargs,  # type: ignore[arg-type]
    )


class TestWait:
    async def test_first_request_immediate(self, config: Config, fake_time: FakeTime) -> None:
        limiter = make_limiter(config, fake_time)
        await limiter.wait("example.com")
        assert fake_time.sleeps == []

    async def test_consecutive_requests_delayed_within_range(
        self, config: Config, fake_time: FakeTime
    ) -> None:
        limiter = make_limiter(config, fake_time)
        await limiter.wait("example.com")
        await limiter.wait("example.com")
        assert len(fake_time.sleeps) == 1
        assert config.rate_min_delay >= 2  # documented default
        assert 0 < fake_time.sleeps[0] <= config.rate_max_delay

    async def test_elapsed_time_reduces_wait(self, config: Config, fake_time: FakeTime) -> None:
        limiter = make_limiter(config, fake_time)
        await limiter.wait("example.com")
        fake_time.advance(10)  # beyond max delay — no wait needed
        await limiter.wait("example.com")
        assert fake_time.sleeps == []

    async def test_domains_independent(self, config: Config, fake_time: FakeTime) -> None:
        limiter = make_limiter(config, fake_time)
        await limiter.wait("a.com")
        await limiter.wait("b.com")  # different domain, no delay
        assert fake_time.sleeps == []

    async def test_randomized_delays_vary(self, config: Config, fake_time: FakeTime) -> None:
        limiter = make_limiter(config, fake_time)
        for _ in range(4):
            await limiter.wait("example.com")
        assert len(set(fake_time.sleeps)) > 1  # never a fixed delay


class TestCooldown:
    async def test_cooldown_after_batch(self, config: Config, fake_time: FakeTime) -> None:
        stats = StatisticsCollector()
        limiter = make_limiter(config, fake_time, stats, cooldown_after=3, cooldown_seconds=15.0)
        for _ in range(3):
            fake_time.advance(10)
            await limiter.wait("example.com")
        assert 15.0 in fake_time.sleeps
        assert stats.snapshot()["counters"]["rate_limiter.cooldowns"] == 1


class TestPenalty:
    async def test_penalize_increases_delay(self, config: Config, fake_time: FakeTime) -> None:
        limiter = make_limiter(config, fake_time)
        await limiter.wait("example.com")
        limiter.penalize("example.com")
        await limiter.wait("example.com")
        assert fake_time.sleeps[0] > config.rate_max_delay  # base + 5s penalty

    async def test_penalty_doubles_and_caps(self, config: Config, fake_time: FakeTime) -> None:
        limiter = make_limiter(config, fake_time)
        for _ in range(10):
            limiter.penalize("example.com")
        assert limiter._penalties["example.com"] == 60.0

    async def test_reward_decays_penalty(self, config: Config, fake_time: FakeTime) -> None:
        limiter = make_limiter(config, fake_time)
        limiter.penalize("example.com")  # 5.0
        limiter.reward("example.com")  # 2.5
        assert limiter._penalties["example.com"] == 2.5
        for _ in range(5):
            limiter.reward("example.com")
        assert "example.com" not in limiter._penalties

    async def test_statistics(self, config: Config, fake_time: FakeTime) -> None:
        stats = StatisticsCollector()
        limiter = make_limiter(config, fake_time, stats)
        limiter.penalize("example.com")
        await limiter.wait("example.com")
        await limiter.wait("example.com")
        counters = stats.snapshot()["counters"]
        assert counters["rate_limiter.penalties"] == 1
        assert counters["rate_limiter.requests_delayed"] >= 1
