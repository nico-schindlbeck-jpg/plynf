# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 The Plinth Authors
"""Runtime settings for the GitHub MCP server.

Reads configuration from env vars prefixed with ``PLINTH_`` (and a few
``PLINTH_GITHUB_*`` overrides). The server itself never reads OAuth secrets —
the gateway forwards the user access token via ``Authorization: Bearer ...``.
"""

from __future__ import annotations

from typing import Literal

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Settings loaded from environment variables.

    Attributes:
        port: TCP port to bind to (default 7426 per CONTRACTS.md v0.3).
        api_base_url: GitHub REST root. Override for testing.
        request_timeout_seconds: httpx timeout per outbound GitHub call.
        log_level: Standard logging level (e.g. ``"INFO"``).
        log_format: ``"console"`` or ``"json"``.
        api_version: GitHub API version pin (sent as ``X-GitHub-Api-Version``).
    """

    port: int = 7426
    api_base_url: str = "https://api.github.com"
    request_timeout_seconds: float = 15.0
    log_level: str = "INFO"
    log_format: Literal["console", "json"] = "console"
    api_version: str = "2022-11-28"

    # ``PLINTH_MOCK_PORT`` is the existing convention for picking the port of a
    # service in dev (the mock-mcp uses it). For consistency we also accept a
    # GitHub-specific override; either env var works.
    model_config = SettingsConfigDict(
        env_prefix="PLINTH_GITHUB_",
        extra="ignore",
        case_sensitive=False,
    )


def get_settings() -> Settings:
    """Build a fresh :class:`Settings` from the environment."""
    import os

    settings = Settings()
    # Honour PLINTH_MOCK_PORT for parity with the existing example commands.
    mock_port = os.environ.get("PLINTH_MOCK_PORT")
    if mock_port:
        try:
            settings = settings.model_copy(update={"port": int(mock_port)})
        except ValueError:
            pass
    return settings
