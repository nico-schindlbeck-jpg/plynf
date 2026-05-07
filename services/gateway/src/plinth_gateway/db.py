# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 The Plinth Authors
"""SQLite connection management and schema bootstrap."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

import aiosqlite

SCHEMA = """
CREATE TABLE IF NOT EXISTS tools (
  tool_id TEXT PRIMARY KEY,
  name TEXT NOT NULL,
  description TEXT NOT NULL,
  transport TEXT NOT NULL,
  endpoint TEXT NOT NULL,
  input_schema TEXT NOT NULL,
  output_schema TEXT NOT NULL,
  idempotent INTEGER NOT NULL DEFAULT 0,
  side_effects TEXT NOT NULL DEFAULT 'read',
  cache_ttl_seconds INTEGER,
  auth_method TEXT NOT NULL DEFAULT 'none',
  auth_config TEXT NOT NULL DEFAULT '{}',
  tenant_id TEXT NOT NULL DEFAULT 'default',
  created_at TIMESTAMP NOT NULL,
  updated_at TIMESTAMP NOT NULL
);

CREATE TABLE IF NOT EXISTS audit_events (
  id TEXT PRIMARY KEY,
  timestamp TIMESTAMP NOT NULL,
  tool_id TEXT NOT NULL,
  workspace_id TEXT,
  agent_id TEXT,
  tenant_id TEXT NOT NULL DEFAULT 'default',
  arguments_hash TEXT NOT NULL,
  arguments_preview TEXT,
  result_hash TEXT,
  cached INTEGER NOT NULL DEFAULT 0,
  duration_ms INTEGER NOT NULL,
  cost_estimate_usd REAL NOT NULL DEFAULT 0,
  error TEXT
);
CREATE INDEX IF NOT EXISTS idx_audit_lookup ON audit_events(workspace_id, tool_id, timestamp DESC);
CREATE INDEX IF NOT EXISTS idx_audit_agent ON audit_events(agent_id, timestamp DESC);

CREATE TABLE IF NOT EXISTS cache_entries (
  cache_key TEXT PRIMARY KEY,
  tool_id TEXT NOT NULL,
  arguments_hash TEXT NOT NULL,
  result TEXT NOT NULL,
  created_at TIMESTAMP NOT NULL,
  expires_at TIMESTAMP NOT NULL,
  hit_count INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_cache_expires ON cache_entries(expires_at);
CREATE INDEX IF NOT EXISTS idx_cache_tool ON cache_entries(tool_id);

CREATE TABLE IF NOT EXISTS agent_limits (
  agent_id TEXT PRIMARY KEY,
  rpm INTEGER NOT NULL DEFAULT 60,
  burst INTEGER NOT NULL DEFAULT 20,
  cost_cap_usd_hour REAL NOT NULL DEFAULT 1.0,
  cost_cap_usd_day REAL NOT NULL DEFAULT 10.0,
  tenant_id TEXT NOT NULL DEFAULT 'default',
  updated_at TIMESTAMP NOT NULL
);

-- Rate-limit state is held in-memory (token buckets per agent) but persisted as
-- a snapshot so a graceful shutdown can restore the bucket level on restart.
CREATE TABLE IF NOT EXISTS rate_limit_snapshots (
  agent_id TEXT PRIMARY KEY,
  tokens REAL NOT NULL,
  last_refill TIMESTAMP NOT NULL
);

-- OAuth connections — encrypted access/refresh tokens for third-party providers.
-- Tokens are AES-256-GCM encrypted at rest with the key from
-- ``Settings.oauth_encryption_key``. ``user_id`` is the provider's stable
-- identifier (e.g. GitHub's numeric user id); ``user_login`` is the
-- human-readable handle ("octocat") for display only.
CREATE TABLE IF NOT EXISTS oauth_connections (
  id TEXT PRIMARY KEY,
  tenant_id TEXT NOT NULL DEFAULT 'default',
  provider TEXT NOT NULL,
  user_id TEXT NOT NULL,
  user_login TEXT,
  scopes TEXT NOT NULL DEFAULT '[]',
  access_token_encrypted TEXT NOT NULL,
  refresh_token_encrypted TEXT,
  expires_at TIMESTAMP,
  created_at TIMESTAMP NOT NULL,
  last_refreshed_at TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_oauth_tenant ON oauth_connections(tenant_id);
CREATE INDEX IF NOT EXISTS idx_oauth_provider ON oauth_connections(provider);
-- Tenant indices for v0.3 columns. These ALTER-added columns must exist
-- before the indices, so creation happens in _migrate.

-- OAuth state — short-lived rows that hold the PKCE verifier and the
-- caller-supplied redirect_uri across the GitHub round-trip. Cleared
-- opportunistically on /authorize and /callback; rows older than
-- ``oauth_state_ttl_seconds`` are considered expired and rejected.
CREATE TABLE IF NOT EXISTS oauth_states (
  state TEXT PRIMARY KEY,
  provider TEXT NOT NULL,
  redirect_uri TEXT NOT NULL,
  scopes TEXT NOT NULL DEFAULT '[]',
  pkce_verifier TEXT,
  tenant_id TEXT NOT NULL DEFAULT 'default',
  created_at TIMESTAMP NOT NULL,
  used INTEGER NOT NULL DEFAULT 0
);

-- v0.5 — Workflow transactions (Saga-style commit/compensate over tool calls).
-- Each transaction owns an ordered list of ``transaction_calls``; on commit we
-- execute calls in seq order; on partial failure, executed calls are rolled
-- back in reverse via their registered compensation_spec.
CREATE TABLE IF NOT EXISTS transactions (
  id TEXT PRIMARY KEY,
  workspace_id TEXT,
  agent_id TEXT,
  tenant_id TEXT NOT NULL DEFAULT 'default',
  status TEXT NOT NULL DEFAULT 'pending',
  metadata TEXT NOT NULL DEFAULT '{}',
  created_at TIMESTAMP NOT NULL,
  committed_at TIMESTAMP,
  rolled_back_at TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_tx_workspace ON transactions(workspace_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_tx_tenant ON transactions(tenant_id, status);

CREATE TABLE IF NOT EXISTS transaction_calls (
  id TEXT PRIMARY KEY,
  tx_id TEXT NOT NULL REFERENCES transactions(id),
  seq INTEGER NOT NULL,
  tool_id TEXT NOT NULL,
  arguments TEXT NOT NULL,
  compensation_spec TEXT,
  status TEXT NOT NULL DEFAULT 'pending',
  result TEXT,
  error TEXT,
  invoked_at TIMESTAMP,
  finished_at TIMESTAMP,
  UNIQUE (tx_id, seq)
);
CREATE INDEX IF NOT EXISTS idx_txc_tx ON transaction_calls(tx_id, seq);
"""


async def _ensure_column(
    conn: aiosqlite.Connection,
    table: str,
    column: str,
    coltype: str,
) -> None:
    """Add ``column`` to ``table`` if it isn't already there."""

    cur = await conn.execute(f"PRAGMA table_info({table})")
    rows = await cur.fetchall()
    await cur.close()
    existing = {row[1] for row in rows}
    if column in existing:
        return
    await conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {coltype}")


async def _migrate(conn: aiosqlite.Connection) -> None:
    """Apply additive in-place migrations. Idempotent."""

    await _ensure_column(
        conn, "tools", "tenant_id", "TEXT NOT NULL DEFAULT 'default'"
    )
    await _ensure_column(
        conn, "audit_events", "tenant_id", "TEXT NOT NULL DEFAULT 'default'"
    )
    await _ensure_column(
        conn, "agent_limits", "tenant_id", "TEXT NOT NULL DEFAULT 'default'"
    )
    await conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_audit_tenant "
        "ON audit_events(tenant_id, timestamp DESC)"
    )
    await conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_tools_tenant ON tools(tenant_id)"
    )


class Database:
    """Thin async wrapper around aiosqlite with shared connection."""

    def __init__(self, path: Path) -> None:
        self._path = path
        self._conn: aiosqlite.Connection | None = None

    @property
    def path(self) -> Path:
        return self._path

    async def connect(self) -> aiosqlite.Connection:
        """Open a connection (idempotent), ensuring schema + migrations."""
        if self._conn is None:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            conn = await aiosqlite.connect(str(self._path))
            conn.row_factory = aiosqlite.Row
            await conn.execute("PRAGMA journal_mode=WAL")
            await conn.execute("PRAGMA foreign_keys=ON")
            await conn.executescript(SCHEMA)
            await _migrate(conn)
            await conn.commit()
            self._conn = conn
        return self._conn

    async def close(self) -> None:
        """Close the connection if open."""
        if self._conn is not None:
            await self._conn.close()
            self._conn = None

    @asynccontextmanager
    async def cursor(self) -> AsyncIterator[aiosqlite.Cursor]:
        """Yield a cursor against the active connection."""
        conn = await self.connect()
        cursor = await conn.cursor()
        try:
            yield cursor
        finally:
            await cursor.close()

    async def execute(self, sql: str, params: tuple = ()) -> None:
        """Execute a write statement and commit."""
        conn = await self.connect()
        await conn.execute(sql, params)
        await conn.commit()

    async def fetchone(self, sql: str, params: tuple = ()) -> aiosqlite.Row | None:
        conn = await self.connect()
        async with conn.execute(sql, params) as cur:
            return await cur.fetchone()

    async def fetchall(self, sql: str, params: tuple = ()) -> list[aiosqlite.Row]:
        conn = await self.connect()
        async with conn.execute(sql, params) as cur:
            return list(await cur.fetchall())
