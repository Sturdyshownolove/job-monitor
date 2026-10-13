"""Filtering pipeline unit tests (Filtering Pipeline.md unit-test checklist).

Database and notifier are stubs here; full-stack flow lives in
test_pipeline_integration.py.
"""

from __future__ import annotations

import pytest

import httpx

from config import Config
from database import InsertResult
from language_check import LanguageChecker
from models import Job
from normalizer import Normalizer
from pipeline import FilteringPipeline, PipelineOutcome
from stats import StatisticsCollector


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


class StubDatabase:
    def __init__(self, result: InsertResult = InsertResult.INSERTED) -> None:
        self.result = result
        self.inserted: list[Job] = []
        self.raises: Exception | None = None

    async def insert_job(self, job: Job) -> InsertResult:
        if self.raises is not None:
            raise self.raises
        self.inserted.append(job)
        return self.result


class StubNotifier:
    def __init__(self, success: bool = True) -> None:
        self.success = success
        self.sent: list[Job] = []

    async def send(self, job: Job) -> bool:
        self.sent.append(job)
        return self.success


def make_pipeline(
    database: StubDatabase | None = None,
    notifier: StubNotifier | None = None,
    statistics: StatisticsCollector | None = None,
    config: Config | None = None,
    language_checker: LanguageChecker | None = None,
) -> tuple[FilteringPipeline, StubDatabase, StubNotifier]:
    database = database if database is not None else StubDatabase()
    notifier = notifier if notifier is not None else StubNotifier()
    pipeline = FilteringPipeline(
        config if config is not None else Config(telegram_token="t", chat_id="c"),
        Normalizer(),
        database,  # type: ignore[arg-type]
        notifier,
        statistics,
        language_checker,
    )
    return pipeline, database, notifier


def make_language_checker(detail_html: str = "") -> LanguageChecker:
    config = Config(telegram_token="t", chat_id="c")
    transport = httpx.MockTransport(
        lambda request: httpx.Response(200, text=detail_html)
    )
    return LanguageChecker(config, transport=transport)


class TestHappyPath:
    async def test_valid_job_inserted_and_notified(self) -> None:
        pipeline, database, notifier = make_pipeline()
        outcome = await pipeline.process_job(make_job())
        assert outcome == PipelineOutcome.INSERTED_AND_NOTIFIED
        assert len(database.inserted) == 1
        assert notifier.sent == database.inserted

    async def test_notification_only_after_insert(self) -> None:
        pipeline, database, notifier = make_pipeline(StubDatabase(InsertResult.DUPLICATE))
        outcome = await pipeline.process_job(make_job())
        assert outcome == PipelineOutcome.DUPLICATE
        assert notifier.sent == []


class TestNormalization:
    async def test_title_variant_normalized_before_hash(self) -> None:
        pipeline, database, _ = make_pipeline()
        await pipeline.process_job(make_job(title="Senior Product Designer (m/w/d)"))
        stored = database.inserted[0]
        assert stored.title == "Senior Product Designer"
        # Same hash as the clean variant — cross-scraper dedup works
        assert stored.hash == make_job().hash

    async def test_url_tracking_stripped_before_hash(self) -> None:
        pipeline, database, _ = make_pipeline()
        await pipeline.process_job(
            make_job(url="https://example.com/jobs/1?utm_source=feed")
        )
        assert database.inserted[0].url == "https://example.com/jobs/1"
        assert database.inserted[0].hash == make_job().hash

    async def test_company_and_location_normalized(self) -> None:
        pipeline, database, _ = make_pipeline()
        await pipeline.process_job(
            make_job(company="Miro,", location="Berlin, Germany")
        )
        stored = database.inserted[0]
        assert stored.company == "Miro"
        assert stored.location == "Berlin"


class TestKeywordFilter:
    @pytest.mark.parametrize(
        "title",
        [
            "Lead Product Designer",
            "Principal UX Designer",
            "Interaction Designer",
            "product designer",  # case-insensitive
        ],
    )
    async def test_accepted_titles(self, title: str) -> None:
        pipeline, database, _ = make_pipeline()
        outcome = await pipeline.process_job(make_job(title=title))
        assert outcome == PipelineOutcome.INSERTED_AND_NOTIFIED

    @pytest.mark.parametrize(
        "title", ["Graphic Designer", "Marketing Designer", "Frontend Developer"]
    )
    async def test_rejected_titles(self, title: str) -> None:
        pipeline, database, _ = make_pipeline()
        outcome = await pipeline.process_job(make_job(title=title))
        assert outcome == PipelineOutcome.REJECTED_TITLE
        assert database.inserted == []

    async def test_configurable_keywords(self) -> None:
        config = Config(telegram_token="t", chat_id="c", keywords="Motion Designer")
        pipeline, database, _ = make_pipeline(config=config)
        assert (
            await pipeline.process_job(make_job(title="Motion Designer"))
            == PipelineOutcome.INSERTED_AND_NOTIFIED
        )
        assert (
            await pipeline.process_job(make_job(2, title="Senior Product Designer"))
            == PipelineOutcome.REJECTED_TITLE
        )


class TestCountryFilter:
    @pytest.mark.parametrize(
        "location", ["Berlin", "Munich", "München", "Remote Germany", "Hamburg, Deutschland"]
    )
    async def test_germany_accepted(self, location: str) -> None:
        pipeline, _, _ = make_pipeline()
        outcome = await pipeline.process_job(make_job(location=location))
        assert outcome == PipelineOutcome.INSERTED_AND_NOTIFIED

    @pytest.mark.parametrize(
        "location",
        ["Remote Europe", "Europe – Remote", "Anywhere in the World", "EMEA Remote"],
    )
    async def test_germany_eligible_remote_regions_accepted(self, location: str) -> None:
        """Remote roles hirable from Germany (RemoteRocketship regions) pass."""
        pipeline, _, _ = make_pipeline()
        outcome = await pipeline.process_job(make_job(location=location))
        assert outcome == PipelineOutcome.INSERTED_AND_NOTIFIED

    @pytest.mark.parametrize("location", ["Paris", "London", "Austin, USA"])
    async def test_non_germany_rejected(self, location: str) -> None:
        pipeline, database, notifier = make_pipeline()
        outcome = await pipeline.process_job(make_job(location=location))
        assert outcome == PipelineOutcome.REJECTED_COUNTRY
        assert database.inserted == []
        assert notifier.sent == []


class TestLanguageFilter:
    async def test_german_only_job_stored_but_not_notified(self) -> None:
        pipeline, database, notifier = make_pipeline(
            language_checker=make_language_checker(
                "<html><body>Verhandlungssicheres Deutsch erforderlich</body></html>"
            )
        )
        outcome = await pipeline.process_job(make_job())
        assert outcome == PipelineOutcome.REJECTED_LANGUAGE
        assert len(database.inserted) == 1  # stored → never re-checked
        assert notifier.sent == []

    async def test_card_language_note_rejects_without_fetch(self) -> None:
        pipeline, database, notifier = make_pipeline(
            language_checker=make_language_checker("unreachable")
        )
        outcome = await pipeline.process_job(make_job(language_note="German Required"))
        assert outcome == PipelineOutcome.REJECTED_LANGUAGE
        assert notifier.sent == []

    async def test_english_accepted_job_notified(self) -> None:
        pipeline, _, notifier = make_pipeline(
            language_checker=make_language_checker(
                "<html><body>Fluent English required; German is a plus.</body></html>"
            )
        )
        outcome = await pipeline.process_job(make_job())
        assert outcome == PipelineOutcome.INSERTED_AND_NOTIFIED
        assert len(notifier.sent) == 1

    async def test_no_evidence_passes(self) -> None:
        pipeline, _, notifier = make_pipeline(
            language_checker=make_language_checker("<html><body>Great team</body></html>")
        )
        outcome = await pipeline.process_job(make_job())
        assert outcome == PipelineOutcome.INSERTED_AND_NOTIFIED
        assert len(notifier.sent) == 1

    async def test_duplicate_short_circuits_before_language_check(self) -> None:
        pipeline, _, notifier = make_pipeline(
            database=StubDatabase(InsertResult.DUPLICATE),
            language_checker=make_language_checker("irrelevant"),
        )
        outcome = await pipeline.process_job(make_job(language_note="German Required"))
        assert outcome == PipelineOutcome.DUPLICATE
        assert notifier.sent == []


class TestFailureRecovery:
    async def test_notify_failure_keeps_job_stored(self) -> None:
        pipeline, database, _ = make_pipeline(notifier=StubNotifier(success=False))
        outcome = await pipeline.process_job(make_job())
        assert outcome == PipelineOutcome.INSERTED_NOTIFY_FAILED
        assert len(database.inserted) == 1

    async def test_database_error_contained(self) -> None:
        database = StubDatabase()
        database.raises = RuntimeError("db exploded")
        pipeline, _, notifier = make_pipeline(database=database)
        outcome = await pipeline.process_job(make_job())
        assert outcome == PipelineOutcome.ERROR
        assert notifier.sent == []

    async def test_batch_survives_one_bad_job(self) -> None:
        pipeline, database, _ = make_pipeline()
        jobs = [
            make_job(1),
            make_job(2, title="Frontend Developer"),
            make_job(3),
        ]
        summary = await pipeline.process(jobs)
        assert summary.received == 3
        assert summary.notified == 2
        assert summary.outcomes[PipelineOutcome.REJECTED_TITLE] == 1


class TestStatistics:
    async def test_counters(self) -> None:
        statistics = StatisticsCollector()
        pipeline, _, _ = make_pipeline(statistics=statistics)
        await pipeline.process(
            [make_job(1), make_job(2, title="Barista"), make_job(3, location="Paris")]
        )
        counters = statistics.snapshot()["counters"]
        assert counters["pipeline.jobs_received"] == 3
        assert counters["pipeline.jobs_inserted"] == 1
        assert counters["pipeline.notifications_sent"] == 1
        assert counters["pipeline.rejected_title"] == 1
        assert counters["pipeline.rejected_country"] == 1

    async def test_summary_counts(self) -> None:
        pipeline, _, _ = make_pipeline(StubDatabase(InsertResult.DUPLICATE))
        summary = await pipeline.process([make_job(1), make_job(1)])
        assert summary.outcomes[PipelineOutcome.DUPLICATE] == 2
        assert summary.inserted == 0
