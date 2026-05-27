-- Migration: 0005_retention
-- Service: workspace
-- v0.4 retention policies: per-workspace GC rules.
--
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
