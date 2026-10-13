"""Health check framework tests (Health Checks.md unit-test checklist)."""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

import httpx
import pytest

import health as health_module
from config import Config
from database import Database
from health import CheckResult, HealthChecker, HealthReport, HealthStatus
from retry import RetryPolicy
from stats import StatisticsCollector
from telegram import TelegramService

internet_ok = httpx.MockTransport(lambda request: httpx.Response(200, text="ok"))


def internet_down_handler(request: httpx.Request) -> httpx.Response:
    raise httpx.ConnectError("no route")


class FakeBrowserManager:
    def __init__(self, healthy: bool = True, launch_fails: bool = False) -> None:
        self.healthy = healthy
        self.launch_fails = launch_fails
        self.started = False
        self.stopped = False

    async def start(self) -> None:
        if self.launch_fails:
            raise RuntimeError("chromium missing")
        self.started = True

    async def stop(self) -> None:
        self.started = False
        self.stopped = True

    async def health_check(self) -> bool:
        return self.healthy


class TestReport:
    def test_overall_critical_dominates(self) -> None:
        report = HealthReport(
            [
                CheckResult("a", HealthStatus.HEALTHY),
                CheckResult("b", HealthStatus.WARNING),
                CheckResult("c", HealthStatus.CRITICAL, "boom"),
            ]
        )
        assert report.overall == HealthStatus.CRITICAL
        assert report.ok is False

    def test_warning_does_not_block_startup(self) -> None:
        report = HealthReport(
            [CheckResult("a", HealthStatus.HEALTHY), CheckResult("b", HealthStatus.WARNING)]
        )
        assert report.overall == HealthStatus.WARNING
        assert report.ok is True

    def test_summary_lines(self) -> None:
        report = HealthReport([CheckResult("db", HealthStatus.CRITICAL, "probe failed")])
        summary = report.summary()
        assert "db: critical (probe failed)" in summary
        assert "overall: critical" in summary


class TestEnvironmentChecks:
    async def test_python_and_dependencies_healthy(
        self, config: Config, tmp_path: Path
    ) -> None:
        checker = HealthChecker(config, transport=internet_ok, base_dir=tmp_path)
        report = await checker.run_startup_checks()
        by_name = {result.name: result for result in report.results}
        assert by_name["python"].status == HealthStatus.HEALTHY
        assert by_name["dependencies"].status == HealthStatus.HEALTHY
        assert by_name["filesystem"].status == HealthStatus.HEALTHY
        assert report.ok

    async def test_missing_dependency_critical(
        self, config: Config, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            health_module, "_REQUIRED_MODULES", ("httpx", "definitely_not_installed_xyz")
        )
        checker = HealthChecker(config, transport=internet_ok, base_dir=tmp_path)
        report = await checker.run_startup_checks()
        by_name = {result.name: result for result in report.results}
        assert by_name["dependencies"].status == HealthStatus.CRITICAL
        assert "definitely_not_installed_xyz" in by_name["dependencies"].message
        assert not report.ok

    async def test_filesystem_creates_directories(
        self, config: Config, tmp_path: Path
    ) -> None:
        checker = HealthChecker(config, transport=internet_ok, base_dir=tmp_path)
        await checker.run_startup_checks()
        assert (tmp_path / "logs").is_dir()
        assert (tmp_path / "database").is_dir()
        assert not (tmp_path / "logs" / ".health_probe").exists()

    async def test_filesystem_blocked_is_critical(
        self, config: Config, tmp_path: Path
    ) -> None:
        (tmp_path / "logs").write_text("a file, not a directory", encoding="utf-8")
        checker = HealthChecker(config, transport=internet_ok, base_dir=tmp_path)
        report = await checker.run_startup_checks()
        by_name = {result.name: result for result in report.results}
        assert by_name["filesystem"].status == HealthStatus.CRITICAL


class TestInternetCheck:
    async def test_reachable(self, config: Config, tmp_path: Path) -> None:
        checker = HealthChecker(config, transport=internet_ok, base_dir=tmp_path)
        report = await checker.run_startup_checks()
        assert {r.name: r for r in report.results}["internet"].status == HealthStatus.HEALTHY

    async def test_fallback_target_used(self, config: Config, tmp_path: Path) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            if "google" in request.url.host:
                raise httpx.ConnectError("blocked")
            return httpx.Response(200, text="ok")

        checker = HealthChecker(
            config, transport=httpx.MockTransport(handler), base_dir=tmp_path
        )
        report = await checker.run_startup_checks()
        assert {r.name: r for r in report.results}["internet"].status == HealthStatus.HEALTHY

    async def test_all_targets_down_critical(self, config: Config, tmp_path: Path) -> None:
        checker = HealthChecker(
            config, transport=httpx.MockTransport(internet_down_handler), base_dir=tmp_path
        )
        report = await checker.run_startup_checks()
        assert {r.name: r for r in report.results}["internet"].status == HealthStatus.CRITICAL
        assert not report.ok


@pytest.fixture
async def live_database(tmp_path: Path) -> AsyncIterator[tuple[Database, Config]]:
    config = Config(
        telegram_token="t",
        chat_id="c",
        database_url=f"sqlite+aiosqlite:///{(tmp_path / 'jobs.db').as_posix()}",
    )
    db = Database(config)
    await db.start()
    yield db, config
    await db.close()


class TestDatabaseCheck:
    async def test_healthy(self, live_database, tmp_path: Path) -> None:
        db, config = live_database
        checker = HealthChecker(config, database=db, transport=internet_ok, base_dir=tmp_path)
        report = await checker.run_startup_checks()
        assert {r.name: r for r in report.results}["database"].status == HealthStatus.HEALTHY

    async def test_probe_leaves_no_rows(self, live_database) -> None:
        db, _ = live_database
        assert await db.health_check() is True
        stats = await db.get_statistics()
        assert stats["total_jobs"] == 0
        assert not await db.job_exists("0" * 64)

    async def test_closed_database_critical(self, live_database, tmp_path: Path) -> None:
        db, config = live_database
        await db.close()
        checker = HealthChecker(config, database=db, transport=internet_ok, base_dir=tmp_path)
        report = await checker.run_startup_checks()
        assert {r.name: r for r in report.results}["database"].status == HealthStatus.CRITICAL
        assert not report.ok


class TestTelegramCheck:
    def make_telegram(self, config: Config, ok: bool) -> TelegramService:
        async def instant_sleep(seconds: float) -> None:
            return None

        def handler(request: httpx.Request) -> httpx.Response:
            if ok:
                return httpx.Response(200, json={"ok": True, "result": {}})
            return httpx.Response(401, json={"ok": False})

        return TelegramService(
            config,
            RetryPolicy(config, sleep=instant_sleep),
            transport=httpx.MockTransport(handler),
            sleep=instant_sleep,
        )

    async def test_healthy(self, config: Config, tmp_path: Path) -> None:
        telegram = self.make_telegram(config, ok=True)
        checker = HealthChecker(
            config, telegram=telegram, transport=internet_ok, base_dir=tmp_path
        )
        report = await checker.run_startup_checks()
        assert {r.name: r for r in report.results}["telegram"].status == HealthStatus.HEALTHY
        await telegram.close()

    async def test_failure_is_warning_not_critical(
        self, config: Config, tmp_path: Path
    ) -> None:
        telegram = self.make_telegram(config, ok=False)
        checker = HealthChecker(
            config, telegram=telegram, transport=internet_ok, base_dir=tmp_path
        )
        report = await checker.run_startup_checks()
        assert {r.name: r for r in report.results}["telegram"].status == HealthStatus.WARNING
        assert report.ok  # app still starts
        await telegram.close()


class TestBrowserCheck:
    async def test_healthy_browser(self, config: Config, tmp_path: Path) -> None:
        manager = FakeBrowserManager(healthy=True)
        checker = HealthChecker(
            config, browser_manager=manager, transport=internet_ok, base_dir=tmp_path
        )
        report = await checker.run_startup_checks()
        assert {r.name: r for r in report.results}["playwright"].status == HealthStatus.HEALTHY
        assert manager.stopped  # temporary browser cleaned up

    async def test_launch_failure_is_warning(self, config: Config, tmp_path: Path) -> None:
        manager = FakeBrowserManager(launch_fails=True)
        checker = HealthChecker(
            config, browser_manager=manager, transport=internet_ok, base_dir=tmp_path
        )
        report = await checker.run_startup_checks()
        result = {r.name: r for r in report.results}["playwright"]
        assert result.status == HealthStatus.WARNING
        assert report.ok  # LinkedIn disabled, app continues


class TestRuntimeChecks:
    async def test_runtime_subset(self, live_database, tmp_path: Path) -> None:
        db, config = live_database
        checker = HealthChecker(config, database=db, transport=internet_ok, base_dir=tmp_path)
        report = await checker.run_runtime_checks()
        names = [result.name for result in report.results]
        assert names == ["internet", "database"]
        assert report.ok

    async def test_statistics_recorded(self, config: Config, tmp_path: Path) -> None:
        statistics = StatisticsCollector()
        checker = HealthChecker(
            config,
            statistics=statistics,
            transport=httpx.MockTransport(internet_down_handler),
            base_dir=tmp_path,
        )
        await checker.run_runtime_checks()
        counters = statistics.snapshot()["counters"]
        assert counters["health.checks_run"] == 1
        assert counters["health.critical_failures"] == 1
