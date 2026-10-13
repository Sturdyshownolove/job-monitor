"""Database layer (Database.md v2.0, ADR-002, ADR-009).

SQLite accessed exclusively through SQLAlchemy 2.x (async engine, aiosqlite).
Schema is versioned by Alembic; `start()` applies pending migrations
automatically. Repository pattern: application code calls high-level methods
only — no SQL outside this module.

Duplicate detection lives here and nowhere else: the unique `hash` column is
the source of truth, an in-transaction lookup is the fast path, and
IntegrityError covers the race. Scrapers never touch this layer.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from pathlib import Path

from alembic import command
from alembic.config import Config as AlembicConfig
from sqlalchemy import DateTime, String, Text, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from config import Config
from logger import logger
from models import Job
from stats import StatisticsCollector

_MIGRATIONS_DIR = Path(__file__).resolve().parent / "database" / "migrations"


class Base(DeclarativeBase):
    pass


class JobRecord(Base):
    """ORM row for the `jobs` table. Persistence detail — never leaves this module."""

    __tablename__ = "jobs"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    title: Mapped[str] = mapped_column(Text, nullable=False)
    company: Mapped[str] = mapped_column(Text, nullable=False)
    location: Mapped[str] = mapped_column(Text, nullable=False)
    url: Mapped[str] = mapped_column(Text, nullable=False)
    source: Mapped[str] = mapped_column(String(30), nullable=False, index=True)
    posted_time: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Stored as naive UTC so SQLite comparisons stay deterministic.
    date_found: Mapped[datetime] = mapped_column(DateTime, nullable=False, index=True)
    hash: Mapped[str] = mapped_column(String(64), nullable=False, unique=True, index=True)


class InsertResult(StrEnum):
    INSERTED = "inserted"
    DUPLICATE = "duplicate"


class Database:
    """Persistence service: engine lifecycle + jobs repository."""

    def __init__(
        self,
        config: Config,
        statistics: StatisticsCollector | None = None,
        retry_policy: "object | None" = None,
    ) -> None:
        self._url = config.database_url
        self._statistics = statistics
        self._retry_policy = retry_policy  # RetryPolicy; retries busy/locked inserts
        self._engine: AsyncEngine | None = None
        self._sessions: async_sessionmaker[AsyncSession] | None = None
        self._duplicates_skipped = 0

    # -- lifecycle -----------------------------------------------------------

    async def start(self) -> None:
        """Create directories, apply Alembic migrations, open the async engine."""
        self._ensure_directory()
        await asyncio.to_thread(self._run_migrations)
        self._engine = create_async_engine(self._url)
        self._sessions = async_sessionmaker(self._engine, expire_on_commit=False)
        logger.info("Database ready ({})", self._url)

    async def close(self) -> None:
        if self._engine is not None:
            await self._engine.dispose()
            self._engine = None
            self._sessions = None
            logger.info("Database closed")

    def _ensure_directory(self) -> None:
        file_part = self._url.rsplit(":///", 1)[-1]
        if file_part and file_part != ":memory:":
            Path(file_part).parent.mkdir(parents=True, exist_ok=True)

    def _run_migrations(self) -> None:
        # Migrations run on a sync engine inside a worker thread — the async
        # event loop never blocks and Alembic needs no async plumbing.
        alembic_config = AlembicConfig()
        alembic_config.set_main_option("script_location", str(_MIGRATIONS_DIR))
        alembic_config.set_main_option("sqlalchemy.url", self._url.replace("+aiosqlite", ""))
        command.upgrade(alembic_config, "head")
        logger.info("Database migrations applied (head)")

    def _session_factory(self) -> async_sessionmaker[AsyncSession]:
        if self._sessions is None:
            raise RuntimeError("Database not started — call start() first")
        return self._sessions

    # -- repository ----------------------------------------------------------

    async def insert_job(self, job: Job) -> InsertResult:
        """Insert a job unless its hash exists. Transactional; race-safe.

        With a RetryPolicy injected, transient SQLite busy/locked errors are
        retried (Failure Cases: SQLite Busy → retry).
        """
        if self._retry_policy is None:
            return await self._insert_once(job)

        async def _attempt() -> InsertResult:
            return await self._insert_once(job)

        return await self._retry_policy.execute(  # type: ignore[union-attr]
            _attempt, operation_name="database.insert"
        )

    async def _insert_once(self, job: Job) -> InsertResult:
        sessions = self._session_factory()
        try:
            async with sessions() as session:
                async with session.begin():
                    if await self._hash_exists(session, job.hash):
                        self._register_duplicate(job)
                        return InsertResult.DUPLICATE
                    session.add(self._to_record(job))
        except IntegrityError:
            # Unique-hash race: session.begin() already rolled back.
            self._register_duplicate(job)
            return InsertResult.DUPLICATE
        logger.info("Job stored: {} @ {} [{}]", job.title, job.company, job.source)
        if self._statistics is not None:
            self._statistics.increment("database.inserts")
        return InsertResult.INSERTED

    async def job_exists(self, job_hash: str) -> bool:
        sessions = self._session_factory()
        async with sessions() as session:
            return await self._hash_exists(session, job_hash)

    async def get_recent_jobs(self, limit: int = 20) -> list[Job]:
        sessions = self._session_factory()
        async with sessions() as session:
            rows = await session.scalars(
                select(JobRecord)
                .order_by(JobRecord.date_found.desc(), JobRecord.id.desc())
                .limit(limit)
            )
            return [self._to_job(record) for record in rows]

    async def get_statistics(self) -> dict[str, object]:
        """Aggregate metrics defined in Database.md (Statistics section)."""
        now = datetime.now(UTC).replace(tzinfo=None)
        today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        week_start = now - timedelta(days=7)
        sessions = self._session_factory()
        async with sessions() as session:
            total = await session.scalar(select(func.count()).select_from(JobRecord)) or 0
            today = await session.scalar(
                select(func.count()).select_from(JobRecord).where(JobRecord.date_found >= today_start)
            ) or 0
            week = await session.scalar(
                select(func.count()).select_from(JobRecord).where(JobRecord.date_found >= week_start)
            ) or 0
            per_source_rows = await session.execute(
                select(JobRecord.source, func.count()).group_by(JobRecord.source)
            )
            distinct_days = await session.scalar(
                select(func.count(func.distinct(func.date(JobRecord.date_found))))
            ) or 0
        return {
            "total_jobs": total,
            "jobs_today": today,
            "jobs_this_week": week,
            "jobs_per_source": dict(per_source_rows.all()),
            "duplicates_skipped": self._duplicates_skipped,
            "average_jobs_per_day": round(total / distinct_days, 2) if distinct_days else 0.0,
        }

    async def health_check(self) -> bool:
        """Read probe + write/rollback probe (Health Checks.md database check).

        The probe row is flushed inside a transaction and rolled back — it
        must never persist.
        """
        try:
            sessions = self._session_factory()
            async with sessions() as session:
                await session.scalar(select(func.count()).select_from(JobRecord))
                session.add(
                    JobRecord(
                        title="__health_probe__",
                        company="__health_probe__",
                        location="-",
                        url="https://health.probe.invalid",
                        source="LinkedIn",
                        posted_time=None,
                        date_found=datetime.now(UTC).replace(tzinfo=None),
                        hash="0" * 64,
                    )
                )
                await session.flush()
                await session.rollback()
            return True
        except Exception as exc:
            logger.warning("Database health check failed: {}", exc)
            return False

    # -- internals -----------------------------------------------------------

    async def _hash_exists(self, session: AsyncSession, job_hash: str) -> bool:
        found = await session.scalar(select(JobRecord.id).where(JobRecord.hash == job_hash))
        return found is not None

    def _register_duplicate(self, job: Job) -> None:
        self._duplicates_skipped += 1
        logger.debug("Duplicate skipped: {} @ {}", job.title, job.company)
        if self._statistics is not None:
            self._statistics.increment("database.duplicates")

    @staticmethod
    def _to_record(job: Job) -> JobRecord:
        return JobRecord(
            title=job.title,
            company=job.company,
            location=job.location,
            url=job.url,
            source=job.source,
            posted_time=job.posted_time,
            date_found=job.discovered_at.astimezone(UTC).replace(tzinfo=None),
            hash=job.hash,
        )

    @staticmethod
    def _to_job(record: JobRecord) -> Job:
        return Job(
            title=record.title,
            company=record.company,
            location=record.location,
            url=record.url,
            source=record.source,
            posted_time=record.posted_time,
            discovered_at=record.date_found.replace(tzinfo=UTC),
            hash=record.hash,
        )
