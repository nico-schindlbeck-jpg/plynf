# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 The Plinth Authors
"""Runtime settings for the Atlassian MCP server.

Reads configuration from env vars prefixed with ``PLINTH_ATLASSIAN_MCP_``.
The server itself never reads OAuth secrets — the gateway forwards the
user access token via ``Authorization: Bearer ...`` on each invocation,
plus the workspace cloudid via ``X-Plinth-OAuth-Cloudid``.
"""

from __future__ import annotations

from typing import Literal

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Settings loaded from environment variables.

    Attributes:
        port: TCP port to bind to (default 7431 per CONTRACTS.md v1.5).
        api_base_url: Atlassian API root. Override for testing.
        request_timeout_seconds: httpx timeout per outbound Atlassian call.
        log_level: Standard logging level (e.g. ``"INFO"``).
        log_format: ``"console"`` or ``"json"``.
    """

    port: int = 7431
    api_base_url: str = "https://api.atlassian.com"
    request_timeout_seconds: float = 15.0
    log_level: str = "INFO"
    log_format: Literal["console", "json"] = "console"

    model_config = SettingsConfigDict(
        env_prefix="PLINTH_ATLASSIAN_MCP_",
        extra="ignore",
        case_sensitive=False,
    )


def get_settings() -> Settings:
    """Build a fresh :class:`Settings` from the environment."""
    return Settings()
