"""Full-stack pipeline integration: real Database (tmp SQLite + Alembic),
real TelegramService (mocked transport), real Normalizer.

Verifies the documented guarantees end to end:
- every accepted job is stored and generates exactly one notification
- duplicates never notify
- rejected jobs never reach the database
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from pathlib import Path

import httpx
import pytest

from config import Config
from database import Database
from models import Job
from normalizer import Normalizer
from pipeline import FilteringPipeline, PipelineOutcome
from retry import RetryPolicy
from stats import StatisticsCollector
from telegram import TelegramService


def make_job(n: int = 1, **overrides: object) -> Job:
    defaults: dict[str, object] = {
        "title": "Senior Product Designer",
        "company": "Miro",
        "location": "Berlin",
        "url": f"https://example.com/jobs/{n}",
        "source": "LinkedIn",
        "posted_time": "2 hours ago",
    }
    defaults.update(overrides)
    return Job.create(**defaults)  # type: ignore[arg-type]


class TelegramRecorder:
    def __init__(self) -> None:
        self.messages: list[dict[str, str]] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.messages.append(json.loads(request.content))
        return httpx.Response(200, json={"ok": True})


@pytest.fixture
async def stack(
    tmp_path: Path,
) -> AsyncIterator[tuple[FilteringPipeline, Database, TelegramRecorder, StatisticsCollector]]:
    config = Config(
        telegram_token="123:tok",
        chat_id="42",
        retry_jitter=False,
        database_url=f"sqlite+aiosqlite:///{(tmp_path / 'jobs.db').as_posix()}",
    )
    statistics = StatisticsCollector()
    database = Database(config, statistics)
    await database.start()

    recorder = TelegramRecorder()

    async def instant_sleep(seconds: float) -> None:
        return None

    telegram = TelegramService(
        config,
        RetryPolicy(config, statistics, sleep=instant_sleep),
        statistics,
        transport=httpx.MockTransport(recorder.handler),
        send_interval=0.0,
        sleep=instant_sleep,
    )
    pipeline = FilteringPipeline(config, Normalizer(), database, telegram, statistics)
    yield pipeline, database, recorder, statistics
    await telegram.close()
    await database.close()


class TestEndToEnd:
    async def test_mixed_batch(self, stack) -> None:
        pipeline, database, recorder, _ = stack
        jobs = [
            make_job(1),                                        # accepted
            make_job(2, title="Graphic Designer"),              # keyword reject
            make_job(3, location="Paris"),                      # country reject
            make_job(1, title="Senior Product Designer (m/w/d)"),  # duplicate of 1
            make_job(4, title="Principal UX Designer", location="Munich"),  # accepted
        ]
        summary = await pipeline.process(jobs)

        assert summary.received == 5
        assert summary.notified == 2
        assert summary.outcomes[PipelineOutcome.DUPLICATE] == 1
        assert summary.outcomes[PipelineOutcome.REJECTED_TITLE] == 1
        assert summary.outcomes[PipelineOutcome.REJECTED_COUNTRY] == 1

        db_stats = await database.get_statistics()
        assert db_stats["total_jobs"] == 2
        assert db_stats["duplicates_skipped"] == 1

        # Exactly one notification per stored job, none for rejects/duplicates
        assert len(recorder.messages) == 2
        texts = [message["text"] for message in recorder.messages]
        assert any("Miro" in text for text in texts)

    async def test_rerun_produces_no_new_notifications(self, stack) -> None:
        pipeline, _, recorder, _ = stack
        jobs = [make_job(1), make_job(2, title="UX Designer", location="Hamburg")]
        await pipeline.process(jobs)
        assert len(recorder.messages) == 2
        # Second scheduler cycle discovers the same jobs — all duplicates
        summary = await pipeline.process(jobs)
        assert summary.outcomes[PipelineOutcome.DUPLICATE] == 2
        assert len(recorder.messages) == 2

    async def test_variant_titles_dedup_across_runs(self, stack) -> None:
        pipeline, database, recorder, _ = stack
        await pipeline.process([make_job(1, title="Senior Product Designer")])
        summary = await pipeline.process(
            [make_job(1, title="Senior Product Designer (m/f/d) | Berlin")]
        )
        assert summary.outcomes[PipelineOutcome.DUPLICATE] == 1
        assert (await database.get_statistics())["total_jobs"] == 1
        assert len(recorder.messages) == 1

    async def test_statistics_flow(self, stack) -> None:
        pipeline, _, _, statistics = stack
        await pipeline.process([make_job(1), make_job(1)])
        counters = statistics.snapshot()["counters"]
        assert counters["pipeline.jobs_received"] == 2
        assert counters["pipeline.jobs_inserted"] == 1
        assert counters["pipeline.duplicates"] == 1
        assert counters["telegram.messages_sent"] == 1
        assert counters["database.inserts"] == 1
        assert counters["database.duplicates"] == 1
