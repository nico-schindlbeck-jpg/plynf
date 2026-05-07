# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 The Plinth Authors
"""Runtime configuration for the dashboard service.

All values are read from environment variables prefixed with ``PLINTH_DASHBOARD_``.
"""

from __future__ import annotations

from typing import Literal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Dashboard-service settings.

    Attributes:
        port: Port the FastAPI app listens on.
        host: Host the FastAPI app binds to.
        workspace_url: Base URL of the workspace service.
        gateway_url: Base URL of the gateway service.
        mock_mcp_url: Base URL of the mock MCP server (for status pill).
        api_token: Bearer token used to authenticate against backends.
        backend_timeout_seconds: Timeout for outbound httpx calls.
        log_level: Standard ``logging`` level name.
        log_format: ``console`` (dev) or ``json`` (prod) formatter.
    """

    model_config = SettingsConfigDict(
        env_prefix="PLINTH_DASHBOARD_",
        env_file=None,
        case_sensitive=False,
        extra="ignore",
    )

    port: int = Field(default=7424)
    host: str = Field(default="0.0.0.0")
    workspace_url: str = Field(default="http://localhost:7421")
    gateway_url: str = Field(default="http://localhost:7422")
    mock_mcp_url: str = Field(default="http://localhost:7423")
    identity_url: str = Field(default="http://localhost:7425")
    api_token: str = Field(default="dashboard-token")
    backend_timeout_seconds: float = Field(default=5.0)
    log_level: str = Field(default="INFO")
    log_format: Literal["console", "json"] = Field(default="console")

    @property
    def auth_header(self) -> str:
        """Return the Authorization header value, ensuring a Bearer prefix."""
        token = self.api_token.strip()
        if not token.lower().startswith("bearer "):
            token = f"Bearer {token}"
        return token


def get_settings(**overrides: object) -> Settings:
    """Construct a fresh :class:`Settings`.

    Tests can pass overrides directly to bypass env vars without touching
    the global state.
    """

    return Settings(**overrides)  # type: ignore[arg-type]
