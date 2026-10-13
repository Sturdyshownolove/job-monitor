"""Filtering Pipeline (Filtering Pipeline.md, ADR-005).

The single path from scraper output to storage and notification. Stateless;
every scraper's jobs pass through the same stages in the documented order:

    Normalize → Validate → Keyword Filter → Country Filter →
    Hash → Duplicate Check → Insert → Notify

The pipeline re-normalizes and rebuilds every Job so the SHA256 hash is
always computed from canonical values — title variants like "(m/w/d)"
deduplicate correctly regardless of which scraper produced them.

Duplicate detection uses only the database insert result (single race-safe
call). Notification fires only after a successful insert. A failure in any
stage rejects that job and the pipeline continues — it always survives.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Protocol

from config import Config
from database import Database, InsertResult
from language_check import LanguageChecker, LanguageVerdict
from logger import logger
from models import Job
from normalizer import Normalizer
from stats import StatisticsCollector


class Notifier(Protocol):
    """Notification provider interface (Telegram today, others later)."""

    async def send(self, job: Job) -> bool: ...


class PipelineOutcome(StrEnum):
    INSERTED_AND_NOTIFIED = "inserted_and_notified"
    INSERTED_NOTIFY_FAILED = "inserted_notify_failed"
    DUPLICATE = "duplicate"
    REJECTED_TITLE = "rejected_title"
    REJECTED_COUNTRY = "rejected_country"
    REJECTED_LANGUAGE = "rejected_language"
    REJECTED_INVALID = "rejected_invalid"
    ERROR = "error"


@dataclass
class PipelineSummary:
    received: int = 0
    outcomes: dict[PipelineOutcome, int] = field(default_factory=dict)

    def record(self, outcome: PipelineOutcome) -> None:
        self.outcomes[outcome] = self.outcomes.get(outcome, 0) + 1

    @property
    def notified(self) -> int:
        return self.outcomes.get(PipelineOutcome.INSERTED_AND_NOTIFIED, 0)

    @property
    def inserted(self) -> int:
        return self.notified + self.outcomes.get(PipelineOutcome.INSERTED_NOTIFY_FAILED, 0)


class FilteringPipeline:
    """Stateless job pipeline. All dependencies injected."""

    def __init__(
        self,
        config: Config,
        normalizer: Normalizer,
        database: Database,
        notifier: Notifier,
        statistics: StatisticsCollector | None = None,
        language_checker: LanguageChecker | None = None,
    ) -> None:
        self._keywords = tuple(keyword.lower() for keyword in config.keywords)
        self._normalizer = normalizer
        self._database = database
        self._notifier = notifier
        self._statistics = statistics
        self._language_checker = language_checker

    async def process(self, jobs: Iterable[Job]) -> PipelineSummary:
        """Run every job through the pipeline. Never raises."""
        summary = PipelineSummary()
        for job in jobs:
            summary.received += 1
            outcome = await self.process_job(job)
            summary.record(outcome)
        logger.info(
            "Pipeline finished: {} received, {} inserted, {} notified",
            summary.received,
            summary.inserted,
            summary.notified,
        )
        return summary

    async def process_job(self, job: Job) -> PipelineOutcome:
        """One job through all stages. Failures reject the job, never propagate."""
        self._count("pipeline.jobs_received")
        try:
            normalized = self._normalize(job)
            if normalized is None:
                return self._reject(job, PipelineOutcome.REJECTED_INVALID, "invalid data")
            if not self._matches_keywords(normalized.title):
                return self._reject(job, PipelineOutcome.REJECTED_TITLE, "title not allowed")
            if not self._normalizer.is_germany_eligible(job.location):
                return self._reject(job, PipelineOutcome.REJECTED_COUNTRY, "outside Germany")

            result = await self._database.insert_job(normalized)
            if result == InsertResult.DUPLICATE:
                self._count("pipeline.duplicates")
                return PipelineOutcome.DUPLICATE

            self._count("pipeline.jobs_inserted")
            # Language check runs after insert: a rejected job stays stored,
            # so it is never fetched or re-evaluated on later cycles.
            if self._language_checker is not None and self._language_checker.enabled:
                verdict = await self._language_checker.check(normalized)
                if verdict == LanguageVerdict.GERMAN_ONLY:
                    logger.info(
                        "Job suppressed (German required, no English): {} @ {}",
                        normalized.title,
                        normalized.company,
                    )
                    self._count("pipeline.rejected_language")
                    return PipelineOutcome.REJECTED_LANGUAGE
            if await self._notifier.send(normalized):
                self._count("pipeline.notifications_sent")
                return PipelineOutcome.INSERTED_AND_NOTIFIED
            # Job is stored; only the notification failed. Never re-notify —
            # the duplicate check guarantees at most one notification per job.
            self._count("pipeline.notifications_failed")
            return PipelineOutcome.INSERTED_NOTIFY_FAILED
        except Exception:
            logger.exception("Pipeline stage failed for {} @ {}", job.title, job.company)
            self._count("pipeline.errors")
            return PipelineOutcome.ERROR

    # -- stages --------------------------------------------------------------

    def _normalize(self, job: Job) -> Job | None:
        """Stages 1+2: canonical values, then validity via Job.create()."""
        title = self._normalizer.normalize_title(job.title)
        company = self._normalizer.normalize_company(job.company)
        location = self._normalizer.normalize_location(job.location)
        url = self._normalizer.normalize_url(job.url)
        if title is None or company is None or location is None or url is None:
            return None
        try:
            return Job.create(
                title=title,
                company=company,
                location=location,
                url=url,
                source=job.source,
                posted_time=self._normalizer.normalize_text(job.posted_time),
                language_note=self._normalizer.normalize_text(job.language_note),
                discovered_at=job.discovered_at,
            )
        except Exception as exc:  # pydantic ValidationError, ValueError from hashing
            logger.warning("Job failed validation after normalization: {}", exc)
            return None

    def _matches_keywords(self, title: str) -> bool:
        lowered = title.lower()
        return any(keyword in lowered for keyword in self._keywords)

    # -- helpers -------------------------------------------------------------

    def _reject(self, job: Job, outcome: PipelineOutcome, reason: str) -> PipelineOutcome:
        logger.debug("Job rejected ({}): {} @ {}", reason, job.title, job.company)
        self._count(f"pipeline.{outcome.value}")
        return outcome

    def _count(self, name: str) -> None:
        if self._statistics is not None:
            self._statistics.increment(name)
