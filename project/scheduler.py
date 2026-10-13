"""Scheduler (Architecture.md, Requirements FR-005/FR-011, Failure Cases).

Periodic execution and workflow coordination only — no scraping, parsing,
SQL or notification logic lives here. Each cycle runs every registered
scraper and hands the results to the Filtering Pipeline:

    Scheduler → Scraper → Pipeline (normalize → validate → filter →
    database → telegram)

Guarantees: cycles never overlap (concurrent trigger is skipped and logged),
one scraper failure never stops the cycle (BaseScraper.run isolates it),
graceful shutdown finishes the current work before exiting, the scheduler
itself never terminates unexpectedly. Runtime health checks run every 30
minutes (Health Checks.md).
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass

from base_scraper import BaseScraper
from config import Config
from health import HealthChecker
from logger import logger
from pipeline import FilteringPipeline, PipelineSummary
from stats import StatisticsCollector

_RUNTIME_HEALTH_INTERVAL_SECONDS = 30 * 60


@dataclass
class CycleReport:
    scrapers_run: int = 0
    jobs_found: int = 0
    summary: PipelineSummary | None = None


class Scheduler:
    """Runs registered scrapers through the pipeline every CHECK_INTERVAL."""

    def __init__(
        self,
        config: Config,
        pipeline: FilteringPipeline,
        statistics: StatisticsCollector,
        health_checker: HealthChecker | None = None,
        health_interval_seconds: float = _RUNTIME_HEALTH_INTERVAL_SECONDS,
    ) -> None:
        self._interval = config.check_interval
        self._pipeline = pipeline
        self._statistics = statistics
        self._health_checker = health_checker
        self._health_interval = health_interval_seconds
        self._scrapers: list[BaseScraper] = []
        self._cycle_active = False
        self._stop_event = asyncio.Event()
        self._last_health_check = time.monotonic()

    # -- registration --------------------------------------------------------

    def register_scraper(self, scraper: BaseScraper) -> None:
        """Adding a website = create scraper, register, done (ADR-013)."""
        self._scrapers.append(scraper)
        logger.info("Scraper registered: {}", scraper.name)

    @property
    def scrapers(self) -> tuple[BaseScraper, ...]:
        return tuple(self._scrapers)

    # -- execution -----------------------------------------------------------

    async def run_cycle(self) -> CycleReport | None:
        """One monitoring cycle. Returns None when skipped due to overlap."""
        if self._cycle_active:
            logger.warning("Previous cycle still active — skipping this run")
            self._statistics.increment("scheduler.skipped_runs")
            return None
        self._cycle_active = True
        start = time.perf_counter()
        self._statistics.increment("scheduler.runs")
        report = CycleReport(summary=PipelineSummary())
        try:
            for scraper in self._scrapers:
                if self._stop_event.is_set():
                    logger.info("Stop requested — finishing cycle early")
                    break
                jobs = await scraper.run()  # never raises (BaseScraper contract)
                report.scrapers_run += 1
                report.jobs_found += len(jobs)
                summary = await self._pipeline.process(jobs)  # never raises
                self._merge(report.summary, summary)
            duration = time.perf_counter() - start
            logger.info(
                "Cycle complete: {} scrapers, {} jobs found, {} new, {} notified in {:.1f}s",
                report.scrapers_run,
                report.jobs_found,
                report.summary.inserted,
                report.summary.notified,
                duration,
            )
            self._statistics.increment("scheduler.completed_runs")
            self._statistics.record_time("scheduler.cycle_duration", duration * 1000)
            return report
        finally:
            self._cycle_active = False

    async def run_forever(self) -> None:
        """Main loop: cycle, wait CHECK_INTERVAL, repeat until stop requested."""
        logger.info(
            "Scheduler started: {} scrapers, interval {}s",
            len(self._scrapers),
            self._interval,
        )
        while not self._stop_event.is_set():
            try:
                await self.run_cycle()
                await self._maybe_run_health_checks()
            except Exception:
                # Lower layers isolate their failures; this is a structural
                # backstop — the scheduler must never terminate unexpectedly.
                logger.exception("Unexpected cycle failure — scheduler continues")
                self._statistics.increment("scheduler.unexpected_errors")
            if self._stop_event.is_set():
                break
            try:
                await asyncio.wait_for(self._stop_event.wait(), timeout=self._interval)
            except TimeoutError:
                continue
        logger.info("Scheduler stopped gracefully")

    def request_stop(self) -> None:
        """Graceful shutdown: current work finishes, then the loop exits."""
        logger.info("Scheduler stop requested")
        self._stop_event.set()

    # -- internals -----------------------------------------------------------

    async def _maybe_run_health_checks(self) -> None:
        if self._health_checker is None:
            return
        now = time.monotonic()
        if now - self._last_health_check < self._health_interval:
            return
        self._last_health_check = now
        await self._health_checker.run_runtime_checks()

    @staticmethod
    def _merge(total: PipelineSummary, part: PipelineSummary) -> None:
        total.received += part.received
        for outcome, count in part.outcomes.items():
            total.outcomes[outcome] = total.outcomes.get(outcome, 0) + count
