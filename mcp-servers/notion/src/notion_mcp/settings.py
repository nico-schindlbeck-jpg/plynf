# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 The Plinth Authors
"""Runtime settings for the Notion MCP server.

Reads configuration from env vars prefixed with ``PLINTH_NOTION_MCP_``. The
server itself never reads OAuth secrets — the gateway forwards the user
access token via ``Authorization: Bearer ...`` on each tool invocation.
"""

from __future__ import annotations

from typing import Literal

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Settings loaded from environment variables.

    Attributes:
        port: TCP port to bind to (default 7429 per CONTRACTS.md v1.1).
        api_base_url: Notion REST root. Override for testing.
        request_timeout_seconds: httpx timeout per outbound Notion call.
        log_level: Standard logging level (e.g. ``"INFO"``).
        log_format: ``"console"`` or ``"json"``.
        api_version: Notion API version pin (sent as ``Notion-Version``).
    """

    port: int = 7429
    api_base_url: str = "https://api.notion.com"
    request_timeout_seconds: float = 15.0
    log_level: str = "INFO"
    log_format: Literal["console", "json"] = "console"
    api_version: str = "2022-06-28"

    model_config = SettingsConfigDict(
        env_prefix="PLINTH_NOTION_MCP_",
        extra="ignore",
        case_sensitive=False,
    )


def get_settings() -> Settings:
    """Build a fresh :class:`Settings` from the environment."""
    return Settings()
