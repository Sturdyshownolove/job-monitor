"""Shared test fixtures."""

from __future__ import annotations

import pytest

from config import Config
from stats import StatisticsCollector


@pytest.fixture
def config() -> Config:
    return Config(telegram_token="test-token", chat_id="1", retry_jitter=False)


@pytest.fixture
def statistics() -> StatisticsCollector:
    return StatisticsCollector()
