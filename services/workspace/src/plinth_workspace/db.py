# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 The Plinth Authors
"""SQLite schema and connection management for the workspace service.

We use ``aiosqlite`` directly to keep dependencies minimal. Every public
helper opens a fresh connection scoped to a request — SQLite's WAL mode
handles concurrency, and per-request connections free us from worrying
about cross-task transaction state.
"""

from __future__ import annotations

import contextlib
from collections.abc import AsyncIterator
from datetime import datetime, timezone
from pathlib import Path

import aiosqlite

# 3.9 compatibility shim for verification; on 3.11+ this is identical to
# ``datetime.UTC``.
UTC = timezone.utc  # noqa: UP017

SCHEMA = """
CREATE TABLE IF NOT EXISTS workspaces (
  id TEXT PRIMARY KEY,
  name TEXT NOT NULL,
  metadata TEXT NOT NULL DEFAULT '{}',
  tenant_id TEXT NOT NULL DEFAULT 'default',
  created_at TIMESTAMP NOT NULL,
  updated_at TIMESTAMP NOT NULL
);
-- idx_workspaces_tenant is created in _migrate after ALTER TABLE so it
-- safely runs on databases predating the tenant_id column.

CREATE TABLE IF NOT EXISTS kv_entries (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  workspace_id TEXT NOT NULL,
  key TEXT NOT NULL,
  value TEXT NOT NULL,
  version INTEGER NOT NULL,
  branch_id TEXT,
  deleted INTEGER NOT NULL DEFAULT 0,
  created_at TIMESTAMP NOT NULL,
  UNIQUE(workspace_id, key, version, branch_id),
  FOREIGN KEY (workspace_id) REFERENCES workspaces(id)
);

CREATE TABLE IF NOT EXISTS file_entries (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  workspace_id TEXT NOT NULL,
  path TEXT NOT NULL,
  blob_sha256 TEXT NOT NULL,
  size INTEGER NOT NULL,
  content_type TEXT NOT NULL,
  version INTEGER NOT NULL,
  branch_id TEXT,
  deleted INTEGER NOT NULL DEFAULT 0,
  created_at TIMESTAMP NOT NULL,
  UNIQUE(workspace_id, path, version, branch_id),
  FOREIGN KEY (workspace_id) REFERENCES workspaces(id)
);

CREATE TABLE IF NOT EXISTS snapshots (
  id TEXT PRIMARY KEY,
  workspace_id TEXT NOT NULL,
  name TEXT NOT NULL,
  message TEXT,
  parent_snapshot_id TEXT,
  kv_versions TEXT NOT NULL,
  file_versions TEXT NOT NULL,
  created_at TIMESTAMP NOT NULL,
  FOREIGN KEY (workspace_id) REFERENCES workspaces(id)
);

CREATE TABLE IF NOT EXISTS branches (
  id TEXT PRIMARY KEY,
  workspace_id TEXT NOT NULL,
  name TEXT NOT NULL,
  from_snapshot_id TEXT NOT NULL,
  merged INTEGER NOT NULL DEFAULT 0,
  merged_at TIMESTAMP,
  created_at TIMESTAMP NOT NULL,
  FOREIGN KEY (workspace_id) REFERENCES workspaces(id)
);

CREATE INDEX IF NOT EXISTS idx_kv_lookup
    ON kv_entries(workspace_id, key, branch_id, version DESC);
CREATE INDEX IF NOT EXISTS idx_files_lookup
    ON file_entries(workspace_id, path, branch_id, version DESC);
CREATE INDEX IF NOT EXISTS idx_snapshots_workspace
    ON snapshots(workspace_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_branches_workspace
    ON branches(workspace_id);

-- v0.2 ----------------------------------------------------------- channels

CREATE TABLE IF NOT EXISTS channels (
  workspace_id TEXT NOT NULL,
  name TEXT NOT NULL,
  created_at TIMESTAMP NOT NULL,
  last_send_at TIMESTAMP,
  last_receive_at TIMESTAMP,
  PRIMARY KEY (workspace_id, name),
  FOREIGN KEY (workspace_id) REFERENCES workspaces(id)
);

CREATE TABLE IF NOT EXISTS channel_messages (
  id TEXT PRIMARY KEY,
  workspace_id TEXT NOT NULL,
  channel_name TEXT NOT NULL,
  seq INTEGER NOT NULL,
  payload TEXT NOT NULL,
  sender TEXT,
  type TEXT,
  correlation_id TEXT,
  headers TEXT NOT NULL DEFAULT '{}',
  sent_at TIMESTAMP NOT NULL,
  delivered_at TIMESTAMP,
  FOREIGN KEY (workspace_id, channel_name) REFERENCES channels(workspace_id, name)
);

CREATE INDEX IF NOT EXISTS idx_channel_messages_lookup
  ON channel_messages(workspace_id, channel_name, seq);

CREATE TABLE IF NOT EXISTS channel_consumers (
  workspace_id TEXT NOT NULL,
  channel_name TEXT NOT NULL,
  consumer TEXT NOT NULL,
  cursor INTEGER NOT NULL DEFAULT 0,
  updated_at TIMESTAMP NOT NULL,
  PRIMARY KEY (workspace_id, channel_name, consumer)
);

-- v0.2 --------------------------------------------------------- workflows

CREATE TABLE IF NOT EXISTS workflows (
  id TEXT PRIMARY KEY,
  workspace_id TEXT NOT NULL,
  name TEXT NOT NULL,
  steps_manifest TEXT NOT NULL,
  metadata TEXT NOT NULL DEFAULT '{}',
  status TEXT NOT NULL DEFAULT 'pending',
  created_at TIMESTAMP NOT NULL,
  started_at TIMESTAMP,
  finished_at TIMESTAMP,
  FOREIGN KEY (workspace_id) REFERENCES workspaces(id)
);

CREATE TABLE IF NOT EXISTS workflow_steps (
  id TEXT PRIMARY KEY,
  workflow_id TEXT NOT NULL,
  name TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'pending',
  attempt INTEGER NOT NULL DEFAULT 1,
  started_at TIMESTAMP,
  finished_at TIMESTAMP,
  input TEXT,
  output TEXT,
  error TEXT,
  snapshot_id TEXT,
  created_at TIMESTAMP NOT NULL,
  -- v1.1 — per-step retry policy + scheduled retry timestamp.
  -- Defaults match v1.0 behaviour (single attempt, no delay) so existing
  -- rows inserted before the column was added stay compatible.
  max_attempts INTEGER NOT NULL DEFAULT 1,
  retry_policy TEXT NOT NULL DEFAULT 'none',
  retry_initial_delay_seconds REAL NOT NULL DEFAULT 1.0,
  retry_max_delay_seconds REAL NOT NULL DEFAULT 60.0,
  retry_jitter INTEGER NOT NULL DEFAULT 1,
  next_retry_at TIMESTAMP,
  FOREIGN KEY (workflow_id) REFERENCES workflows(id)
);

CREATE INDEX IF NOT EXISTS idx_workflow_steps_lookup
  ON workflow_steps(workflow_id, created_at);

CREATE INDEX IF NOT EXISTS idx_workflows_workspace
  ON workflows(workspace_id, created_at);

-- v0.4 ----------------------------------------------------- retention / GC

-- ``keep_versions`` / ``keep_days`` / ``keep_snapshots`` are nullable so a
-- policy can opt out of any one rule by leaving the column NULL. The
-- ``delete_unreferenced_blobs`` flag is a 0/1 INTEGER (SQLite has no real
-- BOOLEAN type — the same column is SMALLINT in Postgres).
CREATE TABLE IF NOT EXISTS retention_policies (
  workspace_id TEXT PRIMARY KEY,
  keep_versions INTEGER,
  keep_days INTEGER,
  keep_snapshots INTEGER,
  delete_unreferenced_blobs INTEGER NOT NULL DEFAULT 1,
  updated_at TIMESTAMP NOT NULL,
  FOREIGN KEY (workspace_id) REFERENCES workspaces(id)
);

-- v0.5 -------------------------------------------- durable workflow executor

-- A lease is a soft lock acquired by a worker over a workflow_step. While
-- the lease is ``running`` and ``expires_at`` is in the future, no other
-- worker may execute the step. The lease reaper expires stale rows so a
-- crashed worker's step is reclaimable.
CREATE TABLE IF NOT EXISTS workflow_step_leases (
  step_id TEXT PRIMARY KEY,
  worker_id TEXT NOT NULL,
  acquired_at TIMESTAMP NOT NULL,
  expires_at TIMESTAMP NOT NULL,
  heartbeat_at TIMESTAMP NOT NULL,
  status TEXT NOT NULL DEFAULT 'running'
);
CREATE INDEX IF NOT EXISTS idx_leases_expiry
  ON workflow_step_leases(expires_at);

CREATE TABLE IF NOT EXISTS workers (
  id TEXT PRIMARY KEY,
  hostname TEXT,
  pid INTEGER,
  started_at TIMESTAMP NOT NULL,
  last_heartbeat_at TIMESTAMP NOT NULL,
  status TEXT NOT NULL DEFAULT 'active'
);
CREATE INDEX IF NOT EXISTS idx_workers_status
  ON workers(status, last_heartbeat_at);

-- v0.5 ----------------------------------------------------- typed channels

-- ``channel_schemas`` attaches an optional JSON Schema document to a channel.
-- The schema is enforced on every send; failed-validation messages are
-- routed to ``<channel>.deadletter`` instead of the main channel and the
-- caller receives a 422 (``SCHEMA_VIOLATION``). Channels without an entry
-- in this table behave exactly like the v0.2 untyped channels.
CREATE TABLE IF NOT EXISTS channel_schemas (
  workspace_id TEXT NOT NULL,
  channel_name TEXT NOT NULL,
  schema_json TEXT NOT NULL,
  version INTEGER NOT NULL DEFAULT 1,
  updated_at TIMESTAMP NOT NULL,
  PRIMARY KEY (workspace_id, channel_name),
  FOREIGN KEY (workspace_id) REFERENCES workspaces(id)
);
CREATE INDEX IF NOT EXISTS idx_channel_schemas_workspace
  ON channel_schemas(workspace_id);

-- v0.6 ------------------------------------------------ generic resource locks

-- Generic distributed locks for arbitrary named resources, scoped per
-- workspace. Used for Agent-A-vs-Agent-B race protection on KV / file /
-- external resource updates. The :path-style ``name`` accepts ``/`` so a
-- caller can lock e.g. ``kv:sources/index`` without escaping.
--
-- The reaper sweeps rows where ``expires_at < now()`` so a crashed holder
-- doesn't deadlock subsequent acquirers; ``acquire`` itself also steals an
-- expired lock atomically via UPSERT (see resource_locks.py).
CREATE TABLE IF NOT EXISTS resource_locks (
  workspace_id TEXT NOT NULL,
  name TEXT NOT NULL,
  holder TEXT NOT NULL,
  acquired_at TIMESTAMP NOT NULL,
  expires_at TIMESTAMP NOT NULL,
  heartbeat_at TIMESTAMP NOT NULL,
  PRIMARY KEY (workspace_id, name),
  FOREIGN KEY (workspace_id) REFERENCES workspaces(id)
);
CREATE INDEX IF NOT EXISTS idx_resource_locks_expiry
  ON resource_locks(expires_at);

-- v1.1 -------------------------------------------- workflow dead-letter queue

-- Per-workflow DLQ for steps that exhausted ``max_attempts``. Each row is
-- a frozen snapshot of the failing :class:`WorkflowStep` (serialized as
-- JSON) so an operator can inspect, replay, or discard the entry without
-- mutating the underlying step row. The reaper does not touch this
-- table; entries persist until ``DELETE`` or replay.
CREATE TABLE IF NOT EXISTS workflow_dlq (
  id TEXT PRIMARY KEY,
  step_id TEXT NOT NULL,
  workflow_id TEXT NOT NULL,
  workspace_id TEXT NOT NULL,
  step_name TEXT NOT NULL,
  attempts INTEGER NOT NULL,
  last_error TEXT,
  failed_at TIMESTAMP NOT NULL,
  step_snapshot TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_workflow_dlq_workflow
  ON workflow_dlq(workflow_id);
CREATE INDEX IF NOT EXISTS idx_workflow_dlq_failed_at
  ON workflow_dlq(failed_at);
"""


def _ensure_parent_dir(db_path: Path) -> None:
    db_path.parent.mkdir(parents=True, exist_ok=True)


async def init_db(db_path: Path) -> None:
    """Initialise the database (idempotent).

    Creates the parent directory if missing, applies the schema, and turns on
    WAL + foreign keys. Also runs lightweight in-place migrations so v0.2 DBs
    pick up v0.3 columns (notably ``tenant_id``) on first boot.
    """

    _ensure_parent_dir(db_path)
    async with aiosqlite.connect(db_path) as conn:
        await conn.execute("PRAGMA journal_mode=WAL")
        await conn.execute("PRAGMA foreign_keys=ON")
        await conn.executescript(SCHEMA)
        await _migrate(conn)
        await conn.commit()


async def _migrate(conn: aiosqlite.Connection) -> None:
    """Apply additive in-place migrations.

    Each migration is idempotent — wrapped in a try/except so a second pass
    is a no-op. SQLite refuses ``ADD COLUMN`` for a column that already
    exists, so the simplest safe pattern is to attempt + swallow that one
    error.
    """

    await _ensure_column(conn, "workspaces", "tenant_id", "TEXT NOT NULL DEFAULT 'default'")
    # Index creation runs after the column is guaranteed to exist.
    await conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_workspaces_tenant ON workspaces(tenant_id)"
    )

    # v1.1 — per-step retry policy. Adding to legacy DBs is idempotent
    # because ``_ensure_column`` reads ``PRAGMA table_info`` first. Default
    # values match the v1.0 single-attempt behaviour so untouched rows
    # don't suddenly start retrying.
    await _ensure_column(
        conn, "workflow_steps", "max_attempts", "INTEGER NOT NULL DEFAULT 1"
    )
    await _ensure_column(
        conn, "workflow_steps", "retry_policy", "TEXT NOT NULL DEFAULT 'none'"
    )
    await _ensure_column(
        conn,
        "workflow_steps",
        "retry_initial_delay_seconds",
        "REAL NOT NULL DEFAULT 1.0",
    )
    await _ensure_column(
        conn,
        "workflow_steps",
        "retry_max_delay_seconds",
        "REAL NOT NULL DEFAULT 60.0",
    )
    await _ensure_column(
        conn, "workflow_steps", "retry_jitter", "INTEGER NOT NULL DEFAULT 1"
    )
    await _ensure_column(
        conn, "workflow_steps", "next_retry_at", "TIMESTAMP"
    )


async def _ensure_column(
    conn: aiosqlite.Connection,
    table: str,
    column: str,
    coltype: str,
) -> None:
    """Add ``column`` to ``table`` if missing (using ``PRAGMA table_info``)."""

    cur = await conn.execute(f"PRAGMA table_info({table})")
    rows = await cur.fetchall()
    await cur.close()
    existing = {row[1] for row in rows}
    if column in existing:
        return
    await conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {coltype}")


@contextlib.asynccontextmanager
async def connect(db_path: Path) -> AsyncIterator[aiosqlite.Connection]:
    """Open a fresh aiosqlite connection with sane PRAGMAs.

    Connections returned by this manager have ``row_factory = aiosqlite.Row``
    so callers can index by column name.
    """

    _ensure_parent_dir(db_path)
    conn = await aiosqlite.connect(db_path)
    try:
        await conn.execute("PRAGMA foreign_keys=ON")
        conn.row_factory = aiosqlite.Row
        yield conn
    finally:
        await conn.close()


def now_utc() -> datetime:
    """Return a timezone-aware UTC ``datetime``.

    Centralised so tests can monkeypatch a single seam if they ever need to.
    """

    return datetime.now(UTC)


def iso(ts: datetime) -> str:
    """Serialise a datetime as an ISO-8601 string for SQLite storage."""

    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=UTC)
    return ts.isoformat()


def parse_ts(value: str | datetime | None) -> datetime | None:
    """Parse a stored timestamp back into a timezone-aware datetime."""

    if value is None:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=UTC)
    # SQLite always gives strings via aiosqlite.Row indexing.
    parsed = datetime.fromisoformat(value)
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)
