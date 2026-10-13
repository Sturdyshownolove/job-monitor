"""User agent provider tests (UserAgentRotation.md unit-test checklist)."""

from __future__ import annotations

import random
from collections import Counter

from config import Config
from user_agent import FALLBACK_USER_AGENT, UserAgentProvider


def make_provider(rotate: bool = True, seed: int = 42) -> UserAgentProvider:
    config = Config(
        telegram_token="t",
        chat_id="c",
        rotate_user_agent=rotate,
    )
    return UserAgentProvider(config, rng=random.Random(seed))


class TestNewSession:
    def test_never_empty(self) -> None:
        provider = make_provider()
        for _ in range(50):
            assert provider.new_session()

    def test_rotation_disabled_returns_static(self) -> None:
        provider = make_provider(rotate=False)
        assert provider.new_session() == FALLBACK_USER_AGENT

    def test_deterministic_with_seed(self) -> None:
        assert make_provider(seed=7).new_session() == make_provider(seed=7).new_session()

    def test_all_agents_look_like_browsers(self) -> None:
        provider = make_provider()
        for _ in range(50):
            assert provider.new_session().startswith("Mozilla/5.0")


class TestDistribution:
    def test_chrome_dominates(self) -> None:
        provider = make_provider(seed=1)
        counts = Counter()
        for _ in range(500):
            ua = provider.new_session()
            if "Edg/" in ua:
                counts["edge"] += 1
            elif "Firefox/" in ua:
                counts["firefox"] += 1
            else:
                counts["chrome"] += 1
        assert counts["chrome"] > counts["edge"] > counts["firefox"]

    def test_windows_dominates(self) -> None:
        provider = make_provider(seed=2)
        counts = Counter()
        for _ in range(500):
            ua = provider.new_session()
            if "Windows NT" in ua:
                counts["windows"] += 1
            elif "Mac OS X" in ua:
                counts["macos"] += 1
            else:
                counts["linux"] += 1
        assert counts["windows"] > counts["macos"] > counts["linux"]


class TestFallback:
    def test_fallback_is_modern_chrome(self) -> None:
        assert "Chrome/" in FALLBACK_USER_AGENT
        assert "Windows NT 10.0" in FALLBACK_USER_AGENT
