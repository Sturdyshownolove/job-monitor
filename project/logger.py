"""Central logging service.

Configures Loguru with a console sink and a rotating file sink
(logs/app.log, 10 MB rotation, 10 files retained — Performance Budget.md).

Every module imports `logger` from here. print() is forbidden project-wide.
"""

from __future__ import annotations

import sys
from pathlib import Path

from loguru import logger

_LOG_FORMAT = (
    "<green>{time:YYYY-MM-DD HH:mm:ss.SSS}</green> | "
    "<level>{level: <8}</level> | "
    "<cyan>{name}</cyan>:<cyan>{function}</cyan> | "
    "{message}"
)

_ROTATION = "10 MB"
_RETENTION = 10


def _stderr_sink(message: str) -> None:
    # Resolve sys.stderr at write time: the stream object may be replaced or
    # closed after setup (test runners, console redirection).
    try:
        sys.stderr.write(message)
    except ValueError:
        pass


def setup_logging(log_level: str = "INFO", logs_dir: Path | None = None) -> None:
    """Initialize console and file sinks. Idempotent — replaces existing sinks."""
    logger.remove()
    logger.add(_stderr_sink, level=log_level, format=_LOG_FORMAT, backtrace=False, diagnose=False)

    target_dir = logs_dir if logs_dir is not None else Path("logs")
    target_dir.mkdir(parents=True, exist_ok=True)
    logger.add(
        target_dir / "app.log",
        level=log_level,
        format=_LOG_FORMAT,
        rotation=_ROTATION,
        retention=_RETENTION,
        encoding="utf-8",
        backtrace=False,
        diagnose=False,
    )


__all__ = ["logger", "setup_logging"]
