# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 The Plinth Authors
"""Runtime configuration for the workspace service.

All values are read from environment variables prefixed with ``PLINTH_``.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Literal, Optional  # noqa: UP035

from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict
from pydantic_settings.sources import EnvSettingsSource


AuthMode = Literal["permissive", "verify_local", "verify_remote"]
ReplicationMode = Literal["primary", "replica", "standalone"]


# v1.0 — multi-region. Pydantic-settings tries to JSON-decode any list-typed
# env var; CONTRACTS.md spec'd ``PLINTH_REGION_PEERS=us,eu`` as comma-
# separated, so we route that one field through a custom decoder.
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

    # v0.6 — federated revocation cache. Polls the identity service's
    # ``GET /v1/revocations`` endpoint every ``revocation_poll_interval_seconds``
    # so a token revoked on a peer replica is rejected here within the poll
    # window. ``revocation_poll_url=""`` (default) keeps the cache disabled —
    # which is correct for single-node deployments and v0.5 demos that
    # already rely on Identity's local cache.
    revocation_poll_url: str = Field(default="")
    revocation_poll_interval_seconds: int = Field(default=60, ge=1)
    revocation_poll_enabled: bool = Field(default=True)

    # v1.0 — multi-region scaffolding. ``standalone`` (the default) keeps
    # behaviour identical to v0.6 — replication is opt-in. Only flip to
    # ``primary`` or ``replica`` when you have peers configured. See
    # ``docs/multi-region.md`` for the full operator playbook.
    region_id: str = Field(default="default")
    # ``region_peers`` parses a comma-separated env var into a deduped list
    # of peer region ids: ``PLINTH_REGION_PEERS=us-east-1,ap-south-1``.
    region_peers: list[str] = Field(default_factory=list)
    # Per-peer URL — populated from any env var matching
    # ``PLINTH_REGION_PEER_<ID>_URL``. The model_validator below scrapes
    # ``os.environ`` because pydantic-settings' nested-model parsing can't
    # express "every env var with this prefix is a dict key".
    region_peer_urls: dict[str, str] = Field(default_factory=dict)
    replication_mode: ReplicationMode = Field(default="standalone")
    # Public URL used by replicas in the ``X-Plinth-Primary-Region`` header
    # they emit on 409s. When empty we just emit the region_id; the client
    # is then expected to look up the URL from its own fallback map.
    region_primary_url: str = Field(default="")
    # Cache TTL for ``/v1/regions`` peer status. The endpoint pings every
    # peer (HEAD ``/healthz``) on a cold cache, then serves from memory
    # until ``regions_status_cache_ttl_seconds`` expires.
    regions_status_cache_ttl_seconds: int = Field(default=30, ge=1)
    regions_status_probe_timeout_seconds: float = Field(default=2.0, ge=0.1)

    # v1.0 — per-tenant resource quotas. Default ``False`` so existing v0.6
    # demos that hammer endpoints don't suddenly hit quota walls; flip to
    # True in production after sizing the limits in identity.
    quotas_enabled: bool = Field(default=False)
    quotas_cache_ttl_seconds: int = Field(default=60, ge=0)
    quotas_fetch_timeout_seconds: float = Field(default=2.0, ge=0.1)

    # v1.1 — pluggable coordination backend (see ``coordination.py``).
    # ``memory`` (default) keeps v1.0 single-process behaviour. Switch to
    # ``redis`` for cluster-shared lease coordination + a shared
    # revocation cache across workspace replicas.
    coordination_backend: Literal["memory", "redis"] = Field(default="memory")
    coordination_redis_url: str = Field(default="redis://localhost:6379/0")
    coordination_key_prefix: str = Field(default="plinth")

    @field_validator("region_peers", mode="before")
    @classmethod
    def _coerce_region_peers(cls, value: object) -> object:
        """Allow ``["us","eu"]``-style direct overrides + comma strings.

        The custom :class:`_PlinthEnvSource` already handles env-var
        decoding; this catches ``Settings(region_peers="us,eu")``-style
        constructor calls (used by tests).
        """

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
        """Wire in :class:`_PlinthEnvSource` so comma-separated peers parse."""

        custom_env = _PlinthEnvSource(settings_cls)
        return init_settings, custom_env, dotenv_settings, file_secret_settings

    @model_validator(mode="after")
    def _scrape_region_peer_urls(self) -> Settings:
        """Pick up ``PLINTH_REGION_PEER_<ID>_URL`` env vars.

        Shell env var names can't contain dashes, so the operator-side
        convention is ``PLINTH_REGION_PEER_US_EAST_1_URL`` → peer id
        ``us-east-1`` (underscores back to dashes). The ``region_peers``
        list is the canonical naming source: when one of the parsed env
        var ids exactly matches a declared peer id with underscores
        replaced, we use the dashed form.

        Only fills in keys that aren't already populated from explicit
        constructor overrides — tests can pass ``region_peer_urls={...}``
        without env-var interference.
        """

        prefix = "PLINTH_REGION_PEER_"
        suffix = "_URL"
        # Cross-reference declared peers so we restore dashes correctly.
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
