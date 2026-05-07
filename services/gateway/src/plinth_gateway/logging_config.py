# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 The Plinth Authors
"""Structlog setup. ``console`` for dev, ``json`` for prod."""

from __future__ import annotations

import logging
import sys

import structlog


def configure_logging(level: str = "INFO", fmt: str = "console") -> None:
    """Configure structlog and the stdlib root logger.

    Args:
        level: log level name (DEBUG/INFO/WARNING/ERROR).
        fmt:   ``console`` for human-friendly, ``json`` for machine-parseable.
    """
    log_level = getattr(logging, level.upper(), logging.INFO)

    logging.basicConfig(
        level=log_level,
        format="%(message)s",
        stream=sys.stdout,
        force=True,
    )

    processors: list = [
        structlog.contextvars.merge_contextvars,
        structlog.processors.add_log_level,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
    ]

    if fmt == "json":
        processors.append(structlog.processors.JSONRenderer())
    else:
        processors.append(structlog.dev.ConsoleRenderer(colors=False))

    structlog.configure(
        processors=processors,
        wrapper_class=structlog.make_filtering_bound_logger(log_level),
        context_class=dict,
        logger_factory=structlog.PrintLoggerFactory(file=sys.stdout),
        cache_logger_on_first_use=True,
    )


def get_logger(name: str | None = None) -> structlog.stdlib.BoundLogger:
    """Return a bound structlog logger pre-tagged with ``service=gateway``."""
    logger = structlog.get_logger(name) if name else structlog.get_logger()
    return logger.bind(service="gateway")
