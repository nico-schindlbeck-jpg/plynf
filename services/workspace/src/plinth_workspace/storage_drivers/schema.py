# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 The Plinth Authors
"""SQL DDL emitted on ``init_schema()`` for the workspace service.

We ship the same logical schema in two dialects — SQLite (legacy) and
Postgres (v0.4 new). The schema is idempotent (``CREATE TABLE IF NOT EXISTS``)
so it can be applied on every startup without migration tooling.

The mapping is deliberately mechanical:

    INTEGER PRIMARY KEY AUTOINCREMENT  -> BIGSERIAL
    INTEGER (where used as bool 0/1)   -> SMALLINT
    INTEGER (where used as count)      -> BIGINT
    TIMESTAMP                          -> TIMESTAMPTZ
    TEXT                               -> TEXT
"""

from __future__ import annotations

# ---------------------------------------------------------------------------
# SQLite — mirrors the legacy ``plinth_workspace.db.SCHEMA`` 1:1, plus the
# v0.4 ``retention_policies`` table.

SQLITE_SCHEMA = """
CREATE TABLE IF NOT EXISTS workspaces (
  id TEXT PRIMARY KEY,
  name TEXT NOT NULL,
  metadata TEXT NOT NULL DEFAULT '{}',
  tenant_id TEXT NOT NULL DEFAULT 'default',
  created_at TIMESTAMP NOT NULL,
  updated_at TIMESTAMP NOT NULL
);

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
CREATE INDEX IF NOT EXISTS idx_workspaces_tenant ON workspaces(tenant_id);

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
  FOREIGN KEY (workflow_id) REFERENCES workflows(id)
);

CREATE INDEX IF NOT EXISTS idx_workflow_steps_lookup
  ON workflow_steps(workflow_id, created_at);

CREATE INDEX IF NOT EXISTS idx_workflows_workspace
  ON workflows(workspace_id, created_at);

-- v0.4 -- retention policies for the GC engine.
CREATE TABLE IF NOT EXISTS retention_policies (
  workspace_id TEXT PRIMARY KEY,
  keep_versions INTEGER,
  keep_days INTEGER,
  keep_snapshots INTEGER,
  delete_unreferenced_blobs INTEGER NOT NULL DEFAULT 1,
  updated_at TIMESTAMP NOT NULL,
  FOREIGN KEY (workspace_id) REFERENCES workspaces(id)
);
"""

# ---------------------------------------------------------------------------
# Postgres — semantically equivalent to SQLite, with adjusted types and
# without the AUTOINCREMENT keyword (we rely on BIGSERIAL or explicit IDs).

POSTGRES_SCHEMA = """
CREATE TABLE IF NOT EXISTS workspaces (
  id TEXT PRIMARY KEY,
  name TEXT NOT NULL,
  metadata TEXT NOT NULL DEFAULT '{}',
  tenant_id TEXT NOT NULL DEFAULT 'default',
  created_at TIMESTAMPTZ NOT NULL,
  updated_at TIMESTAMPTZ NOT NULL
);

CREATE TABLE IF NOT EXISTS kv_entries (
  id BIGSERIAL PRIMARY KEY,
  workspace_id TEXT NOT NULL,
  key TEXT NOT NULL,
  value TEXT NOT NULL,
  version BIGINT NOT NULL,
  branch_id TEXT,
  deleted SMALLINT NOT NULL DEFAULT 0,
  created_at TIMESTAMPTZ NOT NULL,
  UNIQUE(workspace_id, key, version, branch_id),
  FOREIGN KEY (workspace_id) REFERENCES workspaces(id)
);

CREATE TABLE IF NOT EXISTS file_entries (
  id BIGSERIAL PRIMARY KEY,
  workspace_id TEXT NOT NULL,
  path TEXT NOT NULL,
  blob_sha256 TEXT NOT NULL,
  size BIGINT NOT NULL,
  content_type TEXT NOT NULL,
  version BIGINT NOT NULL,
  branch_id TEXT,
  deleted SMALLINT NOT NULL DEFAULT 0,
  created_at TIMESTAMPTZ NOT NULL,
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
  created_at TIMESTAMPTZ NOT NULL,
  FOREIGN KEY (workspace_id) REFERENCES workspaces(id)
);

CREATE TABLE IF NOT EXISTS branches (
  id TEXT PRIMARY KEY,
  workspace_id TEXT NOT NULL,
  name TEXT NOT NULL,
  from_snapshot_id TEXT NOT NULL,
  merged SMALLINT NOT NULL DEFAULT 0,
  merged_at TIMESTAMPTZ,
  created_at TIMESTAMPTZ NOT NULL,
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
CREATE INDEX IF NOT EXISTS idx_workspaces_tenant ON workspaces(tenant_id);

CREATE TABLE IF NOT EXISTS channels (
  workspace_id TEXT NOT NULL,
  name TEXT NOT NULL,
  created_at TIMESTAMPTZ NOT NULL,
  last_send_at TIMESTAMPTZ,
  last_receive_at TIMESTAMPTZ,
  PRIMARY KEY (workspace_id, name),
  FOREIGN KEY (workspace_id) REFERENCES workspaces(id)
);

CREATE TABLE IF NOT EXISTS channel_messages (
  id TEXT PRIMARY KEY,
  workspace_id TEXT NOT NULL,
  channel_name TEXT NOT NULL,
  seq BIGINT NOT NULL,
  payload TEXT NOT NULL,
  sender TEXT,
  type TEXT,
  correlation_id TEXT,
  headers TEXT NOT NULL DEFAULT '{}',
  sent_at TIMESTAMPTZ NOT NULL,
  delivered_at TIMESTAMPTZ,
  FOREIGN KEY (workspace_id, channel_name) REFERENCES channels(workspace_id, name)
);

CREATE INDEX IF NOT EXISTS idx_channel_messages_lookup
  ON channel_messages(workspace_id, channel_name, seq);

CREATE TABLE IF NOT EXISTS channel_consumers (
  workspace_id TEXT NOT NULL,
  channel_name TEXT NOT NULL,
  consumer TEXT NOT NULL,
  cursor BIGINT NOT NULL DEFAULT 0,
  updated_at TIMESTAMPTZ NOT NULL,
  PRIMARY KEY (workspace_id, channel_name, consumer)
);

CREATE TABLE IF NOT EXISTS workflows (
  id TEXT PRIMARY KEY,
  workspace_id TEXT NOT NULL,
  name TEXT NOT NULL,
  steps_manifest TEXT NOT NULL,
  metadata TEXT NOT NULL DEFAULT '{}',
  status TEXT NOT NULL DEFAULT 'pending',
  created_at TIMESTAMPTZ NOT NULL,
  started_at TIMESTAMPTZ,
  finished_at TIMESTAMPTZ,
  FOREIGN KEY (workspace_id) REFERENCES workspaces(id)
);

CREATE TABLE IF NOT EXISTS workflow_steps (
  id TEXT PRIMARY KEY,
  workflow_id TEXT NOT NULL,
  name TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'pending',
  attempt BIGINT NOT NULL DEFAULT 1,
  started_at TIMESTAMPTZ,
  finished_at TIMESTAMPTZ,
  input TEXT,
  output TEXT,
  error TEXT,
  snapshot_id TEXT,
  created_at TIMESTAMPTZ NOT NULL,
  FOREIGN KEY (workflow_id) REFERENCES workflows(id)
);

CREATE INDEX IF NOT EXISTS idx_workflow_steps_lookup
  ON workflow_steps(workflow_id, created_at);

CREATE INDEX IF NOT EXISTS idx_workflows_workspace
  ON workflows(workspace_id, created_at);

CREATE TABLE IF NOT EXISTS retention_policies (
  workspace_id TEXT PRIMARY KEY,
  keep_versions BIGINT,
  keep_days BIGINT,
  keep_snapshots BIGINT,
  delete_unreferenced_blobs SMALLINT NOT NULL DEFAULT 1,
  updated_at TIMESTAMPTZ NOT NULL,
  FOREIGN KEY (workspace_id) REFERENCES workspaces(id)
);
"""


__all__ = ["POSTGRES_SCHEMA", "SQLITE_SCHEMA"]
