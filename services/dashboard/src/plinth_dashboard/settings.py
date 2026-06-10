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
    proxy_url: str = Field(default="http://localhost:7430")
    api_token: str = Field(default="dashboard-token")
    # Control-plane API (``/api/app/*``) backing the marketing-stack /app
    # dashboard. ``app_state_path`` empty = in-memory; set a path to persist
    # config across restarts. ``cors_origins`` allow-lists the separate
    # dashboard origin (Astro dev server / app.plynf.com).
    app_state_path: str = Field(default="")
    cors_origins: str = Field(
        default="http://localhost:4321,http://localhost:4322,https://app.plynf.com"
    )
    # When true, ``/api/app/*`` (except the session-issue endpoint) requires a
    # valid JWT capability token, verified against the identity service. Off by
    # default so the offline demo + tests run unauthenticated; turn on in prod.
    app_auth_required: bool = Field(default=False)
    # Defaults for dashboard-session tokens minted via POST /api/app/session.
    app_session_tenant: str = Field(default="acme")
    app_session_ttl_seconds: int = Field(default=3600)
    # -- Self-serve accounts + billing (the zero-terminal flow) -------------
    # accounts_path empty = in-memory (tests/demo); set a file path in prod
    # (e.g. /data/plynf-accounts.json on a persistent disk).
    accounts_path: str = Field(default="")
    # Shared secret protecting GET /internal/keys/* — the proxy presents it
    # as X-Internal-Secret to resolve customer API keys. Required in prod.
    internal_secret: str = Field(default="")
    # Public site origin used to build checkout success/cancel + portal URLs.
    site_url: str = Field(default="https://plynf.com")
    # Stripe: leave empty to run simulated billing (demo/preview). Setting
    # the four values below is the ONLY step needed to charge real money.
    stripe_secret_key: str = Field(default="")
    stripe_webhook_secret: str = Field(default="")
    stripe_price_pro: str = Field(default="")
    stripe_price_enterprise: str = Field(default="")
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
