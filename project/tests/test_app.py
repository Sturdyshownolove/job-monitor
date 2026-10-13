"""CLI + application assembly tests (M7).

The full assembly path (create_services → run_application) runs offline:
Indeed/Telegram/internet on MockTransports, browser via fakes, database on
tmp SQLite. CLI commands are exercised with Typer's CliRunner.
"""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest
from typer.testing import CliRunner

from app import StartupError, cli, create_services, run_application, shutdown_services
from config import Config
from tests.test_indeed import card, make_handler, page
from tests.test_linkedin import FakeBrowserManager as LinkedInFakeBrowser
from tests.test_linkedin import card as li_card
from tests.test_linkedin import page_html as li_page

runner = CliRunner()

K = "Product Designer"

internet_ok = httpx.MockTransport(lambda request: httpx.Response(200, text="ok"))


def internet_down(request: httpx.Request) -> httpx.Response:
    raise httpx.ConnectError("no route")


class HealthyFakeBrowser(LinkedInFakeBrowser):
    """LinkedIn fake browser that also satisfies the health check.

    RemoteRocketship navigations (jobTitle=... URLs) get an empty page so the
    scraper terminates immediately; LinkedIn URLs route as before.
    """

    async def health_check(self) -> bool:
        return True

    async def navigate(self, page, url: str) -> None:  # type: ignore[override]
        if "jobTitle=" in url:
            page.load("<html><body></body></html>")
            return
        await super().navigate(page, url)


class UnhealthyFakeBrowser(LinkedInFakeBrowser):
    def __init__(self) -> None:
        super().__init__({})

    async def health_check(self) -> bool:
        return False


def make_config(tmp_path: Path, **overrides: object) -> Config:
    defaults: dict[str, object] = {
        "telegram_token": "123:tok",
        "chat_id": "42",
        "keywords": K,
        "retry_jitter": False,
        "rotate_user_agent": False,
        "rate_min_delay": 0,
        "rate_max_delay": 0.001,
        "database_url": f"sqlite+aiosqlite:///{(tmp_path / 'jobs.db').as_posix()}",
        # Assembly tests must stay offline; the language filter has its own
        # unit tests with a mocked transport.
        "language_filter": False,
    }
    defaults.update(overrides)
    return Config(**defaults)  # type: ignore[arg-type]


# StepStone is behind a bot wall in production; tests mirror that state.
stepstone_blocked = httpx.MockTransport(lambda request: httpx.Response(403))


def telegram_recorder() -> tuple[httpx.MockTransport, list[dict[str, str]]]:
    messages: list[dict[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            messages.append(json.loads(request.content))
        return httpx.Response(200, json={"ok": True, "result": {}})

    return httpx.MockTransport(handler), messages


class TestCli:
    def test_version(self) -> None:
        result = runner.invoke(cli, ["version"])
        assert result.exit_code == 0
        assert "1.0.0" in result.output

    def test_check_valid_env(self, tmp_path: Path) -> None:
        env = tmp_path / "test.env"
        env.write_text("TELEGRAM_TOKEN=123:tok\nCHAT_ID=42\n", encoding="utf-8")
        result = runner.invoke(cli, ["check", "--env-file", str(env)])
        assert result.exit_code == 0
        assert "OK" in result.output
        assert "123:tok" not in result.output  # secrets masked

    def test_check_missing_config_fails(self, tmp_path: Path, monkeypatch) -> None:
        monkeypatch.delenv("TELEGRAM_TOKEN", raising=False)
        monkeypatch.delenv("CHAT_ID", raising=False)
        env = tmp_path / "empty.env"
        env.write_text("", encoding="utf-8")
        result = runner.invoke(cli, ["check", "--env-file", str(env)])
        assert result.exit_code == 1

    def test_run_command_registered(self) -> None:
        result = runner.invoke(cli, ["--help"])
        assert result.exit_code == 0
        for command in ("run", "health", "stats", "check", "version"):
            assert command in result.output


class TestAssembly:
    async def test_create_services_wires_everything(self, tmp_path: Path) -> None:
        config = make_config(tmp_path)
        services = await create_services(
            config,
            telegram_transport=telegram_recorder()[0],
            internet_transport=internet_ok,
            browser_manager=UnhealthyFakeBrowser(),  # type: ignore[arg-type]
        )
        try:
            assert services.database is not None
            assert services.scheduler.scrapers == ()  # registration happens in run
            assert services.indeed_scraper.name == "Indeed"
            assert services.linkedin_scraper.name == "LinkedIn"
        finally:
            await shutdown_services(services)


class TestRunApplication:
    async def test_once_end_to_end_linkedin_disabled(self, tmp_path: Path) -> None:
        config = make_config(tmp_path)
        telegram_transport, messages = telegram_recorder()
        services = await create_services(
            config,
            indeed_transport=httpx.MockTransport(
                make_handler({(K, 0): page(card(1), card(2, location="Paris"))})
            ),
            stepstone_transport=stepstone_blocked,
            telegram_transport=telegram_transport,
            internet_transport=internet_ok,
            browser_manager=UnhealthyFakeBrowser(),  # type: ignore[arg-type]
        )
        try:
            await run_application(services, once=True)
            # Playwright unhealthy → browser-only scrapers not registered
            assert [s.name for s in services.scheduler.scrapers] == ["Indeed", "StepStone"]
            db_stats = await services.database.get_statistics()
            assert db_stats["total_jobs"] == 1  # Paris job filtered by pipeline
            assert len(messages) == 1
            assert "Miro" in messages[0]["text"]
        finally:
            await shutdown_services(services)

    async def test_once_with_both_scrapers(self, tmp_path: Path) -> None:
        config = make_config(tmp_path, keywords="Product Designer,UX Designer")
        telegram_transport, messages = telegram_recorder()
        linkedin_browser = HealthyFakeBrowser(
            {(K, 0): li_page(li_card(1, title="UX Designer", company="SAP SE"))}
        )
        services = await create_services(
            config,
            indeed_transport=httpx.MockTransport(make_handler({(K, 0): page(card(1))})),
            stepstone_transport=stepstone_blocked,
            telegram_transport=telegram_transport,
            internet_transport=internet_ok,
            browser_manager=linkedin_browser,  # type: ignore[arg-type]
        )
        try:
            await run_application(services, once=True)
            assert [s.name for s in services.scheduler.scrapers] == [
                "Indeed",
                "StepStone",
                "LinkedIn",
                "RemoteRocketShip",
            ]
            db_stats = await services.database.get_statistics()
            assert db_stats["total_jobs"] == 2
            assert db_stats["jobs_per_source"] == {"Indeed": 1, "LinkedIn": 1}
            assert len(messages) == 2
        finally:
            await shutdown_services(services)

    async def test_critical_health_blocks_startup(self, tmp_path: Path) -> None:
        config = make_config(tmp_path)
        services = await create_services(
            config,
            telegram_transport=telegram_recorder()[0],
            internet_transport=httpx.MockTransport(internet_down),
            browser_manager=UnhealthyFakeBrowser(),  # type: ignore[arg-type]
        )
        try:
            with pytest.raises(StartupError, match="internet"):
                await run_application(services, once=True)
            assert services.scheduler.scrapers == ()  # nothing registered
        finally:
            await shutdown_services(services)

    async def test_shutdown_after_partial_startup_is_safe(self, tmp_path: Path) -> None:
        config = make_config(tmp_path)
        services = await create_services(
            config,
            telegram_transport=telegram_recorder()[0],
            internet_transport=internet_ok,
            browser_manager=UnhealthyFakeBrowser(),  # type: ignore[arg-type]
        )
        await shutdown_services(services)
        await shutdown_services(services)  # idempotent
