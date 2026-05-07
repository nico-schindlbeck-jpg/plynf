# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 The Plinth Authors
"""Worker logging — structlog with console / JSON renderers."""

from __future__ import annotations

import logging
import sys
from typing import Literal

import structlog


def configure_logging(
    *,
    level: str = "INFO",
    fmt: Literal["console", "json"] = "console",
) -> None:
    """Configure structlog + the stdlib logging bridge.

    The worker writes a single line per event so ``docker logs`` /
    ``journalctl`` / ``kubectl logs`` stay readable.
    """

    timestamper = structlog.processors.TimeStamper(fmt="iso", utc=True)
    # ``add_logger_name`` requires the underlying logger to have a ``name``
    # attribute. We use ``PrintLoggerFactory`` (which doesn't), so we can't
    # pull it from there; instead we set a constant via ``contextvars``.
    structlog.contextvars.bind_contextvars(logger="plinth_workflow_worker")
    shared_processors = [
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.add_log_level,
        timestamper,
    ]

    if fmt == "json":
        renderer = structlog.processors.JSONRenderer()
    else:
        renderer = structlog.dev.ConsoleRenderer(colors=False)

    structlog.configure(
        processors=[
            *shared_processors,
            structlog.processors.StackInfoRenderer(),
            structlog.processors.format_exc_info,
            renderer,
        ],
        wrapper_class=structlog.make_filtering_bound_logger(
            getattr(logging, level.upper(), logging.INFO),
        ),
        context_class=dict,
        logger_factory=structlog.PrintLoggerFactory(file=sys.stderr),
        cache_logger_on_first_use=True,
    )

    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(message)s",
        stream=sys.stderr,
    )


def get_logger(name: str = "plinth_workflow_worker") -> structlog.BoundLogger:
    """Return a structured logger bound to ``name``."""
    return structlog.get_logger(name)
