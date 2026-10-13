"""Telegram service tests (Telegram.md unit-test checklist). Mocked API only."""

from __future__ import annotations

import json
from collections.abc import Callable

import httpx
import pytest

from config import Config
from logger import logger
from models import Job
from retry import RetryPolicy
from stats import StatisticsCollector
from telegram import DEFAULT_TEMPLATE, TelegramService, escape_markdown

TOKEN = "123456:secret-bot-token"


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


def make_service(
    handler: Callable[[httpx.Request], httpx.Response],
    statistics: StatisticsCollector | None = None,
    send_interval: float = 0.0,
    template: str = DEFAULT_TEMPLATE,
) -> tuple[TelegramService, Config]:
    config = Config(telegram_token=TOKEN, chat_id="42", retry_jitter=False)

    async def instant_sleep(seconds: float) -> None:
        return None

    service = TelegramService(
        config,
        RetryPolicy(config, statistics, sleep=instant_sleep),
        statistics,
        template=template,
        transport=httpx.MockTransport(handler),
        send_interval=send_interval,
        sleep=instant_sleep,
    )
    return service, config


def ok_response(request: httpx.Request) -> httpx.Response:
    return httpx.Response(200, json={"ok": True, "result": {}})


class TestEscaping:
    def test_special_characters_escaped(self) -> None:
        assert escape_markdown("a_b*c[d]e(f)g.h!i-j") == r"a\_b\*c\[d\]e\(f\)g\.h\!i\-j"

    def test_plain_text_untouched(self) -> None:
        assert escape_markdown("Senior Product Designer") == "Senior Product Designer"


class TestMessageBuilder:
    def test_default_template_fields(self) -> None:
        service, _ = make_service(ok_response)
        message = service.build_message(make_job())
        assert "Senior Product Designer" in message
        assert "Miro" in message
        assert "Berlin" in message
        assert "LinkedIn" in message
        assert "2 hours ago" in message
        assert r"https://example\.com/jobs/1" in message

    def test_values_markdown_escaped(self) -> None:
        service, _ = make_service(ok_response)
        message = service.build_message(make_job(title="C++ & UX_Designer!"))
        assert r"C\+\+ & UX\_Designer\!" in message

    def test_missing_posted_time(self) -> None:
        service, _ = make_service(ok_response)
        assert "Unknown" in service.build_message(make_job(posted_time=None))

    def test_custom_template(self) -> None:
        service, _ = make_service(ok_response, template="{title} at {company}")
        assert service.build_message(make_job()) == "Senior Product Designer at Miro"


class TestSend:
    async def test_success(self, statistics: StatisticsCollector) -> None:
        captured: dict[str, object] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured["path"] = request.url.path
            captured["body"] = json.loads(request.content)
            return httpx.Response(200, json={"ok": True})

        service, _ = make_service(handler, statistics)
        async with service:
            assert await service.send(make_job()) is True
        assert captured["path"].endswith("/sendMessage")
        body = captured["body"]
        assert body["chat_id"] == "42"
        assert body["parse_mode"] == "MarkdownV2"
        assert "Miro" in body["text"]
        assert statistics.snapshot()["counters"]["telegram.messages_sent"] == 1

    async def test_transient_500_retried_then_succeeds(self) -> None:
        calls = {"n": 0}

        def handler(request: httpx.Request) -> httpx.Response:
            calls["n"] += 1
            if calls["n"] < 3:
                return httpx.Response(500)
            return httpx.Response(200, json={"ok": True})

        service, _ = make_service(handler)
        async with service:
            assert await service.send(make_job()) is True
        assert calls["n"] == 3

    async def test_429_retried(self) -> None:
        calls = {"n": 0}

        def handler(request: httpx.Request) -> httpx.Response:
            calls["n"] += 1
            if calls["n"] == 1:
                return httpx.Response(429)
            return httpx.Response(200, json={"ok": True})

        service, _ = make_service(handler)
        async with service:
            assert await service.send(make_job()) is True
        assert calls["n"] == 2

    async def test_invalid_token_not_retried(self, statistics: StatisticsCollector) -> None:
        calls = {"n": 0}

        def handler(request: httpx.Request) -> httpx.Response:
            calls["n"] += 1
            return httpx.Response(401, json={"ok": False, "description": "Unauthorized"})

        service, _ = make_service(handler, statistics)
        async with service:
            assert await service.send(make_job()) is False
        assert calls["n"] == 1
        assert statistics.snapshot()["counters"]["telegram.messages_failed"] == 1

    async def test_ok_false_payload_fails_without_retry(self) -> None:
        calls = {"n": 0}

        def handler(request: httpx.Request) -> httpx.Response:
            calls["n"] += 1
            return httpx.Response(200, json={"ok": False, "description": "chat not found"})

        service, _ = make_service(handler)
        async with service:
            assert await service.send(make_job()) is False
        assert calls["n"] == 1

    async def test_network_error_retried_then_fails_gracefully(self) -> None:
        calls = {"n": 0}

        def handler(request: httpx.Request) -> httpx.Response:
            calls["n"] += 1
            raise httpx.ConnectError("refused")

        service, _ = make_service(handler)
        async with service:
            assert await service.send(make_job()) is False
        assert calls["n"] == 4  # initial + 3 retries

    async def test_token_never_leaks_into_logs(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError(f"cannot reach {request.url}")  # URL contains token

        records: list[str] = []
        sink_id = logger.add(lambda message: records.append(str(message)), level="DEBUG")
        try:
            service, _ = make_service(handler)
            async with service:
                assert await service.send(make_job()) is False
        finally:
            logger.remove(sink_id)
        assert records  # something was logged
        assert all(TOKEN not in record for record in records)


class TestSendMany:
    async def test_counts_successes_and_continues_after_failure(self) -> None:
        calls = {"n": 0}

        def handler(request: httpx.Request) -> httpx.Response:
            calls["n"] += 1
            if calls["n"] == 2:
                return httpx.Response(400, json={"ok": False, "description": "bad"})
            return httpx.Response(200, json={"ok": True})

        service, _ = make_service(handler)
        async with service:
            sent = await service.send_many([make_job(1), make_job(2), make_job(3)])
        assert sent == 2

    async def test_throttles_between_messages(self) -> None:
        sleeps: list[float] = []

        async def recording_sleep(seconds: float) -> None:
            sleeps.append(seconds)

        config = Config(telegram_token=TOKEN, chat_id="42", retry_jitter=False)
        service = TelegramService(
            config,
            RetryPolicy(config, sleep=recording_sleep),
            transport=httpx.MockTransport(ok_response),
            send_interval=1.0,
            sleep=recording_sleep,
            clock=lambda: 0.0,  # frozen clock → full interval waited each time
        )
        async with service:
            await service.send_many([make_job(1), make_job(2), make_job(3)])
        assert sleeps.count(1.0) == 2  # between messages, not before the first

    async def test_send_self_throttles_even_without_send_many(self) -> None:
        sleeps: list[float] = []

        async def recording_sleep(seconds: float) -> None:
            sleeps.append(seconds)

        config = Config(telegram_token=TOKEN, chat_id="42", retry_jitter=False)
        service = TelegramService(
            config,
            RetryPolicy(config, sleep=recording_sleep),
            transport=httpx.MockTransport(ok_response),
            send_interval=1.0,
            sleep=recording_sleep,
            clock=lambda: 0.0,
        )
        async with service:
            await service.send(make_job(1))
            await service.send(make_job(2))  # direct calls, e.g. from the pipeline
        assert sleeps == [1.0]


class TestHealthCheck:
    async def test_healthy(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            assert request.url.path.endswith("/getMe")
            return httpx.Response(200, json={"ok": True, "result": {"username": "bot"}})

        service, _ = make_service(handler)
        async with service:
            assert await service.health_check() is True

    async def test_unhealthy(self) -> None:
        service, _ = make_service(
            lambda request: httpx.Response(401, json={"ok": False})
        )
        async with service:
            assert await service.health_check() is False
