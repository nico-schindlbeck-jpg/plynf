# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 The Plinth Authors
"""Runtime configuration for the workspace service.

All values are read from environment variables prefixed with ``PLINTH_``.
"""

from __future__ import annotations

from pathlib import Path
from typing import Literal, Optional  # noqa: UP035

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


AuthMode = Literal["permissive", "verify_local", "verify_remote"]


class Settings(BaseSettings):
    """Workspace-service settings.

    Attributes:
        data_dir: Root directory for SQLite + blob storage.
        workspace_port: Port the FastAPI app listens on.
        workspace_host: Host the FastAPI app binds to.
        log_level: Standard ``logging`` level name.
        log_format: ``console`` (dev) or ``json`` (prod) formatter.
        auth_required: When True, requests without a bearer token get 401.
        auth_mode: ``permissive`` | ``verify_local`` | ``verify_remote``.
            See ``plinth_workspace.auth`` for the semantics of each. The
            default is ``permissive`` so v0.2 demos keep working.
        identity_jwt_secret: Shared HS256 secret used by ``verify_local``.
        jwt_audience: Expected ``aud`` claim value.
        identity_url: Base URL used by ``verify_remote`` mode.
        auth_remote_timeout_seconds: HTTP timeout for ``verify_remote``.
    """

    model_config = SettingsConfigDict(
        env_prefix="PLINTH_",
        env_file=None,
        case_sensitive=False,
        extra="ignore",
    )

    data_dir: Path = Field(default=Path("/tmp/plinth-data"))
    workspace_port: int = Field(default=7421)
    workspace_host: str = Field(default="0.0.0.0")
    log_level: str = Field(default="INFO")
    log_format: Literal["console", "json"] = Field(default="console")
    auth_required: bool = Field(default=False)

    # v0.3 — JWT auth
    auth_mode: AuthMode = Field(default="permissive")
    # The env name matches the identity service for ergonomic shared-secret
    # deployments (set ``PLINTH_IDENTITY_JWT_SECRET`` once, every service picks
    # it up). The local field name keeps the service prefix-agnostic.
    identity_jwt_secret: Optional[str] = Field(default=None)  # noqa: UP007, UP045
    jwt_audience: str = Field(default="plinth")
    identity_url: str = Field(default="http://localhost:7425")
    auth_remote_timeout_seconds: float = Field(default=2.0)

    # v0.4 — RS256 verification via JWKS. The cache hits identity at most
    # once every ``identity_jwks_cache_ttl_seconds`` (and on demand when an
    # unknown ``kid`` shows up, e.g. right after a rotation).
    identity_jwks_cache_ttl_seconds: int = Field(default=300)

    # v0.4 — pluggable storage driver. ``sqlite`` is the default to keep
    # every existing deployment unaffected; switch to ``postgres`` and supply
    # ``database_url`` for production scale-out.
    storage_driver: Literal["sqlite", "postgres"] = Field(default="sqlite")
    database_url: str = Field(default="")
    workspace_database_url: str = Field(default="")
    db_pool_min_size: int = Field(default=5)
    db_pool_max_size: int = Field(default=20)

    # v0.5 — lease reaper for the durable workflow executor. The reaper
    # is enabled by default but always opt-in to "workers actually
    # exist": the v0.2 in-process workflow path never creates leases, so
    # the reaper is a no-op when no workers are running.
    lease_reaper_enabled: bool = Field(default=True)
    lease_reaper_interval_seconds: int = Field(default=30, ge=1)
    worker_inactive_timeout_seconds: int = Field(default=300, ge=1)

    # v0.5 — schema migration framework. When True (default) the service
    # applies any pending migrations on startup before serving requests.
    # Set to False in environments where migration application is gated by
    # an operator (CI/CD pipeline, blue/green deploy). When False the
    # service still starts but emits a WARNING per pending migration.
    auto_migrate: bool = Field(default=True)

    # v0.5 — load-shedding middleware. Default disabled so existing
    # deployments are unaffected. When ``load_shed_enabled = True``, each
    # request acquires a slot from a bounded inflight + queue tracker;
    # over-capacity requests get a 503 with ``Retry-After`` instead of
    # piling up and exhausting memory.
    load_shed_enabled: bool = Field(default=False)
    load_shed_max_inflight: int = Field(default=200, ge=1)
    load_shed_max_queue: int = Field(default=1000, ge=0)
    load_shed_retry_after_seconds: int = Field(default=1, ge=0)

    @property
    def effective_database_url(self) -> str:
        """Return the workspace-specific URL if set, else the shared one."""

        return self.workspace_database_url or self.database_url

    @property
    def db_path(self) -> Path:
        """Absolute path to the SQLite database file."""
        return self.data_dir / "workspace.db"

    @property
    def blobs_dir(self) -> Path:
        """Root directory holding content-addressed blobs."""
        return self.data_dir / "blobs"

    @property
    def jwt_secret_value(self) -> str | None:
        """Effective HS256 secret for ``verify_local`` mode.

        Returns the configured ``identity_jwt_secret`` if any. We don't
        auto-generate one here — the workspace is a verifier, not an issuer.
        """

        return self.identity_jwt_secret


def get_settings(**overrides: object) -> Settings:
    """Construct a fresh :class:`Settings`.

    Tests can pass overrides directly to bypass env vars without touching
    the global state.
    """

    return Settings(**overrides)  # type: ignore[arg-type]
