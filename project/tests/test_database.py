"""Database layer integration tests (Database.md unit-test checklist).

Real SQLite files in tmp_path, real Alembic migrations, real async engine.
Verifies: migrations, insert, duplicate detection (fast path + unique-index
race), rollback recovery, recent jobs, statistics.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from config import Config
from database import Database, InsertResult
from models import Job
from stats import StatisticsCollector


def make_config(tmp_path: Path) -> Config:
    url = f"sqlite+aiosqlite:///{(tmp_path / 'db' / 'jobs.db').as_posix()}"
    return Config(telegram_token="t", chat_id="c", database_url=url)


def make_job(n: int = 1, **overrides: object) -> Job:
    defaults: dict[str, object] = {
        "title": f"Product Designer {n}",
        "company": "Miro",
        "location": "Berlin",
        "url": f"https://example.com/jobs/{n}",
        "source": "LinkedIn",
        "posted_time": "2 hours ago",
    }
    defaults.update(overrides)
    return Job.create(**defaults)  # type: ignore[arg-type]


@pytest.fixture
def db_config(tmp_path: Path) -> Config:
    return make_config(tmp_path)


@pytest.fixture
async def database(db_config: Config, statistics: StatisticsCollector) -> AsyncIterator[Database]:
    db = Database(db_config, statistics)
    await db.start()
    yield db
    await db.close()


class TestMigrations:
    async def test_start_creates_database_file(self, db_config: Config, database: Database) -> None:
        file_part = db_config.database_url.rsplit(":///", 1)[-1]
        assert Path(file_part).exists()

    async def test_alembic_version_at_head(self, db_config: Config, database: Database) -> None:
        engine = create_async_engine(db_config.database_url)
        try:
            async with engine.connect() as conn:
                version = await conn.scalar(text("SELECT version_num FROM alembic_version"))
        finally:
            await engine.dispose()
        assert version == "0001"

    async def test_unique_index_on_hash(self, db_config: Config, database: Database) -> None:
        engine = create_async_engine(db_config.database_url)
        try:
            async with engine.connect() as conn:
                rows = (await conn.execute(text("PRAGMA index_list('jobs')"))).mappings().all()
        finally:
            await engine.dispose()
        by_name = {row["name"]: row for row in rows}
        assert by_name["ix_jobs_hash"]["unique"] == 1
        assert "ix_jobs_source" in by_name
        assert "ix_jobs_date_found" in by_name

    async def test_migrations_idempotent(
        self, db_config: Config, statistics: StatisticsCollector
    ) -> None:
        first = Database(db_config, statistics)
        await first.start()
        await first.insert_job(make_job(1))
        await first.close()
        # Second start on the same file: upgrade head is a no-op, data survives
        second = Database(db_config, statistics)
        await second.start()
        try:
            assert await second.job_exists(make_job(1).hash)
        finally:
            await second.close()


class TestInsert:
    async def test_insert_new_job(self, database: Database) -> None:
        assert await database.insert_job(make_job(1)) == InsertResult.INSERTED
        assert await database.job_exists(make_job(1).hash)

    async def test_row_fields_persisted(self, database: Database) -> None:
        stamp = datetime(2026, 7, 20, 12, 30, tzinfo=UTC)
        job = make_job(1, discovered_at=stamp)
        await database.insert_job(job)
        stored = (await database.get_recent_jobs())[0]
        assert stored.title == job.title
        assert stored.company == job.company
        assert stored.location == job.location
        assert stored.url == job.url
        assert stored.source == job.source
        assert stored.posted_time == job.posted_time
        assert stored.discovered_at == stamp
        assert stored.hash == job.hash

    async def test_distinct_jobs_both_inserted(self, database: Database) -> None:
        assert await database.insert_job(make_job(1)) == InsertResult.INSERTED
        assert await database.insert_job(make_job(2)) == InsertResult.INSERTED
        stats = await database.get_statistics()
        assert stats["total_jobs"] == 2

    async def test_not_started_raises(self, db_config: Config) -> None:
        db = Database(db_config)
        with pytest.raises(RuntimeError, match="not started"):
            await db.insert_job(make_job(1))


class TestDeduplication:
    async def test_duplicate_ignored(self, database: Database) -> None:
        await database.insert_job(make_job(1))
        result = await database.insert_job(make_job(1, posted_time="5 hours ago"))
        assert result == InsertResult.DUPLICATE
        stats = await database.get_statistics()
        assert stats["total_jobs"] == 1
        assert stats["duplicates_skipped"] == 1

    async def test_duplicate_metrics(
        self, database: Database, statistics: StatisticsCollector
    ) -> None:
        await database.insert_job(make_job(1))
        await database.insert_job(make_job(1))
        counters = statistics.snapshot()["counters"]
        assert counters["database.inserts"] == 1
        assert counters["database.duplicates"] == 1

    async def test_race_caught_by_unique_index(
        self, database: Database, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Fast-path lookup blinded — the unique index must still win."""
        await database.insert_job(make_job(1))

        async def blind_lookup(session: object, job_hash: str) -> bool:
            return False

        monkeypatch.setattr(database, "_hash_exists", blind_lookup)
        result = await database.insert_job(make_job(1))
        assert result == InsertResult.DUPLICATE
        monkeypatch.undo()
        stats = await database.get_statistics()
        assert stats["total_jobs"] == 1
        assert stats["duplicates_skipped"] == 1


class TestRollback:
    async def test_failed_insert_rolls_back_and_recovers(
        self, database: Database, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        await database.insert_job(make_job(1))

        async def blind_lookup(session: object, job_hash: str) -> bool:
            return False

        monkeypatch.setattr(database, "_hash_exists", blind_lookup)
        await database.insert_job(make_job(1))  # IntegrityError path → rollback
        monkeypatch.undo()

        # Layer stays usable and consistent after the rolled-back transaction
        assert await database.insert_job(make_job(2)) == InsertResult.INSERTED
        stats = await database.get_statistics()
        assert stats["total_jobs"] == 2


class TestRecentJobs:
    async def test_ordered_newest_first(self, database: Database) -> None:
        base = datetime(2026, 7, 1, tzinfo=UTC)
        for n in range(3):
            await database.insert_job(make_job(n, discovered_at=base + timedelta(days=n)))
        recent = await database.get_recent_jobs()
        dates = [job.discovered_at for job in recent]
        assert dates == sorted(dates, reverse=True)

    async def test_limit_respected(self, database: Database) -> None:
        for n in range(5):
            await database.insert_job(make_job(n))
        assert len(await database.get_recent_jobs(limit=2)) == 2

    async def test_roundtrip_equality(self, database: Database) -> None:
        job = make_job(1)
        await database.insert_job(job)
        assert (await database.get_recent_jobs())[0] == job  # hash equality


class TestStatistics:
    async def test_aggregates(self, database: Database) -> None:
        now = datetime.now(UTC)
        await database.insert_job(make_job(1, source="LinkedIn", discovered_at=now))
        await database.insert_job(make_job(2, source="Indeed", discovered_at=now))
        await database.insert_job(
            make_job(3, source="LinkedIn", discovered_at=now - timedelta(days=10))
        )
        await database.insert_job(make_job(1))  # duplicate
        stats = await database.get_statistics()
        assert stats["total_jobs"] == 3
        assert stats["jobs_today"] == 2
        assert stats["jobs_this_week"] == 2
        assert stats["jobs_per_source"] == {"LinkedIn": 2, "Indeed": 1}
        assert stats["duplicates_skipped"] == 1
        assert stats["average_jobs_per_day"] == 1.5  # 3 jobs over 2 distinct days

    async def test_empty_database(self, database: Database) -> None:
        stats = await database.get_statistics()
        assert stats["total_jobs"] == 0
        assert stats["jobs_per_source"] == {}
        assert stats["average_jobs_per_day"] == 0.0


class TestBusyRetry:
    async def test_locked_insert_retried_via_policy(
        self, db_config: Config, statistics: StatisticsCollector, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from sqlalchemy.exc import OperationalError

        from retry import RetryPolicy

        async def instant_sleep(seconds: float) -> None:
            return None

        db = Database(db_config, statistics, RetryPolicy(db_config, statistics, sleep=instant_sleep))
        await db.start()
        try:
            original = db._insert_once
            calls = {"n": 0}

            async def flaky(job: Job) -> InsertResult:
                calls["n"] += 1
                if calls["n"] == 1:
                    raise OperationalError("INSERT", {}, Exception("database is locked"))
                return await original(job)

            monkeypatch.setattr(db, "_insert_once", flaky)
            result = await db.insert_job(make_job(1))
            assert result == InsertResult.INSERTED
            assert calls["n"] == 2
        finally:
            await db.close()

    async def test_without_policy_locked_error_propagates(
        self, db_config: Config, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from sqlalchemy.exc import OperationalError

        db = Database(db_config)
        await db.start()
        try:
            async def always_locked(job: Job) -> InsertResult:
                raise OperationalError("INSERT", {}, Exception("database is locked"))

            monkeypatch.setattr(db, "_insert_once", always_locked)
            with pytest.raises(OperationalError):
                await db.insert_job(make_job(1))
        finally:
            await db.close()


class TestLifecycle:
    async def test_close_idempotent(self, db_config: Config) -> None:
        db = Database(db_config)
        await db.start()
        await db.close()
        await db.close()

    async def test_operations_after_close_raise(self, db_config: Config) -> None:
        db = Database(db_config)
        await db.start()
        await db.close()
        with pytest.raises(RuntimeError, match="not started"):
            await db.job_exists("x" * 64)
