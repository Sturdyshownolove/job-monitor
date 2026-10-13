"""Health Check Framework (Health Checks.md).

Startup validation before the scheduler starts, plus lightweight runtime
checks. Levels: HEALTHY, WARNING (continue), CRITICAL (do not start /
disable affected functionality). Telegram and Playwright failures are
WARNING per spec; configuration, internet, database and dependency failures
are CRITICAL.
"""

from __future__ import annotations

import importlib
import sys
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path

import httpx

from config import Config
from logger import logger
from stats import StatisticsCollector

_INTERNET_TARGETS: tuple[str, ...] = (
    "https://www.google.com",
    "https://www.cloudflare.com",
)
_INTERNET_TIMEOUT = 5.0
_REQUIRED_MODULES: tuple[str, ...] = (
    "httpx",
    "playwright",
    "bs4",
    "lxml",
    "sqlalchemy",
    "alembic",
    "aiosqlite",
    "pydantic",
    "loguru",
    "typer",
    "tenacity",
    "dotenv",
)
_REQUIRED_DIRS: tuple[str, ...] = ("logs", "database")
_MIN_PYTHON = (3, 13)


class HealthStatus(StrEnum):
    HEALTHY = "healthy"
    WARNING = "warning"
    CRITICAL = "critical"


@dataclass(frozen=True)
class CheckResult:
    name: str
    status: HealthStatus
    message: str = ""


@dataclass
class HealthReport:
    results: list[CheckResult] = field(default_factory=list)

    @property
    def overall(self) -> HealthStatus:
        statuses = {result.status for result in self.results}
        if HealthStatus.CRITICAL in statuses:
            return HealthStatus.CRITICAL
        if HealthStatus.WARNING in statuses:
            return HealthStatus.WARNING
        return HealthStatus.HEALTHY

    @property
    def ok(self) -> bool:
        """Startup gate: only CRITICAL blocks the application."""
        return self.overall != HealthStatus.CRITICAL

    def summary(self) -> str:
        lines = [
            f"{result.name}: {result.status.value}"
            + (f" ({result.message})" if result.message else "")
            for result in self.results
        ]
        lines.append(f"overall: {self.overall.value}")
        return "\n".join(lines)


class HealthChecker:
    """Runs startup and runtime checks against injected services.

    Services left as None are skipped (e.g. no BrowserManager when the
    LinkedIn scraper is disabled). `transport` mocks the internet check in
    tests; `base_dir` relocates filesystem checks.
    """

    def __init__(
        self,
        config: Config,
        database: object | None = None,
        telegram: object | None = None,
        browser_manager: object | None = None,
        statistics: StatisticsCollector | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
        base_dir: Path = Path("."),
    ) -> None:
        self._config = config
        self._database = database
        self._telegram = telegram
        self._browser_manager = browser_manager
        self._statistics = statistics
        self._transport = transport
        self._base_dir = base_dir

    async def run_startup_checks(self) -> HealthReport:
        """Full pre-start validation (Health Checks.md startup checks)."""
        report = HealthReport()
        report.results.append(self._check_python())
        report.results.append(self._check_dependencies())
        report.results.append(self._check_filesystem())
        report.results.append(await self._check_internet())
        if self._database is not None:
            report.results.append(await self._check_database())
        if self._telegram is not None:
            report.results.append(await self._check_telegram())
        if self._browser_manager is not None:
            report.results.append(await self._check_browser())
        self._log_report("startup", report)
        return report

    async def run_runtime_checks(self) -> HealthReport:
        """Periodic subset while the scheduler runs."""
        report = HealthReport()
        report.results.append(await self._check_internet())
        if self._database is not None:
            report.results.append(await self._check_database())
        if self._telegram is not None:
            report.results.append(await self._check_telegram())
        self._log_report("runtime", report)
        return report

    # -- individual checks ---------------------------------------------------

    def _check_python(self) -> CheckResult:
        version = sys.version_info
        if (version.major, version.minor) >= _MIN_PYTHON:
            return CheckResult("python", HealthStatus.HEALTHY)
        return CheckResult(
            "python",
            HealthStatus.WARNING,
            f"requires {_MIN_PYTHON[0]}.{_MIN_PYTHON[1]}+, running {version.major}.{version.minor}",
        )

    def _check_dependencies(self) -> CheckResult:
        missing = []
        for module in _REQUIRED_MODULES:
            try:
                importlib.import_module(module)
            except ImportError:
                missing.append(module)
        if missing:
            return CheckResult(
                "dependencies", HealthStatus.CRITICAL, f"missing: {', '.join(missing)}"
            )
        return CheckResult("dependencies", HealthStatus.HEALTHY)

    def _check_filesystem(self) -> CheckResult:
        try:
            for name in _REQUIRED_DIRS:
                (self._base_dir / name).mkdir(parents=True, exist_ok=True)
            probe = self._base_dir / "logs" / ".health_probe"
            probe.write_text("ok", encoding="utf-8")
            probe.unlink()
            return CheckResult("filesystem", HealthStatus.HEALTHY)
        except OSError as exc:
            return CheckResult("filesystem", HealthStatus.CRITICAL, str(exc))

    async def _check_internet(self) -> CheckResult:
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(_INTERNET_TIMEOUT), transport=self._transport
        ) as client:
            for target in _INTERNET_TARGETS:
                try:
                    response = await client.get(target)
                    if response.status_code < 500:
                        return CheckResult("internet", HealthStatus.HEALTHY)
                except httpx.HTTPError:
                    continue
        return CheckResult("internet", HealthStatus.CRITICAL, "no target reachable")

    async def _check_database(self) -> CheckResult:
        if await self._database.health_check():  # type: ignore[union-attr]
            return CheckResult("database", HealthStatus.HEALTHY)
        return CheckResult("database", HealthStatus.CRITICAL, "read/write probe failed")

    async def _check_telegram(self) -> CheckResult:
        if await self._telegram.health_check():  # type: ignore[union-attr]
            return CheckResult("telegram", HealthStatus.HEALTHY)
        # WARNING per spec: scraping continues, notifications degraded
        return CheckResult("telegram", HealthStatus.WARNING, "getMe failed")

    async def _check_browser(self) -> CheckResult:
        manager = self._browser_manager
        try:
            started_here = False
            if not manager.started:  # type: ignore[union-attr]
                await manager.start()  # type: ignore[union-attr]
                started_here = True
            healthy = await manager.health_check()  # type: ignore[union-attr]
            if started_here:
                await manager.stop()  # type: ignore[union-attr]
            if healthy:
                return CheckResult("playwright", HealthStatus.HEALTHY)
            return CheckResult("playwright", HealthStatus.WARNING, "browser unhealthy")
        except Exception as exc:
            # WARNING per spec: LinkedIn scraper gets disabled, app continues
            return CheckResult("playwright", HealthStatus.WARNING, str(exc))

    # -- reporting -----------------------------------------------------------

    def _log_report(self, kind: str, report: HealthReport) -> None:
        for result in report.results:
            if result.status == HealthStatus.HEALTHY:
                logger.debug("Health [{}] {}: healthy", kind, result.name)
            elif result.status == HealthStatus.WARNING:
                logger.warning("Health [{}] {}: {}", kind, result.name, result.message)
            else:
                logger.error("Health [{}] {}: {}", kind, result.name, result.message)
        logger.info("Health [{}] overall: {}", kind, report.overall.value)
        if self._statistics is not None:
            self._statistics.increment("health.checks_run")
            if report.overall == HealthStatus.CRITICAL:
                self._statistics.increment("health.critical_failures")
            elif report.overall == HealthStatus.WARNING:
                self._statistics.increment("health.warnings")
