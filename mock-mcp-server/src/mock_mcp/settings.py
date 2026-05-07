# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 The Plinth Authors
"""Runtime settings for the Mock MCP Server.

Reads configuration from environment variables prefixed with ``PLINTH_MOCK_``.
"""

from __future__ import annotations

from pathlib import Path
from typing import Literal

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Settings loaded from ``PLINTH_MOCK_*`` env vars.

    Attributes:
        port: TCP port the FastAPI app binds to.
        fixtures_dir: Filesystem root that ``fs.read`` / ``fs.write`` operate on.
            Created on startup if it doesn't exist.
        log_level: Standard logging level (e.g. ``"INFO"``, ``"DEBUG"``).
        log_format: ``"console"`` for human-readable, ``"json"`` for structured logs.
    """

    port: int = 7423
    fixtures_dir: Path = Path("/tmp/plinth-mock-fixtures")
    log_level: str = "INFO"
    log_format: Literal["console", "json"] = "console"

    model_config = SettingsConfigDict(env_prefix="PLINTH_MOCK_", extra="ignore")


def get_settings() -> Settings:
    """Construct a fresh ``Settings`` instance from the current environment."""
    return Settings()
