"""Unit tests for config.py — validation, defaults, fail-fast behavior."""

from __future__ import annotations

import pytest

from config import Config, ConfigurationError, load_config

REQUIRED_ENV = {
    "TELEGRAM_TOKEN": "123456:test-token",
    "CHAT_ID": "987654321",
}

ALL_ENV_KEYS = [
    "TELEGRAM_TOKEN",
    "CHAT_ID",
    "CHECK_INTERVAL",
    "HEADLESS",
    "LOG_LEVEL",
    "REQUEST_TIMEOUT",
    "MAX_RETRIES",
    "RETRY_BASE_DELAY",
    "RETRY_BACKOFF",
    "RETRY_MAX_DELAY",
    "RETRY_JITTER",
    "RATE_MIN_DELAY",
    "RATE_MAX_DELAY",
    "ROTATE_USER_AGENT",
]


@pytest.fixture(autouse=True)
def clean_env(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    # Isolate from the developer's real .env: load_config() reads ./ .env,
    # so run every config test from an empty working directory.
    monkeypatch.chdir(tmp_path)
    for key in ALL_ENV_KEYS:
        monkeypatch.delenv(key, raising=False)


def set_env(monkeypatch: pytest.MonkeyPatch, extra: dict[str, str] | None = None) -> None:
    env = {**REQUIRED_ENV, **(extra or {})}
    for key, value in env.items():
        monkeypatch.setenv(key, value)


class TestRequiredValues:
    def test_valid_minimal_config(self, monkeypatch: pytest.MonkeyPatch) -> None:
        set_env(monkeypatch)
        config = load_config()
        assert config.telegram_token == REQUIRED_ENV["TELEGRAM_TOKEN"]
        assert config.chat_id == REQUIRED_ENV["CHAT_ID"]

    def test_missing_telegram_token_fails(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("CHAT_ID", "1")
        with pytest.raises(ConfigurationError, match="TELEGRAM_TOKEN"):
            load_config()

    def test_missing_chat_id_fails(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("TELEGRAM_TOKEN", "t")
        with pytest.raises(ConfigurationError, match="CHAT_ID"):
            load_config()

    def test_empty_token_fails(self, monkeypatch: pytest.MonkeyPatch) -> None:
        set_env(monkeypatch, {"TELEGRAM_TOKEN": "   "})
        with pytest.raises(ConfigurationError, match="TELEGRAM_TOKEN"):
            load_config()

    def test_error_message_contains_no_secret(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("TELEGRAM_TOKEN", "super-secret-token")
        with pytest.raises(ConfigurationError) as excinfo:
            load_config()
        assert "super-secret-token" not in str(excinfo.value)


class TestDefaults:
    def test_documented_defaults(self, monkeypatch: pytest.MonkeyPatch) -> None:
        set_env(monkeypatch)
        config = load_config()
        assert config.check_interval == 300
        assert config.headless is True
        assert config.log_level == "INFO"
        assert config.request_timeout == 30.0
        assert config.max_retries == 3
        assert config.retry_base_delay == 1.0
        assert config.retry_backoff == 2.0
        assert config.retry_max_delay == 30.0
        assert config.retry_jitter is True
        assert config.rate_min_delay == 2.0
        assert config.rate_max_delay == 5.0
        assert config.rotate_user_agent is True


class TestCheckInterval:
    def test_minimum_accepted(self, monkeypatch: pytest.MonkeyPatch) -> None:
        set_env(monkeypatch, {"CHECK_INTERVAL": "60"})
        assert load_config().check_interval == 60

    def test_maximum_accepted(self, monkeypatch: pytest.MonkeyPatch) -> None:
        set_env(monkeypatch, {"CHECK_INTERVAL": "3600"})
        assert load_config().check_interval == 3600

    def test_below_minimum_fails(self, monkeypatch: pytest.MonkeyPatch) -> None:
        set_env(monkeypatch, {"CHECK_INTERVAL": "30"})
        with pytest.raises(ConfigurationError, match="CHECK_INTERVAL"):
            load_config()

    def test_above_maximum_fails(self, monkeypatch: pytest.MonkeyPatch) -> None:
        set_env(monkeypatch, {"CHECK_INTERVAL": "7200"})
        with pytest.raises(ConfigurationError, match="CHECK_INTERVAL"):
            load_config()


class TestBooleans:
    @pytest.mark.parametrize("raw,expected", [("true", True), ("false", False), ("TRUE", True), ("False", False)])
    def test_headless_parses_documented_literals(
        self, monkeypatch: pytest.MonkeyPatch, raw: str, expected: bool
    ) -> None:
        set_env(monkeypatch, {"HEADLESS": raw})
        assert load_config().headless is expected

    @pytest.mark.parametrize("raw", ["yes", "1", "on", "enabled"])
    def test_headless_rejects_other_literals(
        self, monkeypatch: pytest.MonkeyPatch, raw: str
    ) -> None:
        set_env(monkeypatch, {"HEADLESS": raw})
        with pytest.raises(ConfigurationError, match="HEADLESS"):
            load_config()


class TestLogLevel:
    @pytest.mark.parametrize("level", ["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"])
    def test_supported_levels(self, monkeypatch: pytest.MonkeyPatch, level: str) -> None:
        set_env(monkeypatch, {"LOG_LEVEL": level})
        assert load_config().log_level == level

    def test_lowercase_normalized(self, monkeypatch: pytest.MonkeyPatch) -> None:
        set_env(monkeypatch, {"LOG_LEVEL": "debug"})
        assert load_config().log_level == "DEBUG"

    def test_unsupported_level_fails(self, monkeypatch: pytest.MonkeyPatch) -> None:
        set_env(monkeypatch, {"LOG_LEVEL": "VERBOSE"})
        with pytest.raises(ConfigurationError, match="LOG_LEVEL"):
            load_config()


class TestNumericBounds:
    def test_zero_timeout_fails(self, monkeypatch: pytest.MonkeyPatch) -> None:
        set_env(monkeypatch, {"REQUEST_TIMEOUT": "0"})
        with pytest.raises(ConfigurationError, match="REQUEST_TIMEOUT"):
            load_config()

    def test_zero_retries_fails(self, monkeypatch: pytest.MonkeyPatch) -> None:
        set_env(monkeypatch, {"MAX_RETRIES": "0"})
        with pytest.raises(ConfigurationError, match="MAX_RETRIES"):
            load_config()

    def test_rate_min_above_max_fails(self, monkeypatch: pytest.MonkeyPatch) -> None:
        set_env(monkeypatch, {"RATE_MIN_DELAY": "6", "RATE_MAX_DELAY": "3"})
        with pytest.raises(ConfigurationError):
            load_config()


class TestImmutability:
    def test_config_is_frozen(self, monkeypatch: pytest.MonkeyPatch) -> None:
        set_env(monkeypatch)
        config = load_config()
        with pytest.raises(Exception):
            config.check_interval = 999  # type: ignore[misc]

    def test_direct_construction_validates(self) -> None:
        config = Config(telegram_token="t", chat_id="c")
        assert config.check_interval == 300
