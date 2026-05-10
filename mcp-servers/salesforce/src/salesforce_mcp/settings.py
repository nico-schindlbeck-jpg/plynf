# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 The Plinth Authors
"""Runtime settings for the Salesforce MCP server.

Reads configuration from env vars prefixed with ``PLINTH_SALESFORCE_MCP_``.
"""

from __future__ import annotations

from typing import Literal

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Settings loaded from environment variables.

    Attributes:
        port: TCP port to bind to (default 7432 per CONTRACTS.md v1.5).
        api_version: Salesforce REST API version (e.g. ``v60.0``). Salesforce
            URLs are versioned at the path level, e.g.
            ``{instance_url}/services/data/v60.0/...``.
        request_timeout_seconds: httpx timeout per outbound Salesforce call.
        log_level: Standard logging level.
        log_format: ``"console"`` or ``"json"``.
    """

    port: int = 7432
    api_version: str = "v60.0"
    request_timeout_seconds: float = 15.0
    log_level: str = "INFO"
    log_format: Literal["console", "json"] = "console"

    model_config = SettingsConfigDict(
        env_prefix="PLINTH_SALESFORCE_MCP_",
        extra="ignore",
        case_sensitive=False,
    )


def get_settings() -> Settings:
    """Build a fresh :class:`Settings` from the environment."""
    return Settings()
