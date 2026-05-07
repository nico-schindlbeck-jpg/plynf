# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 The Plinth Authors
"""Entrypoint: ``python -m plinth_dashboard``."""

from __future__ import annotations

import uvicorn

from .logging_config import configure_logging
from .server import create_app
from .settings import get_settings


def main() -> None:
    """Run the dashboard service via uvicorn."""

    settings = get_settings()
    configure_logging(level=settings.log_level, fmt=settings.log_format)
    app = create_app(settings)
    uvicorn.run(
        app,
        host=settings.host,
        port=settings.port,
        log_level=settings.log_level.lower(),
        access_log=False,
    )


if __name__ == "__main__":
    main()
