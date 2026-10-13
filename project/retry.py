"""Retry Policy (RetryPolicy.md).

Centralized retry strategy built on tenacity. Every retryable component uses
this policy; custom retry loops are forbidden. Default: 3 retries after the
initial attempt, exponential backoff (1s, 2s, 4s), optional jitter, 30s cap.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

import httpx
from playwright.async_api import TimeoutError as PlaywrightTimeoutError
from sqlalchemy.exc import OperationalError
from tenacity import (
    AsyncRetrying,
    RetryCallState,
    retry_if_exception,
    stop_after_attempt,
    wait_exponential,
    wait_exponential_jitter,
)

from config import Config
from logger import logger
from stats import StatisticsCollector


class RetryableError(Exception):
    """Marker base class: operations raising this are always retried."""


class NonRetryableError(Exception):
    """Marker base class: operations raising this fail immediately."""


RETRYABLE_HTTP_STATUS: frozenset[int] = frozenset({429, 500, 502, 503, 504})


def is_retryable(exc: BaseException) -> bool:
    """Classify an exception (RetryPolicy.md retryable/non-retryable lists)."""
    if isinstance(exc, NonRetryableError):
        return False
    if isinstance(exc, RetryableError):
        return True
    if isinstance(exc, httpx.HTTPStatusError):
        return exc.response.status_code in RETRYABLE_HTTP_STATUS
    if isinstance(exc, (httpx.TimeoutException, httpx.TransportError)):
        return True
    if isinstance(exc, PlaywrightTimeoutError):
        return True
    if isinstance(exc, OperationalError):
        # SQLite busy/locked is transient (Failure Cases); schema errors are not.
        message = str(exc).lower()
        return "locked" in message or "busy" in message
    return False


class RetryPolicy:
    """Reusable retry executor. Inject into HTTPClient, BrowserManager, Telegram."""

    def __init__(
        self,
        config: Config,
        statistics: StatisticsCollector | None = None,
        sleep: Callable[[float], Awaitable[None]] | None = None,
    ) -> None:
        self._max_attempts = config.max_retries + 1
        self._base_delay = config.retry_base_delay
        self._backoff = config.retry_backoff
        self._max_delay = config.retry_max_delay
        self._jitter = config.retry_jitter
        self._statistics = statistics
        self._sleep = sleep

    async def execute(
        self,
        operation: Callable[[], Awaitable[Any]],
        *,
        operation_name: str = "operation",
    ) -> Any:
        """Run a zero-argument async operation under the retry policy.

        Re-raises the final exception unchanged after retries are exhausted —
        the policy never suppresses failures.
        """
        kwargs: dict[str, Any] = {}
        if self._sleep is not None:
            kwargs["sleep"] = self._sleep
        retryer = AsyncRetrying(
            stop=stop_after_attempt(self._max_attempts),
            wait=self._build_wait(),
            retry=retry_if_exception(is_retryable),
            before_sleep=self._make_logger(operation_name),
            reraise=True,
            **kwargs,
        )
        return await retryer(operation)

    def _build_wait(self) -> Any:
        if self._jitter:
            return wait_exponential_jitter(
                initial=self._base_delay,
                max=self._max_delay,
                exp_base=self._backoff,
                jitter=self._base_delay,
            )
        return wait_exponential(
            multiplier=self._base_delay, exp_base=self._backoff, max=self._max_delay
        )

    def _make_logger(self, operation_name: str) -> Callable[[RetryCallState], None]:
        def _log(state: RetryCallState) -> None:
            exc = state.outcome.exception() if state.outcome else None
            delay = state.next_action.sleep if state.next_action else 0.0
            logger.warning(
                "Retry {}/{} for {} in {:.1f}s after {}: {}",
                state.attempt_number,
                self._max_attempts,
                operation_name,
                delay,
                type(exc).__name__ if exc else "unknown",
                exc,
            )
            if self._statistics is not None:
                self._statistics.increment("retry.attempts")

        return _log
