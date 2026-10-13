"""Session manager tests (SessionManager.md unit-test checklist)."""

from __future__ import annotations

import httpx
import pytest

from config import Config
from session_manager import SessionManager
from stats import StatisticsCollector

UA = "TestAgent/1.0"


@pytest.fixture
def manager(config: Config, statistics: StatisticsCollector) -> SessionManager:
    return SessionManager(config, statistics)


class TestCreateSession:
    async def test_headers_injected(self, manager: SessionManager) -> None:
        client = manager.create_session(UA)
        try:
            assert client.headers["User-Agent"] == UA
            assert "Accept" in client.headers
            assert "Accept-Language" in client.headers
        finally:
            await manager.close_session(client)

    async def test_timeout_from_config(self, config: Config, manager: SessionManager) -> None:
        client = manager.create_session(UA)
        try:
            assert client.timeout.read == config.request_timeout
        finally:
            await manager.close_session(client)

    async def test_redirects_enabled(self, manager: SessionManager) -> None:
        client = manager.create_session(UA)
        try:
            assert client.follow_redirects is True
            assert client.max_redirects == 5
        finally:
            await manager.close_session(client)


class TestLifecycle:
    async def test_close_session(self, manager: SessionManager) -> None:
        client = manager.create_session(UA)
        await manager.close_session(client)
        assert client.is_closed

    async def test_context_manager_closes(self, manager: SessionManager) -> None:
        async with manager.session(UA) as client:
            assert not client.is_closed
        assert client.is_closed

    async def test_context_manager_closes_on_error(self, manager: SessionManager) -> None:
        with pytest.raises(RuntimeError):
            async with manager.session(UA) as client:
                raise RuntimeError("boom")
        assert client.is_closed

    async def test_metrics(self, config: Config) -> None:
        stats = StatisticsCollector()
        manager = SessionManager(config, stats)
        async with manager.session(UA):
            pass
        counters = stats.snapshot()["counters"]
        assert counters["session.created"] == 1
        assert counters["session.closed"] == 1


class TestSessionReuse:
    async def test_cookies_persist_within_session(self, manager: SessionManager) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/set":
                return httpx.Response(200, headers={"set-cookie": "sid=abc"}, text="ok")
            return httpx.Response(200, text=request.headers.get("cookie", ""))

        async with manager.session(UA, transport=httpx.MockTransport(handler)) as client:
            await client.get("https://example.com/set")
            response = await client.get("https://example.com/read")
            assert "sid=abc" in response.text
