# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 The Plinth Authors
"""Entrypoint for ``python -m mock_mcp``.

Starts a uvicorn server bound to the port configured via
``PLINTH_MOCK_PORT`` (default 7423).
"""

from __future__ import annotations

import uvicorn

from .settings import get_settings


def main() -> None:
    """Start the mock-mcp uvicorn server on the configured port."""
    settings = get_settings()
    uvicorn.run(
        "mock_mcp.server:app",
        host="0.0.0.0",  # noqa: S104 - dev server, intentional
        port=settings.port,
        log_level=settings.log_level.lower(),
    )


if __name__ == "__main__":
    main()
