# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 The Plynf Authors
"""Postgres-backed savings event sink.

Replaces the JSONL fallback for production deployments. Reuses asyncpg
(already a workspace dependency) and creates its own table so the proxy
service doesn't share write paths with workspace/identity.

Schema:

    CREATE TABLE IF NOT EXISTS proxy_savings_events (
      id BIGSERIAL PRIMARY KEY,
      ts DOUBLE PRECISION NOT NULL,
      tenant_id TEXT NOT NULL,
      agent_id TEXT,
      connector TEXT NOT NULL,
      tool TEXT NOT NULL,
      model TEXT NOT NULL,
      raw_response_tokens INTEGER NOT NULL,
      shaped_response_tokens INTEGER NOT NULL,
      saved_tokens INTEGER NOT NULL,
      cache_hit BOOLEAN NOT NULL,
      request_hash TEXT NOT NULL,
      workflow_id TEXT,
      cost_saved_usd DOUBLE PRECISION NOT NULL
    );

Tool-response bodies are NEVER persisted. Only counts, hashes, and a few
identifiers. That's the security promise on the website — enforced here.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from .savings import SavingsEvent

log = logging.getLogger("plinth.proxy.postgres")


SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS proxy_savings_events (
  id BIGSERIAL PRIMARY KEY,
  ts DOUBLE PRECISION NOT NULL,
  tenant_id TEXT NOT NULL,
  agent_id TEXT,
  connector TEXT NOT NULL,
  tool TEXT NOT NULL,
  model TEXT NOT NULL,
  raw_response_tokens INTEGER NOT NULL,
  shaped_response_tokens INTEGER NOT NULL,
  saved_tokens INTEGER NOT NULL,
  cache_hit BOOLEAN NOT NULL,
  request_hash TEXT NOT NULL,
  workflow_id TEXT,
  cost_saved_usd DOUBLE PRECISION NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_pse_tenant_ts
    ON proxy_savings_events (tenant_id, ts DESC);
CREATE INDEX IF NOT EXISTS idx_pse_connector_ts
    ON proxy_savings_events (connector, ts DESC);
"""

INSERT_SQL = """
INSERT INTO proxy_savings_events (
    ts, tenant_id, agent_id, connector, tool, model,
    raw_response_tokens, shaped_response_tokens, saved_tokens,
    cache_hit, request_hash, workflow_id, cost_saved_usd
) VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13)
"""

AGGREGATE_SQL = """
SELECT
    COUNT(*)::BIGINT                            AS total_calls,
    COALESCE(SUM(raw_response_tokens), 0)::BIGINT     AS total_raw_tokens,
    COALESCE(SUM(shaped_response_tokens), 0)::BIGINT  AS total_shaped_tokens,
    COALESCE(SUM(saved_tokens), 0)::BIGINT             AS total_saved_tokens,
    COALESCE(SUM(cost_saved_usd), 0)::DOUBLE PRECISION AS total_cost_saved_usd,
    COALESCE(AVG(CASE WHEN cache_hit THEN 1.0 ELSE 0.0 END), 0)::DOUBLE PRECISION
                                                        AS cache_hit_rate
FROM proxy_savings_events
WHERE tenant_id = $1
"""

TOP_CONNECTORS_SQL = """
SELECT connector, SUM(saved_tokens)::BIGINT AS saved
FROM proxy_savings_events
WHERE tenant_id = $1
GROUP BY connector
ORDER BY saved DESC
LIMIT 10
"""


def _row_args(e: SavingsEvent) -> tuple:
    """Map a SavingsEvent to a positional argument tuple for INSERT_SQL."""
    return (
        e.ts,
        e.tenant_id,
        e.agent_id,
        e.connector,
        e.tool,
        e.model,
        int(e.raw_response_tokens),
        int(e.shaped_response_tokens),
        int(e.saved_tokens),
        bool(e.cache_hit),
        e.request_hash,
        e.workflow_id,
        float(e.cost_saved_usd()),
    )


@dataclass
class PostgresSavingsSink:
    """Async Postgres sink. Lazily acquires a pool on first use."""

    dsn: str
    _pool: Any = field(default=None, init=False, repr=False)

    async def _ensure_pool(self):
        if self._pool is not None:
            return self._pool
        try:
            import asyncpg  # type: ignore[import-not-found]
        except ImportError as e:  # pragma: no cover - dep advertised in pyproject
            raise RuntimeError(
                "asyncpg is required for the Postgres sink; "
                "install with `pip install 'asyncpg>=0.29'`."
            ) from e
        # Normalise SQLAlchemy-style DSNs the workspace uses.
        dsn = self.dsn
        if dsn.startswith("postgresql+asyncpg://"):
            dsn = "postgresql://" + dsn[len("postgresql+asyncpg://") :]
        self._pool = await asyncpg.create_pool(dsn=dsn, min_size=1, max_size=10)
        async with self._pool.acquire() as conn:
            await conn.execute(SCHEMA_SQL)
        return self._pool

    async def emit_async(self, event: SavingsEvent) -> None:
        pool = await self._ensure_pool()
        async with pool.acquire() as conn:
            await conn.execute(INSERT_SQL, *_row_args(event))

    def emit(self, event: SavingsEvent) -> None:
        """Sync shim for compatibility with the existing SavingsSink interface.

        Schedules the async write on the running event loop. The proxy runs
        under uvicorn so there's always a loop; for non-async callers, see
        ``emit_async`` directly.
        """
        import asyncio

        try:
            loop = asyncio.get_event_loop()
        except RuntimeError:
            # No loop — synchronously block. Acceptable for one-off scripts.
            asyncio.run(self.emit_async(event))
            return
        if loop.is_running():
            asyncio.create_task(self.emit_async(event))
        else:
            loop.run_until_complete(self.emit_async(event))

    async def aggregate_for_tenant(self, tenant_id: str) -> dict[str, Any]:
        pool = await self._ensure_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(AGGREGATE_SQL, tenant_id)
            tops = await conn.fetch(TOP_CONNECTORS_SQL, tenant_id)
        out = dict(row) if row else {}
        out["top_connectors_by_savings"] = [
            (r["connector"], int(r["saved"])) for r in tops
        ]
        # Compute savings_pct here so callers don't repeat the math.
        total_raw = int(out.get("total_raw_tokens") or 0)
        total_saved = int(out.get("total_saved_tokens") or 0)
        out["savings_pct"] = round(total_saved / total_raw, 4) if total_raw else 0.0
        return out

    async def close(self) -> None:
        if self._pool is not None:
            await self._pool.close()
            self._pool = None


__all__ = ["PostgresSavingsSink", "SCHEMA_SQL", "INSERT_SQL", "_row_args"]
