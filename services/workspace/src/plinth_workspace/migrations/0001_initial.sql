-- Migration: 0001_initial
-- Service: workspace
-- Captures the v0.1 baseline schema:
--   workspaces, kv_entries, file_entries, snapshots, branches.
-- All statements are idempotent (CREATE ... IF NOT EXISTS) so the runner can
-- safely apply this against a database that already carries the same shape
-- via the legacy CREATE-IF-NOT-EXISTS bootstrap.

CREATE TABLE IF NOT EXISTS workspaces (
  id TEXT PRIMARY KEY,
  name TEXT NOT NULL,
  metadata TEXT NOT NULL DEFAULT '{}',
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
