# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 The Plinth Authors
"""structlog configuration for the workspace service."""

from __future__ import annotations

import logging
import sys
from typing import Literal

import structlog

from . import __service__


def configure_logging(level: str = "INFO", fmt: Literal["console", "json"] = "console") -> None:
    """Configure structlog + stdlib logging for the workspace service.

    Args:
        level: Standard ``logging`` level name (``INFO``, ``DEBUG``, ...).
        fmt: ``console`` for human-readable output, ``json`` for ingest.
    """

    log_level = getattr(logging, level.upper(), logging.INFO)

    timestamper = structlog.processors.TimeStamper(fmt="iso", utc=True)

    shared_processors: list[structlog.types.Processor] = [
        structlog.contextvars.merge_contextvars,
        structlog.processors.add_log_level,
        timestamper,
        structlog.processors.StackInfoRenderer(),
        structlog.processors.format_exc_info,
    ]

    if fmt == "json":
        renderer: structlog.types.Processor = structlog.processors.JSONRenderer()
    else:
        renderer = structlog.dev.ConsoleRenderer(colors=False)

    structlog.configure(
        processors=[*shared_processors, renderer],
        wrapper_class=structlog.make_filtering_bound_logger(log_level),
        logger_factory=structlog.PrintLoggerFactory(file=sys.stdout),
        cache_logger_on_first_use=True,
    )

    # Tame the stdlib loggers used by uvicorn and friends; we only emit through
    # structlog ourselves.
    logging.basicConfig(
        format="%(message)s",
        stream=sys.stdout,
        level=log_level,
    )

    # Bind the service name once so every log line carries it.
    structlog.contextvars.clear_contextvars()
    structlog.contextvars.bind_contextvars(service=__service__)


def get_logger(name: str | None = None) -> structlog.stdlib.BoundLogger:
    """Return a structlog logger.

    Always prefer :func:`get_logger` over :func:`structlog.get_logger`
    inside this package so we have a single seam for tests.
    """

    return structlog.get_logger(name or __service__)
