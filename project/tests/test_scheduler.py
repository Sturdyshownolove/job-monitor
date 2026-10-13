"""Scheduler tests (Architecture.md / Failure Cases scheduler requirements)."""

from __future__ import annotations

import asyncio

import pytest

from base_scraper import BaseScraper
from config import Config
from models import Job
from pipeline import PipelineOutcome, PipelineSummary
from scheduler import Scheduler
from stats import StatisticsCollector


def make_job(n: int = 1) -> Job:
    return Job.create(
        title=f"Product Designer {n}",
        company="Miro",
        location="Berlin",
        url=f"https://example.com/jobs/{n}",
        source="LinkedIn",
    )


class StubScraper(BaseScraper):
    def __init__(
        self,
        config: Config,
        statistics: StatisticsCollector,
        name: str,
        jobs: list[Job] | None = None,
        gate: asyncio.Event | None = None,
        on_scrape=None,
    ) -> None:
        super().__init__(config, statistics)
        self.name = name
        self._jobs = jobs if jobs is not None else []
        self._gate = gate
        self._on_scrape = on_scrape
        self.calls = 0

    async def scrape(self) -> list[Job]:
        self.calls += 1
        if self._on_scrape is not None:
            self._on_scrape()
        if self._gate is not None:
            await self._gate.wait()
        return self._jobs


class CrashingScraper(BaseScraper):
    name = "crashing"

    async def scrape(self) -> list[Job]:
        raise RuntimeError("site down")


class RecordingPipeline:
    def __init__(self) -> None:
        self.batches: list[list[Job]] = []

    async def process(self, jobs) -> PipelineSummary:
        batch = list(jobs)
        self.batches.append(batch)
        summary = PipelineSummary(received=len(batch))
        for _ in batch:
            summary.record(PipelineOutcome.INSERTED_AND_NOTIFIED)
        return summary


class RecordingHealth:
    def __init__(self) -> None:
        self.runtime_calls = 0

    async def run_runtime_checks(self):
        self.runtime_calls += 1


def make_scheduler(
    config: Config,
    statistics: StatisticsCollector,
    health: RecordingHealth | None = None,
    health_interval: float = 10**9,
) -> tuple[Scheduler, RecordingPipeline]:
    pipeline = RecordingPipeline()
    scheduler = Scheduler(
        config,
        pipeline,  # type: ignore[arg-type]
        statistics,
        health_checker=health,  # type: ignore[arg-type]
        health_interval_seconds=health_interval,
    )
    return scheduler, pipeline


class TestRegistration:
    def test_register_preserves_order(
        self, config: Config, statistics: StatisticsCollector
    ) -> None:
        scheduler, _ = make_scheduler(config, statistics)
        a = StubScraper(config, statistics, "a")
        b = StubScraper(config, statistics, "b")
        scheduler.register_scraper(a)
        scheduler.register_scraper(b)
        assert scheduler.scrapers == (a, b)


class TestRunCycle:
    async def test_runs_every_scraper_through_pipeline(
        self, config: Config, statistics: StatisticsCollector
    ) -> None:
        scheduler, pipeline = make_scheduler(config, statistics)
        scheduler.register_scraper(StubScraper(config, statistics, "a", [make_job(1)]))
        scheduler.register_scraper(
            StubScraper(config, statistics, "b", [make_job(2), make_job(3)])
        )
        report = await scheduler.run_cycle()
        assert report is not None
        assert report.scrapers_run == 2
        assert report.jobs_found == 3
        assert report.summary.notified == 3
        assert [len(batch) for batch in pipeline.batches] == [1, 2]

    async def test_failing_scraper_does_not_stop_cycle(
        self, config: Config, statistics: StatisticsCollector
    ) -> None:
        scheduler, pipeline = make_scheduler(config, statistics)
        scheduler.register_scraper(CrashingScraper(config, statistics))
        healthy = StubScraper(config, statistics, "healthy", [make_job(1)])
        scheduler.register_scraper(healthy)
        report = await scheduler.run_cycle()
        assert report is not None
        assert report.scrapers_run == 2  # crashing scraper isolated, returned []
        assert healthy.calls == 1
        assert report.summary.notified == 1

    async def test_statistics_recorded(
        self, config: Config, statistics: StatisticsCollector
    ) -> None:
        scheduler, _ = make_scheduler(config, statistics)
        scheduler.register_scraper(StubScraper(config, statistics, "a"))
        await scheduler.run_cycle()
        counters = statistics.snapshot()["counters"]
        assert counters["scheduler.runs"] == 1
        assert counters["scheduler.completed_runs"] == 1
        assert statistics.snapshot()["timers"]["scheduler.cycle_duration"]["count"] == 1


class TestOverlapPrevention:
    async def test_concurrent_cycle_skipped(
        self, config: Config, statistics: StatisticsCollector
    ) -> None:
        scheduler, _ = make_scheduler(config, statistics)
        gate = asyncio.Event()
        scheduler.register_scraper(
            StubScraper(config, statistics, "slow", [make_job(1)], gate=gate)
        )
        first = asyncio.create_task(scheduler.run_cycle())
        await asyncio.sleep(0)  # let the first cycle enter the scraper
        second = await scheduler.run_cycle()
        assert second is None
        assert statistics.snapshot()["counters"]["scheduler.skipped_runs"] == 1
        gate.set()
        report = await first
        assert report is not None
        assert report.jobs_found == 1


class TestGracefulShutdown:
    async def test_run_forever_stops_on_request(
        self, config: Config, statistics: StatisticsCollector
    ) -> None:
        scheduler, _ = make_scheduler(config, statistics)
        scraper = StubScraper(config, statistics, "quick", [make_job(1)])
        scheduler.register_scraper(scraper)
        task = asyncio.create_task(scheduler.run_forever())
        while scraper.calls == 0:
            await asyncio.sleep(0.01)
        scheduler.request_stop()  # scheduler is waiting out CHECK_INTERVAL
        await asyncio.wait_for(task, timeout=5)
        assert scraper.calls == 1

    async def test_stop_between_scrapers_finishes_cycle_early(
        self, config: Config, statistics: StatisticsCollector
    ) -> None:
        scheduler, _ = make_scheduler(config, statistics)
        first = StubScraper(
            config, statistics, "first", [make_job(1)], on_scrape=scheduler.request_stop
        )
        second = StubScraper(config, statistics, "second", [make_job(2)])
        scheduler.register_scraper(first)
        scheduler.register_scraper(second)
        report = await scheduler.run_cycle()
        assert report is not None
        assert first.calls == 1
        assert second.calls == 0  # graceful: current work finished, rest skipped
        assert report.scrapers_run == 1


class TestNeverDies:
    async def test_run_forever_survives_unexpected_cycle_failure(
        self, config: Config, statistics: StatisticsCollector
    ) -> None:
        class ExplodingPipeline:
            async def process(self, jobs):
                raise RuntimeError("contract violated")

        scheduler = Scheduler(config, ExplodingPipeline(), statistics)  # type: ignore[arg-type]
        scraper = StubScraper(config, statistics, "quick", [make_job(1)])
        scheduler.register_scraper(scraper)
        task = asyncio.create_task(scheduler.run_forever())
        while statistics.snapshot()["counters"].get("scheduler.unexpected_errors", 0) == 0:
            await asyncio.sleep(0.01)
        scheduler.request_stop()
        await asyncio.wait_for(task, timeout=5)  # loop survived and exited cleanly


class TestRuntimeHealth:
    async def test_health_checks_run_when_interval_elapsed(
        self, config: Config, statistics: StatisticsCollector
    ) -> None:
        health = RecordingHealth()
        scheduler, _ = make_scheduler(config, statistics, health, health_interval=0)
        scraper = StubScraper(config, statistics, "quick")
        scheduler.register_scraper(scraper)
        task = asyncio.create_task(scheduler.run_forever())
        while health.runtime_calls == 0:
            await asyncio.sleep(0.01)
        scheduler.request_stop()
        await asyncio.wait_for(task, timeout=5)
        assert health.runtime_calls >= 1

    async def test_health_checks_respect_interval(
        self, config: Config, statistics: StatisticsCollector
    ) -> None:
        health = RecordingHealth()
        scheduler, _ = make_scheduler(config, statistics, health, health_interval=10**9)
        scheduler.register_scraper(StubScraper(config, statistics, "quick"))
        task = asyncio.create_task(scheduler.run_forever())
        await asyncio.sleep(0.05)
        scheduler.request_stop()
        await asyncio.wait_for(task, timeout=5)
        assert health.runtime_calls == 0
