# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 The Plinth Authors
"""Structlog-based logging configuration for the Mock MCP Server."""

from __future__ import annotations

import logging
import sys
from typing import Literal

import structlog


def configure_logging(level: str = "INFO", fmt: Literal["console", "json"] = "console") -> None:
    """Configure structlog and the stdlib root logger.

    Args:
        level: Root logging level name (e.g. ``"INFO"``).
        fmt: ``"console"`` for human-readable output, ``"json"`` for structured.
    """
    log_level = getattr(logging, level.upper(), logging.INFO)
    logging.basicConfig(
        format="%(message)s",
        stream=sys.stdout,
        level=log_level,
    )

    processors: list[structlog.types.Processor] = [
        structlog.contextvars.merge_contextvars,
        structlog.processors.add_log_level,
        structlog.processors.TimeStamper(fmt="iso"),
    ]
    if fmt == "json":
        processors.append(structlog.processors.JSONRenderer())
    else:
        processors.append(structlog.dev.ConsoleRenderer(colors=False))

    structlog.configure(
        processors=processors,
        wrapper_class=structlog.make_filtering_bound_logger(log_level),
        context_class=dict,
        logger_factory=structlog.PrintLoggerFactory(),
        cache_logger_on_first_use=True,
    )


def get_logger(name: str | None = None) -> structlog.stdlib.BoundLogger:
    """Return a structlog logger bound with ``service=mock-mcp``."""
    logger = structlog.get_logger(name) if name else structlog.get_logger()
    return logger.bind(service="mock-mcp")
