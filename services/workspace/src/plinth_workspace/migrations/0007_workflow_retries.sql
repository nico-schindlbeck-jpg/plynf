-- Migration: 0007_workflow_retries
-- Service: workspace
-- v1.1 — workflow retries + dead-letter queue.
--
-- Adds six per-step retry-policy columns to ``workflow_steps`` and the
-- new ``workflow_dlq`` table that captures snapshots of steps that
-- exhausted ``max_attempts`` failures. All ``ALTER`` statements are
-- guarded by ``ADD COLUMN IF NOT EXISTS`` semantics — SQLite errors on
-- re-add but the v1.1 in-place migrator (``db.py::_migrate``) already
-- handles legacy DBs that pre-date this file via
-- ``PRAGMA table_info`` checks. The runner runs each statement here
-- exactly once after recording the migration ID, so duplicate-add is
-- not a concern when applied through the migration framework.
--
-- Defaults match the v1.0 baseline so any existing ``workflow_steps``
-- rows continue to behave identically (single attempt, no delay).

ALTER TABLE workflow_steps ADD COLUMN max_attempts INTEGER NOT NULL DEFAULT 1;
ALTER TABLE workflow_steps ADD COLUMN retry_policy TEXT NOT NULL DEFAULT 'none';
ALTER TABLE workflow_steps ADD COLUMN retry_initial_delay_seconds REAL NOT NULL DEFAULT 1.0;
ALTER TABLE workflow_steps ADD COLUMN retry_max_delay_seconds REAL NOT NULL DEFAULT 60.0;
ALTER TABLE workflow_steps ADD COLUMN retry_jitter INTEGER NOT NULL DEFAULT 1;
ALTER TABLE workflow_steps ADD COLUMN next_retry_at TIMESTAMP;

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
