# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 The Plinth Authors
"""Environment-driven settings for the gateway service."""

from __future__ import annotations

from pathlib import Path
from typing import Literal, Optional  # noqa: UP035

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

AuthMode = Literal["permissive", "verify_local", "verify_remote"]


class Settings(BaseSettings):
    """Runtime configuration. All env vars are prefixed with ``PLINTH_``."""

    model_config = SettingsConfigDict(
        env_prefix="PLINTH_",
        env_file=None,
        case_sensitive=False,
        extra="ignore",
    )

    data_dir: Path = Field(default=Path("/tmp/plinth-data"))
    gateway_host: str = Field(default="0.0.0.0")
    gateway_port: int = Field(default=7422)
    log_level: str = Field(default="INFO")
    log_format: str = Field(default="console")
    backend_timeout_seconds: float = Field(default=30.0)
    inbound_auth_required: bool = Field(default=True)

    # Rate limiting (token bucket per agent_id).
    # Set ``rate_limits_enabled = False`` to bypass both the bucket and the
    # cost cap (useful for local development and benchmarks).
    rate_limits_enabled: bool = Field(default=True)
    rate_limit_default_rpm: int = Field(default=60)
    rate_limit_default_burst: int = Field(default=20)

    # Cost caps (rolling-window USD per agent).
    cost_cap_default_usd_hour: float = Field(default=1.0)
    cost_cap_default_usd_day: float = Field(default=10.0)

    # v0.3 — JWT auth (see ``plinth_gateway.jwt_auth``).
    auth_mode: AuthMode = Field(default="permissive")
    identity_jwt_secret: Optional[str] = Field(default=None)  # noqa: UP007, UP045
    jwt_audience: str = Field(default="plinth")
    identity_url: str = Field(default="http://localhost:7425")
    auth_remote_timeout_seconds: float = Field(default=2.0)

    # v0.4 — RS256 verification via JWKS. The cache hits identity at most
    # once every ``identity_jwks_cache_ttl_seconds`` (and on demand when an
    # unknown ``kid`` shows up, e.g. right after a rotation).
    identity_jwks_cache_ttl_seconds: int = Field(default=300)

    # OAuth — at-rest encryption + GitHub provider.
    # ``oauth_encryption_key`` is a base64-encoded 32-byte AES-256 key. If empty
    # the gateway auto-generates one at ``$PLINTH_DATA_DIR/gateway-oauth-key``
    # and emits a warning (acceptable in dev; production must always set it).
    # ``oauth_github_client_id`` empty means the GitHub authorize endpoint
    # returns a helpful 503 — the gateway does NOT crash on startup.
    oauth_encryption_key: str = Field(default="")
    oauth_github_client_id: str = Field(default="")
    oauth_github_client_secret: str = Field(default="")
    oauth_github_redirect_uri: str = Field(
        default="http://localhost:7422/v1/oauth/github/callback"
    )
    oauth_github_scopes: str = Field(default="repo,read:user")

    # v0.4 — Slack OAuth.
    # Empty client_id means the slack authorize endpoint returns 503 with
    # ``OAUTH_NOT_CONFIGURED`` (same pattern as GitHub).
    oauth_slack_client_id: str = Field(default="")
    oauth_slack_client_secret: str = Field(default="")
    oauth_slack_redirect_uri: str = Field(
        default="http://localhost:7422/v1/oauth/slack/callback"
    )
    oauth_slack_scopes: str = Field(default="channels:read,chat:write,users:read")

    # v0.4 — Linear OAuth.
    oauth_linear_client_id: str = Field(default="")
    oauth_linear_client_secret: str = Field(default="")
    oauth_linear_redirect_uri: str = Field(
        default="http://localhost:7422/v1/oauth/linear/callback"
    )
    oauth_linear_scopes: str = Field(default="read,write")

    oauth_state_ttl_seconds: int = Field(default=600)

    # v0.4 — OTLP observability event stream.
    # When ``otlp_enabled = True`` the gateway forwards every audit event to an
    # OpenTelemetry collector as an OTel Log record. Default is ``False`` so v0.3
    # deploys keep their exact behaviour. Failures inside the OTLP path are
    # *never* allowed to break a tool invocation.
    otlp_enabled: bool = Field(default=False)
    otlp_endpoint: str = Field(default="http://localhost:4318")
    otlp_service_name: str = Field(default="plinth-gateway")
    otlp_batch_size: int = Field(default=64)
    otlp_flush_interval_seconds: float = Field(default=2.0)
    otlp_headers_json: str = Field(default="{}")

    # v0.4 — pluggable storage driver. ``sqlite`` is the default.
    storage_driver: Literal["sqlite", "postgres"] = Field(default="sqlite")
    database_url: str = Field(default="")
    gateway_database_url: str = Field(default="")
    db_pool_min_size: int = Field(default=5)
    db_pool_max_size: int = Field(default=20)

    # v0.5 — load-shedding middleware. Default disabled so existing
    # deployments are unaffected. When ``load_shed_enabled = True``, each
    # request acquires a slot from a bounded inflight + queue tracker;
    # over-capacity requests get a 503 with ``Retry-After`` instead of
    # piling up and exhausting memory.
    load_shed_enabled: bool = Field(default=False)
    load_shed_max_inflight: int = Field(default=200, ge=1)
    load_shed_max_queue: int = Field(default=1000, ge=0)
    load_shed_retry_after_seconds: int = Field(default=1, ge=0)

    # v0.5 — schema migration framework. Default True applies pending
    # migrations on startup. Set False where migration application is
    # gated by an operator (CI/CD pipeline, blue/green deploy). When False
    # the service still starts but emits a WARNING per pending migration.
    auto_migrate: bool = Field(default=True)

    @property
    def effective_database_url(self) -> str:
        """Service-specific URL wins, then the shared one."""

        return self.gateway_database_url or self.database_url

    @property
    def db_path(self) -> Path:
        """Absolute path to the SQLite database file."""
        return self.data_dir / "gateway.db"

    @property
    def jwt_secret_value(self) -> str | None:
        """Effective HS256 secret for ``verify_local`` mode."""

        return self.identity_jwt_secret

    def ensure_data_dir(self) -> None:
        """Make sure the configured data dir exists on disk."""
        self.data_dir.mkdir(parents=True, exist_ok=True)


def get_settings() -> Settings:
    """Construct a fresh ``Settings`` instance from environment.

    A fresh instance per call lets tests vary env vars without singleton fights.
    """
    return Settings()
