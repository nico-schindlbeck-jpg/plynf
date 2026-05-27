-- Migration: 0003_workflows
-- Service: workspace
-- v0.2 workflows: named sequences of agent steps with checkpointed state.
-- Adds workflows, workflow_steps tables + supporting indices.

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
