# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 The Plinth Authors
"""Per-tenant resource quotas storage + Pydantic models.

The quota envelope lives in Identity (the source of truth for tenancy) and
is consulted by Workspace + Gateway when accepting create/invoke calls.
Storage uses the ``tenant_quotas`` table seeded by ``init_db``; "no row"
means the tenant gets the contract defaults — :func:`default_quotas` is the
single source of those defaults so the Pydantic model, the SQL DEFAULTs,
and the SDK are guaranteed to agree.

This module also exposes :class:`TenantQuotas` and :class:`TenantUsage`
Pydantic models reused across the FastAPI routes and SDK clients.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Optional  # noqa: UP035

from pydantic import BaseModel, ConfigDict, Field

from .store import connect

UTC = timezone.utc  # noqa: UP017


# ---------------------------------------------------------------------------
# Default values (mirrors CONTRACTS.md → Per-Tenant Resource Quotas).

DEFAULT_MAX_WORKSPACES = 100
DEFAULT_MAX_STORAGE_GB = 10.0
DEFAULT_MAX_CHANNELS_PER_WORKSPACE = 50
DEFAULT_MAX_WORKFLOWS_PER_WORKSPACE = 100
DEFAULT_MAX_ACTIVE_TOKENS = 1000
DEFAULT_MAX_OAUTH_CONNECTIONS = 50
DEFAULT_MAX_COST_USD_DAY = 100.0
DEFAULT_MAX_COST_USD_MONTH = 2000.0
DEFAULT_MAX_INVOCATIONS_PER_MINUTE = 600


# ---------------------------------------------------------------------------
# Models


class TenantQuotas(BaseModel):
    """Quota envelope for a single tenant.

    Defaults match the contract — large enough that v0.x demos that hammer
    endpoints don't suddenly start hitting quota walls.
    """

    model_config = ConfigDict(extra="ignore")

    tenant_id: str
    max_workspaces: int = Field(default=DEFAULT_MAX_WORKSPACES, ge=0)
    max_storage_gb: float = Field(default=DEFAULT_MAX_STORAGE_GB, ge=0.0)
    max_channels_per_workspace: int = Field(
        default=DEFAULT_MAX_CHANNELS_PER_WORKSPACE, ge=0
    )
    max_workflows_per_workspace: int = Field(
        default=DEFAULT_MAX_WORKFLOWS_PER_WORKSPACE, ge=0
    )
    max_active_tokens: int = Field(default=DEFAULT_MAX_ACTIVE_TOKENS, ge=0)
    max_oauth_connections: int = Field(default=DEFAULT_MAX_OAUTH_CONNECTIONS, ge=0)
    max_cost_usd_day: float = Field(default=DEFAULT_MAX_COST_USD_DAY, ge=0.0)
    max_cost_usd_month: float = Field(default=DEFAULT_MAX_COST_USD_MONTH, ge=0.0)
    max_invocations_per_minute: int = Field(
        default=DEFAULT_MAX_INVOCATIONS_PER_MINUTE, ge=0
    )
    updated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class TenantQuotasUpdate(BaseModel):
    """Body of ``POST /v1/tenants/{tenant_id}/quotas``.

    All fields are optional so callers can patch a single value without
    re-stating the rest. Unset fields fall back to the existing row (or
    the contract defaults if no row exists).
    """

    model_config = ConfigDict(extra="forbid")

    max_workspaces: Optional[int] = Field(default=None, ge=0)  # noqa: UP045
    max_storage_gb: Optional[float] = Field(default=None, ge=0.0)  # noqa: UP045
    max_channels_per_workspace: Optional[int] = Field(default=None, ge=0)  # noqa: UP045
    max_workflows_per_workspace: Optional[int] = Field(default=None, ge=0)  # noqa: UP045
    max_active_tokens: Optional[int] = Field(default=None, ge=0)  # noqa: UP045
    max_oauth_connections: Optional[int] = Field(default=None, ge=0)  # noqa: UP045
    max_cost_usd_day: Optional[float] = Field(default=None, ge=0.0)  # noqa: UP045
    max_cost_usd_month: Optional[float] = Field(default=None, ge=0.0)  # noqa: UP045
    max_invocations_per_minute: Optional[int] = Field(default=None, ge=0)  # noqa: UP045


class TenantUsage(BaseModel):
    """Computed usage rollup for a single tenant.

    Some fields (``storage_gb``, ``cost_usd_day``, ``cost_usd_month``,
    ``last_invocation_at``) are owned by other services (workspace,
    gateway). Identity reports them as ``0`` / ``None`` and a ``notes``
    map of cross-service usage that v1.0 doesn't aggregate yet — operators
    should query the relevant service for accurate numbers.
    """

    model_config = ConfigDict(extra="ignore")

    tenant_id: str
    workspaces: int = 0
    storage_gb: float = 0.0
    active_tokens: int = 0
    oauth_connections: int = 0
    cost_usd_day: float = 0.0
    cost_usd_month: float = 0.0
    last_invocation_at: Optional[datetime] = None  # noqa: UP045
    notes: dict[str, str] = Field(default_factory=dict)


# ---------------------------------------------------------------------------
# Helpers


def default_quotas(tenant_id: str) -> TenantQuotas:
    """Return a :class:`TenantQuotas` populated with contract defaults."""

    return TenantQuotas(tenant_id=tenant_id)


def _row_to_quotas(row, tenant_id: str) -> TenantQuotas:
    return TenantQuotas(
        tenant_id=tenant_id,
        max_workspaces=int(row["max_workspaces"]),
        max_storage_gb=float(row["max_storage_gb"]),
        max_channels_per_workspace=int(row["max_channels_per_workspace"]),
        max_workflows_per_workspace=int(row["max_workflows_per_workspace"]),
        max_active_tokens=int(row["max_active_tokens"]),
        max_oauth_connections=int(row["max_oauth_connections"]),
        max_cost_usd_day=float(row["max_cost_usd_day"]),
        max_cost_usd_month=float(row["max_cost_usd_month"]),
        max_invocations_per_minute=int(row["max_invocations_per_minute"]),
        updated_at=_parse_ts(row["updated_at"]),
    )


def _parse_ts(value: object) -> datetime:
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=UTC)
    parsed = datetime.fromisoformat(str(value))
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


# ---------------------------------------------------------------------------
# Storage


class QuotaStore:
    """CRUD against ``tenant_quotas`` + lightweight usage queries.

    The store is intentionally a thin wrapper over SQL — the same contract
    is exposed by Postgres deployments via the same pythoneic API since
    we use ``aiosqlite``-style row access.
    """

    def __init__(self, db_path: Path) -> None:
        self._db_path = db_path

    @property
    def db_path(self) -> Path:
        return self._db_path

    # ------------------------------------------------------------------ get

    async def get(self, tenant_id: str) -> TenantQuotas:
        """Return the stored quotas for ``tenant_id`` or contract defaults.

        Per-spec: a tenant without an explicit row returns defaults — never
        404. This makes the workspace/gateway quota fetch path branchless.
        """

        async with connect(self._db_path) as conn:
            cur = await conn.execute(
                "SELECT * FROM tenant_quotas WHERE tenant_id=?",
                (tenant_id,),
            )
            row = await cur.fetchone()
            await cur.close()
        if row is None:
            return default_quotas(tenant_id)
        return _row_to_quotas(row, tenant_id)

    # ------------------------------------------------------------------ set

    async def set(
        self,
        tenant_id: str,
        update: TenantQuotasUpdate,
    ) -> TenantQuotas:
        """Upsert the quota row.

        Unset fields on ``update`` fall back to the existing row, otherwise
        to the contract defaults. Returns the fully-resolved :class:`TenantQuotas`.

        Race-safety: SQLite serialises writes per-database so this is safe
        under concurrent updates. The combined SELECT + UPSERT is wrapped
        in a transaction (``BEGIN IMMEDIATE``) so simultaneous patches
        don't drop fields.
        """

        existing = await self.get(tenant_id)
        merged = TenantQuotas(
            tenant_id=tenant_id,
            max_workspaces=(
                update.max_workspaces
                if update.max_workspaces is not None
                else existing.max_workspaces
            ),
            max_storage_gb=(
                update.max_storage_gb
                if update.max_storage_gb is not None
                else existing.max_storage_gb
            ),
            max_channels_per_workspace=(
                update.max_channels_per_workspace
                if update.max_channels_per_workspace is not None
                else existing.max_channels_per_workspace
            ),
            max_workflows_per_workspace=(
                update.max_workflows_per_workspace
                if update.max_workflows_per_workspace is not None
                else existing.max_workflows_per_workspace
            ),
            max_active_tokens=(
                update.max_active_tokens
                if update.max_active_tokens is not None
                else existing.max_active_tokens
            ),
            max_oauth_connections=(
                update.max_oauth_connections
                if update.max_oauth_connections is not None
                else existing.max_oauth_connections
            ),
            max_cost_usd_day=(
                update.max_cost_usd_day
                if update.max_cost_usd_day is not None
                else existing.max_cost_usd_day
            ),
            max_cost_usd_month=(
                update.max_cost_usd_month
                if update.max_cost_usd_month is not None
                else existing.max_cost_usd_month
            ),
            max_invocations_per_minute=(
                update.max_invocations_per_minute
                if update.max_invocations_per_minute is not None
                else existing.max_invocations_per_minute
            ),
            updated_at=datetime.now(UTC),
        )

        async with connect(self._db_path) as conn:
            await conn.execute("BEGIN IMMEDIATE")
            try:
                await conn.execute(
                    """
                    INSERT INTO tenant_quotas (
                        tenant_id,
                        max_workspaces, max_storage_gb,
                        max_channels_per_workspace, max_workflows_per_workspace,
                        max_active_tokens, max_oauth_connections,
                        max_cost_usd_day, max_cost_usd_month,
                        max_invocations_per_minute, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(tenant_id) DO UPDATE SET
                        max_workspaces = excluded.max_workspaces,
                        max_storage_gb = excluded.max_storage_gb,
                        max_channels_per_workspace = excluded.max_channels_per_workspace,
                        max_workflows_per_workspace = excluded.max_workflows_per_workspace,
                        max_active_tokens = excluded.max_active_tokens,
                        max_oauth_connections = excluded.max_oauth_connections,
                        max_cost_usd_day = excluded.max_cost_usd_day,
                        max_cost_usd_month = excluded.max_cost_usd_month,
                        max_invocations_per_minute = excluded.max_invocations_per_minute,
                        updated_at = excluded.updated_at
                    """,
                    (
                        tenant_id,
                        merged.max_workspaces,
                        merged.max_storage_gb,
                        merged.max_channels_per_workspace,
                        merged.max_workflows_per_workspace,
                        merged.max_active_tokens,
                        merged.max_oauth_connections,
                        merged.max_cost_usd_day,
                        merged.max_cost_usd_month,
                        merged.max_invocations_per_minute,
                        merged.updated_at.isoformat(),
                    ),
                )
                await conn.commit()
            except Exception:
                await conn.rollback()
                raise
        return merged

    # ----------------------------------------------------------------- delete

    async def delete(self, tenant_id: str) -> bool:
        """Drop the quota row, reverting the tenant to defaults.

        Returns True if a row was deleted, False if nothing was there.
        """

        async with connect(self._db_path) as conn:
            cur = await conn.execute(
                "DELETE FROM tenant_quotas WHERE tenant_id=?",
                (tenant_id,),
            )
            await conn.commit()
            removed = cur.rowcount or 0
            await cur.close()
        return removed > 0

    # ------------------------------------------------------------------ usage

    async def usage(self, tenant_id: str) -> TenantUsage:
        """Compute the per-tenant usage rollup.

        Identity owns ``active_tokens`` (count of non-revoked, non-expired
        rows in ``issued_tokens``). All other counters live in other
        services so we surface them as zero with a ``notes`` entry pointing
        at the canonical source. v1.0 known limitation — see CONTRACTS.md.
        """

        active_tokens = 0
        async with connect(self._db_path) as conn:
            now = datetime.now(UTC).isoformat()
            cur = await conn.execute(
                "SELECT COUNT(*) AS c FROM issued_tokens "
                "WHERE tenant_id=? AND revoked=0 AND expires_at > ?",
                (tenant_id, now),
            )
            row = await cur.fetchone()
            if row is not None:
                active_tokens = int(row["c"])
            await cur.close()

        return TenantUsage(
            tenant_id=tenant_id,
            workspaces=0,
            storage_gb=0.0,
            active_tokens=active_tokens,
            oauth_connections=0,
            cost_usd_day=0.0,
            cost_usd_month=0.0,
            last_invocation_at=None,
            notes={
                "workspaces": "owned by workspace service",
                "storage_gb": "owned by workspace service",
                "oauth_connections": "owned by gateway service",
                "cost_usd_day": "owned by gateway service (audit_events)",
                "cost_usd_month": "owned by gateway service (audit_events)",
                "last_invocation_at": "owned by gateway service",
            },
        )


__all__ = [
    "DEFAULT_MAX_ACTIVE_TOKENS",
    "DEFAULT_MAX_CHANNELS_PER_WORKSPACE",
    "DEFAULT_MAX_COST_USD_DAY",
    "DEFAULT_MAX_COST_USD_MONTH",
    "DEFAULT_MAX_INVOCATIONS_PER_MINUTE",
    "DEFAULT_MAX_OAUTH_CONNECTIONS",
    "DEFAULT_MAX_STORAGE_GB",
    "DEFAULT_MAX_WORKFLOWS_PER_WORKSPACE",
    "DEFAULT_MAX_WORKSPACES",
    "QuotaStore",
    "TenantQuotas",
    "TenantQuotasUpdate",
    "TenantUsage",
    "default_quotas",
]
