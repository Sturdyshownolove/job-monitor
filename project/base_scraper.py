"""BaseScraper (BaseScraper.md).

Abstract interface every website scraper inherits. Hosts the shared run
lifecycle: logging, timing, statistics and failure isolation. `run()` never
raises — one scraper failure never stops the application.
"""

from __future__ import annotations

import time
from abc import ABC, abstractmethod

from config import Config
from logger import logger
from models import Job
from stats import StatisticsCollector


class BaseScraper(ABC):
    """Template for platform scrapers. Subclasses implement scrape() only."""

    name: str = "base"

    def __init__(self, config: Config, statistics: StatisticsCollector) -> None:
        self._config = config
        self._statistics = statistics

    @abstractmethod
    async def scrape(self) -> list[Job]:
        """Platform-specific discovery. Returns normalized Job objects only."""

    async def run(self) -> list[Job]:
        """Scheduler entry point. Catches every failure; never raises."""
        prefix = f"scraper.{self.name}"
        logger.info("Scraper {} started", self.name)
        self._statistics.increment(f"{prefix}.runs")
        start = time.perf_counter()
        try:
            jobs = await self.scrape()
        except Exception:
            duration = time.perf_counter() - start
            logger.exception("Scraper {} failed after {:.1f}s", self.name, duration)
            self._statistics.increment(f"{prefix}.failed_runs")
            self._statistics.record_time(f"{prefix}.duration", duration * 1000)
            return []
        duration = time.perf_counter() - start
        logger.info(
            "Scraper {} finished: {} jobs in {:.1f}s", self.name, len(jobs), duration
        )
        self._statistics.increment(f"{prefix}.successful_runs")
        self._statistics.increment(f"{prefix}.jobs_found", len(jobs))
        self._statistics.record_time(f"{prefix}.duration", duration * 1000)
        return jobs
