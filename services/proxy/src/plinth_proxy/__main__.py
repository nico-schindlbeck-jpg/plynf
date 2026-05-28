# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 The Plynf Authors
"""Run with ``python -m plinth_proxy`` or ``plinth-proxy`` (via entry point)."""

from __future__ import annotations

import uvicorn

from .settings import ProxySettings


def main() -> None:
    settings = ProxySettings()
    uvicorn.run(
        "plinth_proxy.api:app",
        host=settings.host,
        port=settings.port,
        log_level="info",
    )


if __name__ == "__main__":  # pragma: no cover
    main()
