-- Migration: 0006_resource_locks
-- Service: workspace
-- v0.6 generic resource locks: coordination primitive for arbitrary named
-- resources within a workspace, decoupled from the workflow-step lease
-- table introduced in v0.5.
--
-- A lock row is identified by ``(workspace_id, name)`` — ``name`` may
-- contain ``/`` so callers can prefix lock targets meaningfully (e.g.
-- ``kv:sources/index``). The lease reaper sweeps rows where
-- ``expires_at < now()`` so a crashed holder never deadlocks subsequent
-- acquirers; ``acquire`` itself also steals expired rows atomically.

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
