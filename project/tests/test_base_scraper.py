"""BaseScraper tests (BaseScraper.md unit-test checklist)."""

from __future__ import annotations

import pytest

from base_scraper import BaseScraper
from config import Config
from models import Job
from stats import StatisticsCollector


def make_job() -> Job:
    return Job.create(
        title="Product Designer",
        company="Miro",
        location="Berlin",
        url="https://example.com/jobs/1",
        source="LinkedIn",
    )


class GoodScraper(BaseScraper):
    name = "good"

    async def scrape(self) -> list[Job]:
        return [make_job()]


class EmptyScraper(BaseScraper):
    name = "empty"

    async def scrape(self) -> list[Job]:
        return []


class FailingScraper(BaseScraper):
    name = "failing"

    async def scrape(self) -> list[Job]:
        raise RuntimeError("site exploded")


class TestRun:
    async def test_successful_run_returns_jobs(
        self, config: Config, statistics: StatisticsCollector
    ) -> None:
        jobs = await GoodScraper(config, statistics).run()
        assert len(jobs) == 1
        counters = statistics.snapshot()["counters"]
        assert counters["scraper.good.runs"] == 1
        assert counters["scraper.good.successful_runs"] == 1
        assert counters["scraper.good.jobs_found"] == 1

    async def test_empty_result_is_success(
        self, config: Config, statistics: StatisticsCollector
    ) -> None:
        jobs = await EmptyScraper(config, statistics).run()
        assert jobs == []
        assert statistics.snapshot()["counters"]["scraper.empty.successful_runs"] == 1

    async def test_failure_returns_empty_never_raises(
        self, config: Config, statistics: StatisticsCollector
    ) -> None:
        jobs = await FailingScraper(config, statistics).run()
        assert jobs == []
        counters = statistics.snapshot()["counters"]
        assert counters["scraper.failing.failed_runs"] == 1
        assert "scraper.failing.successful_runs" not in counters

    async def test_duration_recorded(
        self, config: Config, statistics: StatisticsCollector
    ) -> None:
        await GoodScraper(config, statistics).run()
        assert statistics.snapshot()["timers"]["scraper.good.duration"]["count"] == 1

    async def test_duration_recorded_on_failure(
        self, config: Config, statistics: StatisticsCollector
    ) -> None:
        await FailingScraper(config, statistics).run()
        assert statistics.snapshot()["timers"]["scraper.failing.duration"]["count"] == 1


class TestAbstract:
    def test_cannot_instantiate_without_scrape(
        self, config: Config, statistics: StatisticsCollector
    ) -> None:
        with pytest.raises(TypeError):
            BaseScraper(config, statistics)  # type: ignore[abstract]
