"""Rate Limiter (RateLimiter.md).

Controls outbound request frequency per domain: randomized delays
(RATE_MIN_DELAY–RATE_MAX_DELAY), burst protection, batch cooldowns, and
penalty growth on HTTP 429. Every HTTP request and browser navigation passes
through here; scrapers never sleep on their own.
"""

from __future__ import annotations

import asyncio
import random
import time
from collections.abc import Awaitable, Callable

from config import Config
from logger import logger
from stats import StatisticsCollector

_PENALTY_INITIAL = 5.0
_PENALTY_CAP = 60.0
_PENALTY_FLOOR = 0.5


class RateLimiter:
    """Per-domain randomized delay with cooldowns and 429 penalties."""

    def __init__(
        self,
        config: Config,
        statistics: StatisticsCollector | None = None,
        *,
        cooldown_after: int = 20,
        cooldown_seconds: float = 15.0,
        rng: random.Random | None = None,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self._min_delay = config.rate_min_delay
        self._max_delay = config.rate_max_delay
        self._cooldown_after = cooldown_after
        self._cooldown_seconds = cooldown_seconds
        self._statistics = statistics
        self._rng = rng if rng is not None else random.Random()
        self._clock = clock
        self._sleep = sleep
        self._last_request: dict[str, float] = {}
        self._request_count: dict[str, int] = {}
        self._penalties: dict[str, float] = {}
        self._locks: dict[str, asyncio.Lock] = {}

    def _lock_for(self, domain: str) -> asyncio.Lock:
        if domain not in self._locks:
            self._locks[domain] = asyncio.Lock()
        return self._locks[domain]

    async def wait(self, domain: str) -> None:
        """Block until the next request to `domain` is allowed."""
        async with self._lock_for(domain):
            delay = self._rng.uniform(self._min_delay, self._max_delay)
            delay += self._penalties.get(domain, 0.0)
            last = self._last_request.get(domain)
            if last is not None:
                remaining = delay - (self._clock() - last)
                if remaining > 0:
                    logger.debug("Rate limit: waiting {:.2f}s for {}", remaining, domain)
                    await self._sleep(remaining)
                    self._record_wait(remaining)

            count = self._request_count.get(domain, 0) + 1
            self._request_count[domain] = count
            if self._cooldown_after > 0 and count % self._cooldown_after == 0:
                logger.info(
                    "Cooldown for {} after {} requests: {:.0f}s",
                    domain,
                    count,
                    self._cooldown_seconds,
                )
                await self._sleep(self._cooldown_seconds)
                if self._statistics is not None:
                    self._statistics.increment("rate_limiter.cooldowns")

            self._last_request[domain] = self._clock()

    def penalize(self, domain: str) -> None:
        """Increase delay for a domain after HTTP 429 (doubles, capped)."""
        current = self._penalties.get(domain, 0.0)
        new_penalty = min(current * 2 if current else _PENALTY_INITIAL, _PENALTY_CAP)
        self._penalties[domain] = new_penalty
        logger.warning("Rate limit penalty for {}: +{:.1f}s per request", domain, new_penalty)
        if self._statistics is not None:
            self._statistics.increment("rate_limiter.penalties")

    def reward(self, domain: str) -> None:
        """Gradually decay the penalty after successful responses."""
        current = self._penalties.get(domain, 0.0)
        if not current:
            return
        halved = current / 2
        if halved < _PENALTY_FLOOR:
            self._penalties.pop(domain, None)
        else:
            self._penalties[domain] = halved

    def _record_wait(self, seconds: float) -> None:
        if self._statistics is not None:
            self._statistics.increment("rate_limiter.requests_delayed")
            self._statistics.record_time("rate_limiter.wait", seconds * 1000)
