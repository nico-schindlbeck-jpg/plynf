# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 The Plinth Authors
"""Environment-driven settings for the gateway service."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Literal, Optional  # noqa: UP035

from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict
from pydantic_settings.sources import EnvSettingsSource

AuthMode = Literal["permissive", "verify_local", "verify_remote"]
ReplicationMode = Literal["primary", "replica", "standalone"]


class _PlinthEnvSource(EnvSettingsSource):
    """Subclass that accepts comma-separated values for ``region_peers``."""

    def decode_complex_value(self, field_name, field, value):  # type: ignore[override]
        if field_name == "region_peers" and isinstance(value, str):
            stripped = value.strip()
            if not stripped:
                return []
            return [part.strip() for part in stripped.split(",") if part.strip()]
        return super().decode_complex_value(field_name, field, value)


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

    # v1.1 — Notion OAuth. Notion's flow is workspace-scoped (no per-call
    # scopes) and does NOT support PKCE. Empty client_id means the authorize
    # endpoint returns 503 with ``OAUTH_NOT_CONFIGURED`` (same pattern as the
    # other providers).
    oauth_notion_client_id: str = Field(default="")
    oauth_notion_client_secret: str = Field(default="")
    oauth_notion_redirect_uri: str = Field(
        default="http://localhost:7422/v1/oauth/notion/callback"
    )
    oauth_notion_scopes: str = Field(default="")

    # v1.1 — Google Workspace OAuth. PKCE-enabled, refresh-token-aware.
    # Default scopes cover Drive (file-scoped), Docs, Sheets, Calendar
    # (read-only), Gmail (read-only). Operators tighten or broaden via the
    # ``oauth_google_scopes`` env var.
    oauth_google_client_id: str = Field(default="")
    oauth_google_client_secret: str = Field(default="")
    oauth_google_redirect_uri: str = Field(
        default="http://localhost:7422/v1/oauth/google/callback"
    )
    oauth_google_scopes: str = Field(
        default=(
            "openid,email,profile,"
            "https://www.googleapis.com/auth/drive.file,"
            "https://www.googleapis.com/auth/documents,"
            "https://www.googleapis.com/auth/spreadsheets,"
            "https://www.googleapis.com/auth/calendar.readonly,"
            "https://www.googleapis.com/auth/gmail.readonly"
        )
    )

    # v1.5 — Atlassian (Jira + Confluence) OAuth. PKCE-enabled. The gateway
    # also fetches the workspace's ``cloudid`` from
    # ``https://api.atlassian.com/oauth/token/accessible-resources`` after
    # token exchange and stores it in ``connection.metadata`` so MCP server
    # can address Jira/Confluence via the cloudid-prefixed REST routes.
    oauth_atlassian_client_id: str = Field(default="")
    oauth_atlassian_client_secret: str = Field(default="")
    oauth_atlassian_redirect_uri: str = Field(
        default="http://localhost:7422/v1/oauth/atlassian/callback"
    )
    oauth_atlassian_scopes: str = Field(
        default=(
            "read:jira-work,write:jira-work,"
            "read:confluence-content.summary,write:confluence-content,"
            "offline_access"
        )
    )

    # v1.5 — Salesforce OAuth. PKCE-enabled. The token response includes
    # ``instance_url`` (per-org REST API base). The gateway captures this
    # into ``connection.metadata`` and re-injects it as
    # ``X-Plinth-OAuth-InstanceUrl`` on every proxied invoke.
    oauth_salesforce_client_id: str = Field(default="")
    oauth_salesforce_client_secret: str = Field(default="")
    oauth_salesforce_redirect_uri: str = Field(
        default="http://localhost:7422/v1/oauth/salesforce/callback"
    )
    oauth_salesforce_scopes: str = Field(default="api,refresh_token,offline_access")

    # v1.5 — Asana OAuth. PKCE-enabled.
    oauth_asana_client_id: str = Field(default="")
    oauth_asana_client_secret: str = Field(default="")
    oauth_asana_redirect_uri: str = Field(
        default="http://localhost:7422/v1/oauth/asana/callback"
    )
    oauth_asana_scopes: str = Field(default="default")

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

    # v0.6 — federated revocation cache. Polls the identity service's
    # ``GET /v1/revocations`` endpoint every ``revocation_poll_interval_seconds``
    # so a token revoked on a peer replica is rejected here within the
    # poll window. ``revocation_poll_url=""`` (default) keeps the cache
    # disabled — single-node deployments and v0.5 demos rely on Identity's
    # local cache and need no extra configuration.
    revocation_poll_url: str = Field(default="")
    revocation_poll_interval_seconds: int = Field(default=60, ge=1)
    revocation_poll_enabled: bool = Field(default=True)

    # v1.0 — multi-region scaffolding (see ``docs/multi-region.md``).
    # The gateway is stateless — region settings here drive only the
    # ``/v1/regions`` discovery endpoint + the region-aware tag on outgoing
    # tool invocations. There's no replication primitive on the gateway
    # itself, but a replica-mode deployment can still serve cached reads.
    region_id: str = Field(default="default")
    region_peers: list[str] = Field(default_factory=list)
    region_peer_urls: dict[str, str] = Field(default_factory=dict)
    replication_mode: ReplicationMode = Field(default="standalone")
    region_primary_url: str = Field(default="")
    regions_status_cache_ttl_seconds: int = Field(default=30, ge=1)
    regions_status_probe_timeout_seconds: float = Field(default=2.0, ge=0.1)

    # v1.0 — per-tenant resource quotas. Default ``False`` so existing v0.6
    # demos that hammer ``/v1/invoke`` don't suddenly hit quota walls; flip
    # to True in production after sizing the limits in identity.
    quotas_enabled: bool = Field(default=False)
    quotas_cache_ttl_seconds: int = Field(default=60, ge=0)
    quotas_fetch_timeout_seconds: float = Field(default=2.0, ge=0.1)

    # v1.1 — pluggable coordination backend (see ``coordination.py``).
    # ``memory`` (default) keeps v1.0 behaviour exactly: rate-limits, cost
    # caps, and revocation cache are per-process. Flip to ``redis`` to
    # share state across replicas; the gateway, identity, and workspace
    # services all read this same triplet so a multi-replica deployment
    # can flip everything at once.
    coordination_backend: Literal["memory", "redis"] = Field(default="memory")
    coordination_redis_url: str = Field(default="redis://localhost:6379/0")
    coordination_key_prefix: str = Field(default="plinth")

    @field_validator("region_peers", mode="before")
    @classmethod
    def _coerce_region_peers(cls, value: object) -> object:
        """Allow ``["us","eu"]``-style direct overrides + comma strings."""

        if value is None or isinstance(value, list):
            return value
        if isinstance(value, str):
            stripped = value.strip()
            if not stripped:
                return []
            return [part.strip() for part in stripped.split(",") if part.strip()]
        return value

    @classmethod
    def settings_customise_sources(  # type: ignore[override]
        cls,
        settings_cls,
        init_settings,
        env_settings,
        dotenv_settings,
        file_secret_settings,
    ):
        """Wire in :class:`_PlinthEnvSource` for comma-separated peers."""

        custom_env = _PlinthEnvSource(settings_cls)
        return init_settings, custom_env, dotenv_settings, file_secret_settings

    @model_validator(mode="after")
    def _scrape_region_peer_urls(self) -> Settings:
        """Pick up ``PLINTH_REGION_PEER_<ID>_URL`` env vars."""

        prefix = "PLINTH_REGION_PEER_"
        suffix = "_URL"
        declared = {peer.replace("-", "_").lower(): peer for peer in self.region_peers}
        for env_name, env_value in os.environ.items():
            if not env_name.startswith(prefix) or not env_name.endswith(suffix):
                continue
            raw_id = env_name[len(prefix) : -len(suffix)].lower()
            if not raw_id:
                continue
            peer_id = declared.get(raw_id, raw_id.replace("_", "-"))
            if peer_id in self.region_peer_urls:
                continue
            self.region_peer_urls[peer_id] = env_value
        return self

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
