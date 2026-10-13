"""Application entry point (Architecture.md, Project Structure.md).

Responsibilities: load configuration, initialize logger and services, run
startup health checks, register scrapers, start the scheduler. No business
logic, SQL, HTML parsing or Telegram logic lives here.

Startup sequence (Architecture.md scheduler lifecycle):
Config → Logger → Database → Telegram → Browser → Health Checks → Scrapers →
Scheduler → run forever. A CRITICAL health result blocks startup; a Playwright
WARNING disables the LinkedIn scraper and the application continues.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from pathlib import Path

import httpx
import typer

from browser import BrowserManager
from config import Config, ConfigurationError, load_config
from database import Database
from health import HealthChecker, HealthStatus
from html_parser import HTMLParser
from language_check import LanguageChecker
from logger import logger, setup_logging
from normalizer import Normalizer
from pipeline import FilteringPipeline
from rate_limiter import RateLimiter
from retry import RetryPolicy
from scheduler import Scheduler
from scrapers import (
    IndeedScraper,
    LinkedInScraper,
    RemoteRocketshipScraper,
    StepStoneScraper,
)
from session_manager import SessionManager
from stats import StatisticsCollector
from telegram import TelegramService
from user_agent import UserAgentProvider

APP_VERSION = "1.0.0"

cli = typer.Typer(
    name="job-monitor",
    help="Germany Product Designer Job Monitor",
    add_completion=False,
    no_args_is_help=True,
)


class StartupError(Exception):
    """A CRITICAL health check blocked startup."""


@dataclass
class Services:
    """Fully wired application services."""

    config: Config
    statistics: StatisticsCollector
    database: Database
    telegram: TelegramService
    browser_manager: BrowserManager
    health_checker: HealthChecker
    scheduler: Scheduler
    indeed_scraper: IndeedScraper
    linkedin_scraper: LinkedInScraper
    stepstone_scraper: StepStoneScraper
    remoterocketship_scraper: RemoteRocketshipScraper
    language_checker: LanguageChecker


async def create_services(
    config: Config,
    *,
    indeed_transport: httpx.AsyncBaseTransport | None = None,
    stepstone_transport: httpx.AsyncBaseTransport | None = None,
    telegram_transport: httpx.AsyncBaseTransport | None = None,
    internet_transport: httpx.AsyncBaseTransport | None = None,
    language_transport: httpx.AsyncBaseTransport | None = None,
    browser_manager: BrowserManager | None = None,
) -> Services:
    """Compose every service with shared framework components.

    Transport/browser overrides exist for tests; production passes none.
    """
    statistics = StatisticsCollector()
    retry_policy = RetryPolicy(config, statistics)
    database = Database(config, statistics, retry_policy)
    await database.start()
    telegram = TelegramService(
        config, retry_policy, statistics, transport=telegram_transport
    )
    user_agent_provider = UserAgentProvider(config)
    browser = (
        browser_manager
        if browser_manager is not None
        else BrowserManager(config, user_agent_provider, statistics)
    )
    health_checker = HealthChecker(
        config,
        database=database,
        telegram=telegram,
        browser_manager=browser,
        statistics=statistics,
        transport=internet_transport,
    )

    rate_limiter = RateLimiter(config, statistics)
    session_manager = SessionManager(config, statistics)
    html_parser = HTMLParser()
    normalizer = Normalizer()

    language_checker = LanguageChecker(config, statistics, transport=language_transport)
    pipeline = FilteringPipeline(
        config, normalizer, database, telegram, statistics, language_checker
    )
    scheduler = Scheduler(config, pipeline, statistics, health_checker)

    indeed = IndeedScraper(
        config,
        statistics,
        session_manager,
        rate_limiter,
        retry_policy,
        user_agent_provider,
        html_parser,
        normalizer,
        browser_manager=browser,
        transport=indeed_transport,
    )
    linkedin = LinkedInScraper(
        config, statistics, browser, rate_limiter, retry_policy, html_parser, normalizer
    )
    stepstone = StepStoneScraper(
        config,
        statistics,
        session_manager,
        rate_limiter,
        retry_policy,
        user_agent_provider,
        html_parser,
        normalizer,
        browser_manager=browser,
        transport=stepstone_transport,
    )
    remoterocketship = RemoteRocketshipScraper(
        config, statistics, browser, rate_limiter, retry_policy, html_parser, normalizer
    )
    return Services(
        config=config,
        statistics=statistics,
        database=database,
        telegram=telegram,
        browser_manager=browser,
        health_checker=health_checker,
        scheduler=scheduler,
        indeed_scraper=indeed,
        linkedin_scraper=linkedin,
        stepstone_scraper=stepstone,
        remoterocketship_scraper=remoterocketship,
        language_checker=language_checker,
    )


async def run_application(services: Services, *, once: bool = False) -> None:
    """Health gate → scraper registration → scheduler."""
    report = await services.health_checker.run_startup_checks()
    if not report.ok:
        raise StartupError(report.summary())

    services.scheduler.register_scraper(services.indeed_scraper)
    services.scheduler.register_scraper(services.stepstone_scraper)
    playwright_healthy = any(
        result.name == "playwright" and result.status == HealthStatus.HEALTHY
        for result in report.results
    )
    if playwright_healthy:
        services.scheduler.register_scraper(services.linkedin_scraper)
        services.scheduler.register_scraper(services.remoterocketship_scraper)
    else:
        logger.warning(
            "LinkedIn and RemoteRocketship scrapers disabled — "
            "Playwright health check did not pass"
        )

    if once:
        await services.scheduler.run_cycle()
    else:
        await services.scheduler.run_forever()


async def shutdown_services(services: Services) -> None:
    """Release every resource; safe to call after partial startup."""
    logger.info("Shutting down\n{}", services.statistics.summary())
    await services.telegram.close()
    await services.language_checker.close()
    await services.database.close()
    if services.browser_manager.started:
        await services.browser_manager.stop()


async def _run_async(config: Config, once: bool) -> None:
    services = await create_services(config)
    try:
        await run_application(services, once=once)
    finally:
        await shutdown_services(services)


# -- CLI commands ------------------------------------------------------------


def _mask(secret: str) -> str:
    if len(secret) <= 4:
        return "****"
    return f"{secret[:2]}…{secret[-2:]}"


def _startup(env_file: Path | None) -> Config:
    """Shared startup: config first, then logging. Aborts on invalid config."""
    try:
        config = load_config(env_file)
    except ConfigurationError as exc:
        typer.secho(str(exc), fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1) from None
    setup_logging(config.log_level)
    return config


_ENV_OPTION = typer.Option(None, "--env-file", help="Path to .env file (defaults to ./.env)")


@cli.command()
def run(
    env_file: Path | None = _ENV_OPTION,
    once: bool = typer.Option(False, "--once", help="Run one monitoring cycle and exit"),
) -> None:
    """Start the job monitor (health checks, scrapers, scheduler)."""
    config = _startup(env_file)
    try:
        asyncio.run(_run_async(config, once))
    except StartupError as exc:
        typer.secho(f"Startup blocked by health checks:\n{exc}", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1) from None
    except KeyboardInterrupt:
        logger.info("Interrupted by user — shutdown complete")


@cli.command()
def health(env_file: Path | None = _ENV_OPTION) -> None:
    """Run startup health checks and print the report."""
    config = _startup(env_file)

    async def _health() -> object:
        services = await create_services(config)
        try:
            return await services.health_checker.run_startup_checks()
        finally:
            await shutdown_services(services)

    report = asyncio.run(_health())
    typer.echo(report.summary())
    raise typer.Exit(code=0 if report.ok else 1)


@cli.command()
def stats(env_file: Path | None = _ENV_OPTION) -> None:
    """Show database statistics and recent jobs."""
    config = _startup(env_file)

    async def _stats() -> tuple[dict[str, object], list[object]]:
        database = Database(config)
        await database.start()
        try:
            return await database.get_statistics(), await database.get_recent_jobs(10)
        finally:
            await database.close()

    db_stats, recent = asyncio.run(_stats())
    typer.echo(f"total jobs        {db_stats['total_jobs']}")
    typer.echo(f"jobs today        {db_stats['jobs_today']}")
    typer.echo(f"jobs this week    {db_stats['jobs_this_week']}")
    typer.echo(f"avg jobs per day  {db_stats['average_jobs_per_day']}")
    for source, count in sorted(db_stats["jobs_per_source"].items()):  # type: ignore[union-attr]
        typer.echo(f"  {source}: {count}")
    if recent:
        typer.echo("recent:")
        for job in recent:
            typer.echo(f"  {job.title} @ {job.company} ({job.source})")


@cli.command()
def check(env_file: Path | None = _ENV_OPTION) -> None:
    """Validate configuration and report the effective settings."""
    config = _startup(env_file)
    logger.info("Configuration valid")
    typer.echo(f"telegram_token   {_mask(config.telegram_token)}")
    typer.echo(f"chat_id          {_mask(config.chat_id)}")
    typer.echo(f"database_url     {config.database_url}")
    typer.echo(f"check_interval   {config.check_interval}s")
    typer.echo(f"headless         {config.headless}")
    typer.echo(f"log_level        {config.log_level}")
    typer.echo(f"request_timeout  {config.request_timeout}s")
    typer.echo(f"max_retries      {config.max_retries}")
    typer.echo(f"max_pages        {config.max_pages}")
    typer.echo(f"rate_delay       {config.rate_min_delay}–{config.rate_max_delay}s")
    typer.echo(f"keywords         {len(config.keywords)} configured")
    typer.secho("OK", fg=typer.colors.GREEN)


@cli.command()
def version() -> None:
    """Print application version."""
    typer.echo(APP_VERSION)


def main() -> None:
    cli()


if __name__ == "__main__":
    main()
