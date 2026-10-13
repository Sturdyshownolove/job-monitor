"""Statistics Collector (Statistics Collector.md).

Centralized in-memory metrics: counters, gauges and timers. Thread-safe,
lightweight, independent from logging. Every module reports here; no module
calculates global statistics locally.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from typing import Any


class _TimerStats:
    __slots__ = ("count", "total_ms", "min_ms", "max_ms")

    def __init__(self) -> None:
        self.count = 0
        self.total_ms = 0.0
        self.min_ms = float("inf")
        self.max_ms = 0.0

    def record(self, duration_ms: float) -> None:
        self.count += 1
        self.total_ms += duration_ms
        self.min_ms = min(self.min_ms, duration_ms)
        self.max_ms = max(self.max_ms, duration_ms)

    def as_dict(self) -> dict[str, float]:
        return {
            "count": self.count,
            "total_ms": round(self.total_ms, 3),
            "avg_ms": round(self.total_ms / self.count, 3) if self.count else 0.0,
            "min_ms": round(self.min_ms, 3) if self.count else 0.0,
            "max_ms": round(self.max_ms, 3),
        }


class StatisticsCollector:
    """In-memory metrics registry. Overhead target <0.5 ms per operation."""

    def __init__(self, clock: Callable[[], float] = time.perf_counter) -> None:
        self._clock = clock
        self._lock = threading.Lock()
        self._counters: dict[str, float] = {}
        self._gauges: dict[str, float] = {}
        self._timers: dict[str, _TimerStats] = {}
        self._started_at = time.time()

    def increment(self, name: str, value: float = 1) -> None:
        with self._lock:
            self._counters[name] = self._counters.get(name, 0) + value

    def decrement(self, name: str, value: float = 1) -> None:
        self.increment(name, -value)

    def set_value(self, name: str, value: float) -> None:
        with self._lock:
            self._gauges[name] = value

    def record_time(self, name: str, duration_ms: float) -> None:
        with self._lock:
            self._timers.setdefault(name, _TimerStats()).record(duration_ms)

    @contextmanager
    def measure(self, name: str) -> Iterator[None]:
        """Measure a block's duration in milliseconds."""
        start = self._clock()
        try:
            yield
        finally:
            self.record_time(name, (self._clock() - start) * 1000)

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return {
                "uptime_seconds": round(time.time() - self._started_at, 1),
                "counters": dict(self._counters),
                "gauges": dict(self._gauges),
                "timers": {name: t.as_dict() for name, t in self._timers.items()},
            }

    def summary(self) -> str:
        snap = self.snapshot()
        lines = [f"uptime: {snap['uptime_seconds']}s"]
        lines += [f"{name}: {value}" for name, value in sorted(snap["counters"].items())]
        lines += [f"{name}: {value}" for name, value in sorted(snap["gauges"].items())]
        lines += [
            f"{name}: avg {timer['avg_ms']}ms over {timer['count']} samples"
            for name, timer in sorted(snap["timers"].items())
        ]
        return "\n".join(lines)

    def reset(self) -> None:
        with self._lock:
            self._counters.clear()
            self._gauges.clear()
            self._timers.clear()
