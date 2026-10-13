"""Performance-budget sanity tests (Performance Budget.md, Filtering
Pipeline.md, LinkedIn Test Cases load scenarios).

Bounds are the documented budgets — real numbers are far below them, so
these fail only on genuine regressions, not CI jitter.
"""

from __future__ import annotations

import time
from collections.abc import AsyncIterator
from pathlib import Path

import pytest

from config import Config
from database import Database
from models import Job
from normalizer import Normalizer
from pipeline import FilteringPipeline
from stats import StatisticsCollector
from utils import generate_job_hash


def make_job(n: int) -> Job:
    return Job.create(
        title=f"Senior Product Designer {n} (m/w/d)",
        company="Miro",
        location="Berlin",
        url=f"https://example.com/jobs/{n}",
        source="LinkedIn",
        posted_time="2 hours ago",
    )


class SilentNotifier:
    async def send(self, job: Job) -> bool:
        return True


@pytest.fixture
async def pipeline(tmp_path: Path) -> AsyncIterator[tuple[FilteringPipeline, Database]]:
    config = Config(
        telegram_token="t",
        chat_id="c",
        database_url=f"sqlite+aiosqlite:///{(tmp_path / 'jobs.db').as_posix()}",
    )
    database = Database(config, StatisticsCollector())
    await database.start()
    yield FilteringPipeline(config, Normalizer(), database, SilentNotifier()), database
    await database.close()


class TestBudgets:
    async def test_pipeline_stays_under_100ms_per_job(self, pipeline) -> None:
        pipe, _ = pipeline
        jobs = [make_job(n) for n in range(200)]
        start = time.perf_counter()
        summary = await pipe.process(jobs)
        elapsed = time.perf_counter() - start
        assert summary.inserted == 200
        # Filtering Pipeline.md: <100 ms per job
        assert elapsed / len(jobs) < 0.1, f"{elapsed / len(jobs) * 1000:.1f} ms per job"

    async def test_duplicate_detection_stays_fast_at_scale(self, pipeline) -> None:
        pipe, database = pipeline
        await pipe.process([make_job(n) for n in range(100)])
        duplicates = [make_job(n) for n in range(100)] * 5  # 500 duplicate hits
        start = time.perf_counter()
        summary = await pipe.process(duplicates)
        elapsed = time.perf_counter() - start
        assert summary.inserted == 0
        assert (await database.get_statistics())["total_jobs"] == 100
        assert elapsed / len(duplicates) < 0.1

    def test_hash_generation_under_1ms(self) -> None:
        start = time.perf_counter()
        for n in range(1000):
            generate_job_hash(f"Title {n}", "Company", f"https://x.com/{n}")
        elapsed = time.perf_counter() - start
        # Performance Budget.md: hash generation <1 ms
        assert elapsed / 1000 < 0.001

    def test_title_normalization_throughput(self) -> None:
        normalizer = Normalizer()
        start = time.perf_counter()
        for n in range(1000):
            normalizer.normalize_title(f"  Senior   Product Designer {n} (m/w/d) | Berlin ")
        elapsed = time.perf_counter() - start
        # Extraction budget: normalization <5 ms per field
        assert elapsed / 1000 < 0.005
