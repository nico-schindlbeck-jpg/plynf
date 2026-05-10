# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 The Plinth Authors
"""Tenant-level quota enforcement on ``POST /v1/invoke`` (v1.0).

The gateway already enforces per-agent rate limits + cost caps via
:mod:`plinth_gateway.limits`. This module adds the tenant-level layer:

- ``cost_usd_day`` rolling sum vs ``quota.max_cost_usd_day``
- ``cost_usd_month`` rolling sum vs ``quota.max_cost_usd_month``
- requests/minute count vs ``quota.max_invocations_per_minute``

The first two reuse :func:`cost_caps.cost_used_in_window` against the
existing ``audit_events`` table — keyed by ``tenant_id`` instead of
``agent_id``. The third uses an in-memory cluster-wide token bucket
keyed by ``tenant_id``; one bucket per process is fine because we only
need rough fairness, not strict cluster-wide accounting (the audit
trail captures the ground truth).

All checks are no-ops when ``settings.quotas_enabled`` is False so v0.6
demos don't suddenly hit quota walls.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

import httpx

from .coordination import CoordinationBackend, MemoryBackend
from .db import Database
from .exceptions import GatewayError
from .logging_config import get_logger


UTC = timezone.utc


# ---------------------------------------------------------------------------
# Defaults — kept in sync with services/identity/src/plinth_identity/quotas.py.

DEFAULT_MAX_COST_USD_DAY = 100.0
DEFAULT_MAX_COST_USD_MONTH = 2000.0
DEFAULT_MAX_INVOCATIONS_PER_MINUTE = 600


# ---------------------------------------------------------------------------
# Exception


class QuotaExceeded(GatewayError):
    """Raised when a tenant-level quota would be exceeded.

    Maps to HTTP 429 with ``code=QUOTA_EXCEEDED`` to match the workspace
    error envelope. ``Retry-After`` is omitted: cost caps are long-term,
    not rate-limits.
    """

    code = "QUOTA_EXCEEDED"
    http_status = 429

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
# Models


@dataclass(frozen=True)
class TenantQuotas:
    """Slim view of identity ``TenantQuotas``.

    Constructed via :func:`tenant_quotas_from_dict` from the JSON Identity
    returns; defaults match the contract.
    """

    tenant_id: str
    max_cost_usd_day: float = DEFAULT_MAX_COST_USD_DAY
    max_cost_usd_month: float = DEFAULT_MAX_COST_USD_MONTH
    max_invocations_per_minute: int = DEFAULT_MAX_INVOCATIONS_PER_MINUTE


def tenant_quotas_from_dict(tenant_id: str, body: dict[str, Any]) -> TenantQuotas:
    return TenantQuotas(
        tenant_id=tenant_id,
        max_cost_usd_day=float(body.get("max_cost_usd_day", DEFAULT_MAX_COST_USD_DAY)),
        max_cost_usd_month=float(
            body.get("max_cost_usd_month", DEFAULT_MAX_COST_USD_MONTH)
        ),
        max_invocations_per_minute=int(
            body.get("max_invocations_per_minute", DEFAULT_MAX_INVOCATIONS_PER_MINUTE)
        ),
    )


# ---------------------------------------------------------------------------
# Cache (mirrors workspace/quotas.py:QuotaCache)


class QuotaCache:
    """Identity-quotas fetcher with TTL cache and degraded-mode fallback."""

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
            self._client = httpx.AsyncClient(timeout=self._timeout)
            self._owns_client = True
        return self._client

    def _fresh(self, fetched_at: float) -> bool:
        if self._ttl == 0:
            return False
        return (self._time() - fetched_at) < self._ttl

    def invalidate(self, tenant_id: str | None = None) -> None:
        if tenant_id is None:
            self._cache.clear()
        else:
            self._cache.pop(tenant_id, None)

    async def get(self, tenant_id: str) -> TenantQuotas:
        cached = self._cache.get(tenant_id)
        if cached is not None and self._fresh(cached[1]):
            return cached[0]

        if not self._identity_url:
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
                "gateway.quotas.fetch_failed",
                tenant_id=tenant_id,
                identity_url=self._identity_url,
                error=str(exc),
            )
            return TenantQuotas(tenant_id=tenant_id)

        quotas = tenant_quotas_from_dict(tenant_id, data)
        self._cache[tenant_id] = (quotas, self._time())
        return quotas


# ---------------------------------------------------------------------------
# Tenant token bucket — sliding-window counter, one per tenant.


@dataclass
class _TenantBucket:
    """Per-tenant invocation-count bucket.

    Records each invocation's monotonic timestamp; ``count_in_window``
    drops anything older than the window. Cheap for the common case
    (a few hundred entries per minute) and correct without resorting to
    timer goroutines.
    """

    timestamps: list[float] = field(default_factory=list)


class TenantInvocationBucket:
    """Cluster-wide-keyed (per-tenant) sliding window of invocations.

    The bucket is in-process: this gives accurate rate-limiting on a
    single-replica deployment and a soft fairness guarantee under
    horizontal scale-out. The audit trail remains the canonical billing
    source.
    """

    def __init__(self, *, time_fn=time.monotonic) -> None:
        self._buckets: dict[str, _TenantBucket] = {}
        self._lock = asyncio.Lock()
        self._time = time_fn

    async def count_in_window(
        self,
        tenant_id: str,
        *,
        window_seconds: float = 60.0,
    ) -> int:
        async with self._lock:
            bucket = self._buckets.get(tenant_id)
            if bucket is None:
                return 0
            cutoff = self._time() - window_seconds
            # Drop stale entries while we hold the lock.
            bucket.timestamps = [t for t in bucket.timestamps if t > cutoff]
            return len(bucket.timestamps)

    async def record(self, tenant_id: str) -> None:
        async with self._lock:
            bucket = self._buckets.setdefault(tenant_id, _TenantBucket())
            bucket.timestamps.append(self._time())

    async def reset(self, tenant_id: str | None = None) -> None:
        async with self._lock:
            if tenant_id is None:
                self._buckets.clear()
            else:
                self._buckets.pop(tenant_id, None)


# ---------------------------------------------------------------------------
# Cost helpers — sum audit_events by tenant_id


async def cost_used_by_tenant(
    db: Database,
    tenant_id: str,
    *,
    hours: int,
) -> float:
    if hours <= 0:
        return 0.0
    cutoff = datetime.now(UTC) - timedelta(hours=hours)
    row = await db.fetchone(
        "SELECT COALESCE(SUM(cost_estimate_usd), 0) AS s FROM audit_events "
        "WHERE tenant_id = ? AND timestamp >= ? AND cached = 0",
        (tenant_id, cutoff.isoformat()),
    )
    if row is None or row["s"] is None:
        return 0.0
    return float(row["s"])


# ---------------------------------------------------------------------------
# Enforcer


class TenantQuotaEnforcer:
    """Composition of :class:`QuotaCache`, the audit-cost helpers, and the
    per-tenant token bucket.

    All ``check_*`` methods are no-ops when ``self.enabled`` is False.
    """

    def __init__(
        self,
        cache: QuotaCache,
        db: Database,
        *,
        bucket: TenantInvocationBucket | None = None,
        enabled: bool = False,
        coordination: CoordinationBackend | None = None,
        coordination_prefix: str = "plinth",
    ) -> None:
        self._cache = cache
        self._db = db
        self._bucket = bucket or TenantInvocationBucket()
        self.enabled = bool(enabled)
        # v1.1 — coordination backend. Default ``MemoryBackend`` keeps
        # cost-cap enforcement on the v1.0 audit-table-rolling-sum path;
        # when ``RedisBackend`` is configured, ``record_invoke`` *also*
        # bumps a cost counter so cluster-wide caps surface immediately
        # without waiting for the next audit-events query (saves the
        # "noisy neighbour" race during a tenant's first burst).
        self._coordination: CoordinationBackend = coordination or MemoryBackend()
        self._key_prefix = (coordination_prefix or "plinth").rstrip(":")

    @property
    def cache(self) -> QuotaCache:
        return self._cache

    @property
    def bucket(self) -> TenantInvocationBucket:
        return self._bucket

    @property
    def coordination(self) -> CoordinationBackend:
        return self._coordination

    def _rpm_key(self, tenant_id: str) -> str:
        return f"{self._key_prefix}:tenant:{tenant_id}:invocations"

    def _cost_key(self, tenant_id: str, window: str) -> str:
        return f"{self._key_prefix}:tenant:{tenant_id}:cost:{window}"

    async def aclose(self) -> None:
        await self._cache.aclose()

    async def check_invoke(self, tenant_id: str) -> None:
        """Run all tenant-level checks for an invoke call.

        Order matters: we check the rpm bucket first (cheapest), then
        cost-day, then cost-month. The first violation raises
        :class:`QuotaExceeded` and short-circuits the rest.
        """

        if not self.enabled:
            return
        quotas = await self._cache.get(tenant_id)

        # 1) Invocations per minute. Local sliding-window bucket is the
        # fast path; the cluster-shared counter (Redis-only) is checked
        # on top so a multi-replica deployment sees true cluster-wide
        # rate-limits. ``MemoryBackend`` short-circuits the second check
        # because the local bucket already covers it.
        if quotas.max_invocations_per_minute > 0:
            current_rpm = await self._bucket.count_in_window(tenant_id)
            if current_rpm >= quotas.max_invocations_per_minute:
                raise QuotaExceeded(
                    "max_invocations_per_minute",
                    tenant_id=tenant_id,
                    current=current_rpm,
                    limit=quotas.max_invocations_per_minute,
                )
            if not isinstance(self._coordination, MemoryBackend):
                try:
                    cluster_value = await self._coordination.get(
                        self._rpm_key(tenant_id)
                    )
                    cluster_rpm = int(cluster_value or 0)
                    if cluster_rpm >= quotas.max_invocations_per_minute:
                        raise QuotaExceeded(
                            "max_invocations_per_minute",
                            tenant_id=tenant_id,
                            current=cluster_rpm,
                            limit=quotas.max_invocations_per_minute,
                        )
                except QuotaExceeded:
                    raise
                except Exception:  # noqa: BLE001 — never block on Redis
                    pass

        # 2) Rolling 24h cost.
        if quotas.max_cost_usd_day > 0:
            used_day = await cost_used_by_tenant(self._db, tenant_id, hours=24)
            if used_day >= quotas.max_cost_usd_day:
                raise QuotaExceeded(
                    "max_cost_usd_day",
                    tenant_id=tenant_id,
                    current=round(used_day, 4),
                    limit=quotas.max_cost_usd_day,
                )

        # 3) Rolling 30d (~"month") cost. We use 720h as a stable proxy so
        # the rolling window is independent of calendar month boundaries.
        if quotas.max_cost_usd_month > 0:
            used_month = await cost_used_by_tenant(
                self._db, tenant_id, hours=24 * 30
            )
            if used_month >= quotas.max_cost_usd_month:
                raise QuotaExceeded(
                    "max_cost_usd_month",
                    tenant_id=tenant_id,
                    current=round(used_month, 4),
                    limit=quotas.max_cost_usd_month,
                )

    async def record_invoke(self, tenant_id: str) -> None:
        """Record a successful invocation for the rpm bucket.

        Safe to call when enforcement is disabled (the bucket just
        accumulates entries that nothing will ever read).
        """

        await self._bucket.record(tenant_id)
        # v1.1 — also bump the cluster-shared counter when Redis is in use
        # so peer replicas see this invocation immediately. Best-effort.
        if not isinstance(self._coordination, MemoryBackend):
            try:
                await self._coordination.incr(
                    self._rpm_key(tenant_id),
                    amount=1,
                    ttl_seconds=60,
                )
            except Exception:  # noqa: BLE001
                pass

    async def record_cost(
        self,
        tenant_id: str,
        cost_micros: int,
        *,
        window_seconds: int = 3600,
    ) -> None:
        """Record cost in the cluster-shared rolling window.

        ``cost_micros`` is the cost in micro-USD (i.e. ``cost_usd * 1e6``)
        so the counter stays integer-valued. Best-effort — a Redis outage
        falls through silently and the audit-table sum (the canonical
        billing source) is unaffected.
        """

        if isinstance(self._coordination, MemoryBackend):
            return
        try:
            await self._coordination.incr(
                self._cost_key(tenant_id, "hour"),
                amount=int(cost_micros),
                ttl_seconds=int(window_seconds),
            )
        except Exception:  # noqa: BLE001
            pass


__all__ = [
    "DEFAULT_MAX_COST_USD_DAY",
    "DEFAULT_MAX_COST_USD_MONTH",
    "DEFAULT_MAX_INVOCATIONS_PER_MINUTE",
    "QuotaCache",
    "QuotaExceeded",
    "TenantInvocationBucket",
    "TenantQuotaEnforcer",
    "TenantQuotas",
    "cost_used_by_tenant",
    "tenant_quotas_from_dict",
]
