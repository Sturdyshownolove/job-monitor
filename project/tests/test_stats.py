"""Statistics collector tests (Statistics Collector.md unit-test checklist)."""

from __future__ import annotations

import threading

from stats import StatisticsCollector


class TestCounters:
    def test_increment(self) -> None:
        stats = StatisticsCollector()
        stats.increment("jobs.found")
        stats.increment("jobs.found", 4)
        assert stats.snapshot()["counters"]["jobs.found"] == 5

    def test_decrement(self) -> None:
        stats = StatisticsCollector()
        stats.increment("active", 3)
        stats.decrement("active")
        assert stats.snapshot()["counters"]["active"] == 2


class TestGauges:
    def test_set_value(self) -> None:
        stats = StatisticsCollector()
        stats.set_value("memory_mb", 120.5)
        stats.set_value("memory_mb", 130.0)
        assert stats.snapshot()["gauges"]["memory_mb"] == 130.0


class TestTimers:
    def test_record_time(self) -> None:
        stats = StatisticsCollector()
        stats.record_time("scrape", 100)
        stats.record_time("scrape", 300)
        timer = stats.snapshot()["timers"]["scrape"]
        assert timer["count"] == 2
        assert timer["avg_ms"] == 200
        assert timer["min_ms"] == 100
        assert timer["max_ms"] == 300

    def test_measure_context_manager(self) -> None:
        ticks = iter([0.0, 0.25])
        stats = StatisticsCollector(clock=lambda: next(ticks))
        with stats.measure("op"):
            pass
        timer = stats.snapshot()["timers"]["op"]
        assert timer["count"] == 1
        assert timer["avg_ms"] == 250

    def test_measure_records_on_exception(self) -> None:
        ticks = iter([0.0, 0.1])
        stats = StatisticsCollector(clock=lambda: next(ticks))
        try:
            with stats.measure("op"):
                raise RuntimeError("boom")
        except RuntimeError:
            pass
        assert stats.snapshot()["timers"]["op"]["count"] == 1


class TestSnapshotAndSummary:
    def test_snapshot_isolated_copy(self) -> None:
        stats = StatisticsCollector()
        stats.increment("a")
        snap = stats.snapshot()
        snap["counters"]["a"] = 999
        assert stats.snapshot()["counters"]["a"] == 1

    def test_summary_contains_metrics(self) -> None:
        stats = StatisticsCollector()
        stats.increment("jobs.found", 3)
        stats.record_time("scrape", 50)
        summary = stats.summary()
        assert "jobs.found: 3" in summary
        assert "scrape" in summary

    def test_reset(self) -> None:
        stats = StatisticsCollector()
        stats.increment("a")
        stats.set_value("b", 1)
        stats.record_time("c", 1)
        stats.reset()
        snap = stats.snapshot()
        assert snap["counters"] == {}
        assert snap["gauges"] == {}
        assert snap["timers"] == {}


class TestThreadSafety:
    def test_concurrent_increments(self) -> None:
        stats = StatisticsCollector()

        def work() -> None:
            for _ in range(1000):
                stats.increment("hits")

        threads = [threading.Thread(target=work) for _ in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        assert stats.snapshot()["counters"]["hits"] == 8000
