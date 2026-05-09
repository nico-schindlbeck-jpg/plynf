# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 The Plinth Authors
"""Per-tenant quota enforcement helpers (v1.0).

This module owns three things:

1. :class:`QuotaCache` — fetches :class:`TenantQuotas` from Identity and
   caches the result for ``ttl_seconds`` per tenant. Network calls are
   sync httpx (we run in a thread executor inside async code) so a flaky
   identity service degrades to "use cached or default" instead of
   blocking the whole request path.

2. :class:`QuotaEnforcer` — combines the cache with a counter callback
   to raise :class:`QuotaExceeded` when the would-be-new state would
   cross the limit. The counter is async because every backing query
   already is, and we never want enforcement to block the loop.

3. :func:`tenant_storage_bytes` / :func:`workspace_channel_count` /
   :func:`workspace_workflow_count` — small helpers that keep the SQL
   in one place so the API handlers stay readable.

The cache + enforcer are both opt-in via ``settings.quotas_enabled`` —
default False so existing v0.6 demos that hammer endpoints don't suddenly
hit quota walls. When enabled, identity-unreachable paths log a warning
and allow the operation (the spec calls this "degraded mode").
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx

from .db import connect
from .exceptions import PlinthError
from .logging_config import get_logger


# ---------------------------------------------------------------------------
# Defaults — kept in sync with services/identity/src/plinth_identity/quotas.py.
# A bit of duplication is the cheapest way to avoid a hard cross-service
# import dependency. The two are tested as the single source of truth.

DEFAULT_MAX_WORKSPACES = 100
DEFAULT_MAX_STORAGE_GB = 10.0
DEFAULT_MAX_CHANNELS_PER_WORKSPACE = 50
DEFAULT_MAX_WORKFLOWS_PER_WORKSPACE = 100


@dataclass(frozen=True)
class TenantQuotas:
    """Slim view of the identity ``TenantQuotas`` envelope.

    We don't import the identity model directly because the workspace
    service shouldn't take a build-time dep on identity. Construct via
    :func:`tenant_quotas_from_dict` from the JSON Identity returns.
    """

    tenant_id: str
    max_workspaces: int = DEFAULT_MAX_WORKSPACES
    max_storage_gb: float = DEFAULT_MAX_STORAGE_GB
    max_channels_per_workspace: int = DEFAULT_MAX_CHANNELS_PER_WORKSPACE
    max_workflows_per_workspace: int = DEFAULT_MAX_WORKFLOWS_PER_WORKSPACE


def tenant_quotas_from_dict(tenant_id: str, body: dict[str, Any]) -> TenantQuotas:
    return TenantQuotas(
        tenant_id=tenant_id,
        max_workspaces=int(body.get("max_workspaces", DEFAULT_MAX_WORKSPACES)),
        max_storage_gb=float(body.get("max_storage_gb", DEFAULT_MAX_STORAGE_GB)),
        max_channels_per_workspace=int(
            body.get("max_channels_per_workspace", DEFAULT_MAX_CHANNELS_PER_WORKSPACE)
        ),
        max_workflows_per_workspace=int(
            body.get("max_workflows_per_workspace", DEFAULT_MAX_WORKFLOWS_PER_WORKSPACE)
        ),
    )


# ---------------------------------------------------------------------------
# Exception


class QuotaExceeded(PlinthError):
    """Raised when a quota would be exceeded by the requested op.

    Maps to HTTP 429 with ``code=QUOTA_EXCEEDED``. ``Retry-After`` is
    deliberately omitted: the spec calls this a "long-term quota, not a
    rate-limit", so retrying immediately won't help.
    """

    code = "QUOTA_EXCEEDED"
    status_code = 429
    message = "tenant quota exceeded"

    def __init__(
        self,
        quota: str,
        *,
        tenant_id: str,
        current: float | int,
        limit: float | int,
    ) -> None:
        super().__init__(
            f"Quota {quota} exceeded for tenant {tenant_id!r}: "
            f"{current} >= {limit}",
            details={
                "quota": quota,
                "tenant_id": tenant_id,
                "current": current,
                "limit": limit,
            },
        )


# ---------------------------------------------------------------------------
# Cache


class QuotaCache:
    """Lazy-fetch + TTL-cache for tenant quotas served by Identity.

    Hits ``GET {identity_url}/v1/tenants/{tenant_id}/quotas`` and caches
    the parsed :class:`TenantQuotas` for ``ttl_seconds``. On any HTTP
    error we log a warning and fall back to ``DEFAULT`` quotas — the
    rest of the enforcement code is then a no-op for the request.

    Single-process; the cache is in-memory. That's fine: we only need
    sub-second consistency *across* identity instances, not across our
    own process. Real production deployments that scale workspace
    horizontally will have each replica re-fetch independently.
    """

    def __init__(
        self,
        identity_url: str,
        *,
        ttl_seconds: int = 60,
        timeout_seconds: float = 2.0,
        client: httpx.AsyncClient | None = None,
        time_fn=time.monotonic,
    ) -> None:
        self._identity_url = (identity_url or "").rstrip("/")
        self._ttl = max(0, int(ttl_seconds))
        self._timeout = float(timeout_seconds)
        self._client = client
        self._owns_client = client is None
        self._time = time_fn
        # Stores ``(quotas, fetched_at_monotonic)``.
        self._cache: dict[str, tuple[TenantQuotas, float]] = {}

    @property
    def identity_url(self) -> str:
        return self._identity_url

    async def aclose(self) -> None:
        if self._owns_client and self._client is not None:
            await self._client.aclose()
            self._client = None

    def _ensure_client(self) -> httpx.AsyncClient:
        if self._client is None:
            # Lazy: don't open a client until somebody actually needs one.
            self._client = httpx.AsyncClient(timeout=self._timeout)
            self._owns_client = True
        return self._client

    def _fresh(self, fetched_at: float) -> bool:
        if self._ttl == 0:
            return False
        return (self._time() - fetched_at) < self._ttl

    def invalidate(self, tenant_id: str | None = None) -> None:
        """Drop one (or all) cache entries — used after explicit set/reset."""

        if tenant_id is None:
            self._cache.clear()
        else:
            self._cache.pop(tenant_id, None)

    async def get(self, tenant_id: str) -> TenantQuotas:
        """Return the cached :class:`TenantQuotas` for ``tenant_id``.

        On cache miss / expiry: hits identity. On identity error: returns
        defaults (and does NOT poison the cache, so a transient network
        blip resolves on the next call). On success: caches the result.
        """

        cached = self._cache.get(tenant_id)
        if cached is not None and self._fresh(cached[1]):
            return cached[0]

        if not self._identity_url:
            # No identity configured — defaults forever, no network call.
            quotas = TenantQuotas(tenant_id=tenant_id)
            self._cache[tenant_id] = (quotas, self._time())
            return quotas

        try:
            client = self._ensure_client()
            resp = await client.get(
                f"{self._identity_url}/v1/tenants/{tenant_id}/quotas",
                timeout=self._timeout,
            )
            resp.raise_for_status()
            data = resp.json()
        except (httpx.HTTPError, ValueError) as exc:
            get_logger().warning(
                "workspace.quotas.fetch_failed",
                tenant_id=tenant_id,
                identity_url=self._identity_url,
                error=str(exc),
            )
            # Degraded mode: use defaults, don't cache. A transient blip
            # resolves on the next fetch.
            return TenantQuotas(tenant_id=tenant_id)

        quotas = tenant_quotas_from_dict(tenant_id, data)
        self._cache[tenant_id] = (quotas, self._time())
        return quotas


# ---------------------------------------------------------------------------
# Counter helpers — keep the SQL in one place.


async def tenant_workspace_count(db_path: Path, tenant_id: str) -> int:
    async with connect(db_path) as conn:
        cur = await conn.execute(
            "SELECT COUNT(*) AS c FROM workspaces WHERE tenant_id=?",
            (tenant_id,),
        )
        row = await cur.fetchone()
        await cur.close()
    return int(row["c"]) if row is not None else 0


async def workspace_channel_count(db_path: Path, workspace_id: str) -> int:
    async with connect(db_path) as conn:
        cur = await conn.execute(
            "SELECT COUNT(*) AS c FROM channels WHERE workspace_id=?",
            (workspace_id,),
        )
        row = await cur.fetchone()
        await cur.close()
    return int(row["c"]) if row is not None else 0


async def workspace_has_channel(db_path: Path, workspace_id: str, name: str) -> bool:
    async with connect(db_path) as conn:
        cur = await conn.execute(
            "SELECT 1 FROM channels WHERE workspace_id=? AND name=? LIMIT 1",
            (workspace_id, name),
        )
        row = await cur.fetchone()
        await cur.close()
    return row is not None


async def workspace_workflow_count(db_path: Path, workspace_id: str) -> int:
    async with connect(db_path) as conn:
        cur = await conn.execute(
            "SELECT COUNT(*) AS c FROM workflows WHERE workspace_id=?",
            (workspace_id,),
        )
        row = await cur.fetchone()
        await cur.close()
    return int(row["c"]) if row is not None else 0


async def tenant_storage_bytes(db_path: Path, tenant_id: str) -> int:
    """Sum of file_entries.size for all live (non-deleted) versions in the tenant."""

    async with connect(db_path) as conn:
        cur = await conn.execute(
            """
            SELECT COALESCE(SUM(fe.size), 0) AS total
            FROM file_entries AS fe
            JOIN workspaces AS w ON w.id = fe.workspace_id
            WHERE w.tenant_id = ? AND fe.deleted = 0
            """,
            (tenant_id,),
        )
        row = await cur.fetchone()
        await cur.close()
    return int(row["total"]) if row is not None else 0


# ---------------------------------------------------------------------------
# Enforcer


class QuotaEnforcer:
    """Composition: cache + counters + the enabled flag.

    All ``check_*`` methods are no-ops when ``self.enabled`` is False so
    callers can wire them in unconditionally and we keep the v0.6 demos
    quota-free by default.
    """

    def __init__(
        self,
        cache: QuotaCache,
        db_path: Path,
        *,
        enabled: bool = False,
    ) -> None:
        self._cache = cache
        self._db_path = db_path
        self.enabled = bool(enabled)

    @property
    def cache(self) -> QuotaCache:
        return self._cache

    async def aclose(self) -> None:
        await self._cache.aclose()

    async def check_workspace_create(self, tenant_id: str) -> None:
        if not self.enabled:
            return
        quotas = await self._cache.get(tenant_id)
        current = await tenant_workspace_count(self._db_path, tenant_id)
        if current >= quotas.max_workspaces:
            raise QuotaExceeded(
                "max_workspaces",
                tenant_id=tenant_id,
                current=current,
                limit=quotas.max_workspaces,
            )

    async def check_channel_autocreate(
        self,
        tenant_id: str,
        workspace_id: str,
        channel_name: str,
    ) -> None:
        if not self.enabled:
            return
        # Only enforce on first send (i.e. when the channel doesn't exist
        # yet). Subsequent sends to an existing channel never count.
        if await workspace_has_channel(self._db_path, workspace_id, channel_name):
            return
        quotas = await self._cache.get(tenant_id)
        current = await workspace_channel_count(self._db_path, workspace_id)
        if current >= quotas.max_channels_per_workspace:
            raise QuotaExceeded(
                "max_channels_per_workspace",
                tenant_id=tenant_id,
                current=current,
                limit=quotas.max_channels_per_workspace,
            )

    async def check_workflow_create(
        self,
        tenant_id: str,
        workspace_id: str,
    ) -> None:
        if not self.enabled:
            return
        quotas = await self._cache.get(tenant_id)
        current = await workspace_workflow_count(self._db_path, workspace_id)
        if current >= quotas.max_workflows_per_workspace:
            raise QuotaExceeded(
                "max_workflows_per_workspace",
                tenant_id=tenant_id,
                current=current,
                limit=quotas.max_workflows_per_workspace,
            )

    async def check_file_storage(
        self,
        tenant_id: str,
        new_bytes: int,
    ) -> None:
        if not self.enabled:
            return
        quotas = await self._cache.get(tenant_id)
        current_bytes = await tenant_storage_bytes(self._db_path, tenant_id)
        projected_gb = (current_bytes + max(0, int(new_bytes))) / (1024 ** 3)
        if projected_gb > quotas.max_storage_gb:
            raise QuotaExceeded(
                "max_storage_gb",
                tenant_id=tenant_id,
                current=round(projected_gb, 6),
                limit=quotas.max_storage_gb,
            )


__all__ = [
    "DEFAULT_MAX_CHANNELS_PER_WORKSPACE",
    "DEFAULT_MAX_STORAGE_GB",
    "DEFAULT_MAX_WORKFLOWS_PER_WORKSPACE",
    "DEFAULT_MAX_WORKSPACES",
    "QuotaCache",
    "QuotaEnforcer",
    "QuotaExceeded",
    "TenantQuotas",
    "tenant_quotas_from_dict",
    "tenant_storage_bytes",
    "tenant_workspace_count",
    "workspace_channel_count",
    "workspace_has_channel",
    "workspace_workflow_count",
]
