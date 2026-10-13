"""Central configuration module.

Loads environment variables from `.env`, validates them, and exposes a single
immutable configuration object injected into every service.

See: Config.md (vault). All runtime configuration originates here — no module
may read environment variables directly.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Literal

from dotenv import load_dotenv
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

LogLevel = Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"]

DEFAULT_KEYWORDS: tuple[str, ...] = (
    "Product Designer",
    "Senior Product Designer",
    "UX Designer",
    "UI Designer",
    "UX/UI Designer",
    "Senior UX Designer",
    "Senior UI Designer",
    "Senior UX/UI Designer",
    "Product Design",
    "Interaction Designer",
    "Experience Designer",
)

_BOOL_TRUE = "true"
_BOOL_FALSE = "false"


class ConfigurationError(Exception):
    """Raised when required configuration is missing or invalid.

    Startup must abort immediately when this is raised.
    """


class Config(BaseModel):
    """Immutable application configuration (Config.md)."""

    model_config = ConfigDict(frozen=True)

    telegram_token: str = Field(min_length=1)
    chat_id: str = Field(min_length=1)
    database_url: str = Field(default="sqlite+aiosqlite:///database/jobs.db", min_length=1)
    check_interval: int = Field(default=300, ge=60, le=3600)
    headless: bool = True
    log_level: LogLevel = "INFO"
    request_timeout: float = Field(default=30.0, gt=0)
    max_retries: int = Field(default=3, gt=0)
    retry_base_delay: float = Field(default=1.0, gt=0)
    retry_backoff: float = Field(default=2.0, ge=1)
    retry_max_delay: float = Field(default=30.0, gt=0)
    retry_jitter: bool = True
    rate_min_delay: float = Field(default=2.0, ge=0)
    rate_max_delay: float = Field(default=5.0, gt=0)
    rotate_user_agent: bool = True
    keywords: tuple[str, ...] = DEFAULT_KEYWORDS
    max_pages: int = Field(default=20, ge=1, le=100)
    max_scroll_attempts: int = Field(default=10, ge=1, le=50)
    # Reject jobs that require German without accepting English.
    language_filter: bool = True

    @field_validator("keywords", mode="before")
    @classmethod
    def _parse_keywords(cls, value: object) -> object:
        if isinstance(value, str):
            parsed = tuple(part.strip() for part in value.split(",") if part.strip())
            if not parsed:
                raise ValueError("keyword list must not be empty")
            return parsed
        return value

    @field_validator("headless", "retry_jitter", "rotate_user_agent", "language_filter", mode="before")
    @classmethod
    def _parse_bool(cls, value: object) -> object:
        # Config.md allows only the literals "true" / "false" for boolean vars.
        if isinstance(value, str):
            lowered = value.strip().lower()
            if lowered == _BOOL_TRUE:
                return True
            if lowered == _BOOL_FALSE:
                return False
            raise ValueError(f"expected 'true' or 'false', got {value!r}")
        return value

    @field_validator("log_level", mode="before")
    @classmethod
    def _normalize_log_level(cls, value: object) -> object:
        if isinstance(value, str):
            return value.strip().upper()
        return value

    @model_validator(mode="after")
    def _check_rate_delay_range(self) -> "Config":
        if self.rate_min_delay > self.rate_max_delay:
            raise ValueError(
                f"RATE_MIN_DELAY ({self.rate_min_delay}) must not exceed "
                f"RATE_MAX_DELAY ({self.rate_max_delay})"
            )
        return self


_ENV_FIELD_MAP: dict[str, str] = {
    "TELEGRAM_TOKEN": "telegram_token",
    "CHAT_ID": "chat_id",
    "DATABASE_URL": "database_url",
    "CHECK_INTERVAL": "check_interval",
    "HEADLESS": "headless",
    "LOG_LEVEL": "log_level",
    "REQUEST_TIMEOUT": "request_timeout",
    "MAX_RETRIES": "max_retries",
    "RETRY_BASE_DELAY": "retry_base_delay",
    "RETRY_BACKOFF": "retry_backoff",
    "RETRY_MAX_DELAY": "retry_max_delay",
    "RETRY_JITTER": "retry_jitter",
    "RATE_MIN_DELAY": "rate_min_delay",
    "RATE_MAX_DELAY": "rate_max_delay",
    "ROTATE_USER_AGENT": "rotate_user_agent",
    "KEYWORDS": "keywords",
    "MAX_PAGES": "max_pages",
    "MAX_SCROLL_ATTEMPTS": "max_scroll_attempts",
    "LANGUAGE_FILTER": "language_filter",
}


def load_config(env_file: Path | None = None) -> Config:
    """Load, validate and freeze application configuration.

    Reads `.env` (if present) into the process environment, then builds the
    Config object from environment variables. Raises ConfigurationError with a
    readable, secret-free message when validation fails.
    """
    if env_file is not None:
        load_dotenv(env_file, override=False)
    else:
        # Explicitly resolve from the working directory ("defaults to ./.env").
        # Bare load_dotenv() would search upward from this module's location,
        # escaping test isolation and surprising callers running elsewhere.
        load_dotenv(Path.cwd() / ".env", override=False)

    raw: dict[str, str] = {}
    for env_name, field_name in _ENV_FIELD_MAP.items():
        value = os.environ.get(env_name)
        if value is not None and value.strip() != "":
            raw[field_name] = value

    try:
        return Config(**raw)
    except ValidationError as exc:
        reasons = "; ".join(
            f"{_field_to_env(str(err['loc'][0])) if err['loc'] else 'config'}: {err['msg']}"
            for err in exc.errors()
        )
        raise ConfigurationError(f"Invalid configuration — {reasons}") from None


def _field_to_env(field_name: str) -> str:
    for env_name, mapped in _ENV_FIELD_MAP.items():
        if mapped == field_name:
            return env_name
    return field_name.upper()
