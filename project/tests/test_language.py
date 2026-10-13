"""Language filter tests: policy verdicts + checker fetch behavior."""

from __future__ import annotations

import httpx
import pytest

from config import Config
from language_check import LanguageChecker, LanguagePolicy, LanguageVerdict
from models import Job
from stats import StatisticsCollector


def make_job(language_note: str | None = None, url: str = "https://example.com/job/1") -> Job:
    return Job.create(
        title="Senior Product Designer",
        company="Acme",
        location="Berlin",
        url=url,
        source="Indeed",
        language_note=language_note,
    )


def make_checker(
    handler=None, enabled: bool = True
) -> tuple[LanguageChecker, StatisticsCollector]:
    config = Config(
        telegram_token="t",
        chat_id="c",
        language_filter=enabled,
        retry_jitter=False,
        rotate_user_agent=False,
    )
    statistics = StatisticsCollector()
    transport = httpx.MockTransport(handler) if handler is not None else None
    return LanguageChecker(config, statistics, transport=transport), statistics


class TestLanguagePolicy:
    @pytest.mark.parametrize(
        "text",
        [
            "Fluent German is required for this role.",
            "German required",
            "You are fluent in German.",
            "German at C1 level or above",
            "German: C2",
            "Sehr gute Deutschkenntnisse in Wort und Schrift",
            "Verhandlungssicheres Deutsch erforderlich",
            "Fließende Deutschkenntnisse werden vorausgesetzt",
            "Muttersprache Deutsch",
        ],
    )
    def test_german_required_without_english_rejected(self, text: str) -> None:
        assert LanguagePolicy().evaluate(text) == LanguageVerdict.GERMAN_ONLY

    @pytest.mark.parametrize(
        "text",
        [
            "German is a plus, the team works in English.",
            "German required is a plus",  # plus marker within window
            "Deutschkenntnisse von Vorteil",
            "Deutschkenntnisse wünschenswert, aber kein Muss",
            "German is nice-to-have",
            "Fluent German is an advantage",
        ],
    )
    def test_german_as_plus_passes(self, text: str) -> None:
        assert LanguagePolicy().evaluate(text) != LanguageVerdict.GERMAN_ONLY

    @pytest.mark.parametrize(
        "text",
        [
            "Fluent German and fluent English required.",
            "German C1, English C1",
            "Working language is English; sehr gute Deutschkenntnisse erforderlich.",
            "English-speaking team. Fluent in German required.",
        ],
    )
    def test_german_plus_english_passes(self, text: str) -> None:
        assert LanguagePolicy().evaluate(text) == LanguageVerdict.OK

    def test_english_only_ok(self) -> None:
        assert (
            LanguagePolicy().evaluate("Fluent English required.") == LanguageVerdict.OK
        )

    @pytest.mark.parametrize("text", [None, "", "We build great products."])
    def test_no_evidence_unknown(self, text: str | None) -> None:
        assert LanguagePolicy().evaluate(text) == LanguageVerdict.UNKNOWN

    def test_ukrainian_required_is_not_german(self) -> None:
        assert LanguagePolicy().evaluate("Ukrainian Required") == LanguageVerdict.UNKNOWN


class TestLanguageChecker:
    async def test_card_note_short_circuits_without_fetch(self) -> None:
        calls: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            calls.append(str(request.url))
            return httpx.Response(200, text="irrelevant")

        checker, _ = make_checker(handler)
        verdict = await checker.check(make_job(language_note="German Required"))
        assert verdict == LanguageVerdict.GERMAN_ONLY
        assert calls == []
        await checker.close()

    async def test_detail_page_fetched_when_no_note(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                text="<html><body><p>Verhandlungssicheres Deutsch erforderlich</p></body></html>",
            )

        checker, _ = make_checker(handler)
        assert await checker.check(make_job()) == LanguageVerdict.GERMAN_ONLY
        await checker.close()

    async def test_markers_inside_scripts_ignored(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                text="<html><script>var x='German required';</script><body>Great job</body></html>",
            )

        checker, _ = make_checker(handler)
        assert await checker.check(make_job()) == LanguageVerdict.UNKNOWN
        await checker.close()

    async def test_fetch_failure_yields_unknown(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(403, text="Security Check")

        checker, _ = make_checker(handler)
        assert await checker.check(make_job()) == LanguageVerdict.UNKNOWN
        await checker.close()

    async def test_network_error_yields_unknown(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("boom")

        checker, _ = make_checker(handler)
        assert await checker.check(make_job()) == LanguageVerdict.UNKNOWN
        await checker.close()

    async def test_disabled_checker_always_unknown(self) -> None:
        checker, _ = make_checker(enabled=False)
        assert not checker.enabled
        assert (
            await checker.check(make_job(language_note="German Required"))
            == LanguageVerdict.UNKNOWN
        )
        await checker.close()
