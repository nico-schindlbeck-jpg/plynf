# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 The Plinth Authors
"""Runtime settings for the Google Workspace MCP server.

Reads configuration from env vars prefixed with ``PLINTH_GOOGLE_MCP_``. The
server itself never reads OAuth secrets — the gateway forwards the user
access token via ``Authorization: Bearer ...`` on each tool invocation.
"""

from __future__ import annotations

from typing import Literal

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Settings loaded from environment variables.

    Attributes:
        port: TCP port to bind to (default 7430 per CONTRACTS.md v1.1).
        drive_base_url: Drive API root. Override for testing.
        docs_base_url: Docs API root. Override for testing.
        sheets_base_url: Sheets API root. Override for testing.
        gmail_base_url: Gmail API root. Override for testing.
        request_timeout_seconds: httpx timeout per outbound Google call.
        log_level: Standard logging level.
        log_format: ``"console"`` or ``"json"``.
    """

    port: int = 7430
    # Google has separate endpoint hosts per product family. We expose each
    # individually so tests can mock them in isolation; production uses the
    # canonical defaults.
    drive_base_url: str = "https://www.googleapis.com"
    docs_base_url: str = "https://docs.googleapis.com"
    sheets_base_url: str = "https://sheets.googleapis.com"
    gmail_base_url: str = "https://gmail.googleapis.com"
    calendar_base_url: str = "https://www.googleapis.com"
    request_timeout_seconds: float = 15.0
    log_level: str = "INFO"
    log_format: Literal["console", "json"] = "console"

    model_config = SettingsConfigDict(
        env_prefix="PLINTH_GOOGLE_MCP_",
        extra="ignore",
        case_sensitive=False,
    )


def get_settings() -> Settings:
    """Build a fresh :class:`Settings` from the environment."""
    return Settings()
