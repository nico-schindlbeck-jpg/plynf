-- Migration: 0001_initial
-- Service: gateway
-- v0.1 baseline schema:
--   tools, audit_events, cache_entries.
-- All statements are idempotent (CREATE ... IF NOT EXISTS) so the runner
-- can apply this against legacy databases that already have the tables.

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
  created_at TIMESTAMP NOT NULL,
  updated_at TIMESTAMP NOT NULL
);

CREATE TABLE IF NOT EXISTS audit_events (
  id TEXT PRIMARY KEY,
  timestamp TIMESTAMP NOT NULL,
  tool_id TEXT NOT NULL,
  workspace_id TEXT,
  agent_id TEXT,
  arguments_hash TEXT NOT NULL,
  arguments_preview TEXT,
  result_hash TEXT,
  cached INTEGER NOT NULL DEFAULT 0,
  duration_ms INTEGER NOT NULL,
  cost_estimate_usd REAL NOT NULL DEFAULT 0,
  error TEXT
);
CREATE INDEX IF NOT EXISTS idx_audit_lookup
  ON audit_events(workspace_id, tool_id, timestamp DESC);
CREATE INDEX IF NOT EXISTS idx_audit_agent
  ON audit_events(agent_id, timestamp DESC);

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
