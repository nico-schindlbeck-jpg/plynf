# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 The Plinth Authors
"""SQL DDL emitted on ``init_schema()`` for the gateway service."""

from __future__ import annotations

# SQLite schema mirrors the legacy ``plinth_gateway.db.SCHEMA`` 1:1.
SQLITE_SCHEMA = """
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
  error TEXT,
  prev_hash TEXT,
  event_hash TEXT
);
CREATE INDEX IF NOT EXISTS idx_audit_lookup ON audit_events(workspace_id, tool_id, timestamp DESC);
CREATE INDEX IF NOT EXISTS idx_audit_agent ON audit_events(agent_id, timestamp DESC);
CREATE INDEX IF NOT EXISTS idx_audit_tenant ON audit_events(tenant_id, timestamp DESC);
CREATE INDEX IF NOT EXISTS idx_audit_chain ON audit_events(id, event_hash);
CREATE INDEX IF NOT EXISTS idx_tools_tenant ON tools(tenant_id);

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

CREATE TABLE IF NOT EXISTS rate_limit_snapshots (
  agent_id TEXT PRIMARY KEY,
  tokens REAL NOT NULL,
  last_refill TIMESTAMP NOT NULL
);

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
"""

POSTGRES_SCHEMA = """
CREATE TABLE IF NOT EXISTS tools (
  tool_id TEXT PRIMARY KEY,
  name TEXT NOT NULL,
  description TEXT NOT NULL,
  transport TEXT NOT NULL,
  endpoint TEXT NOT NULL,
  input_schema TEXT NOT NULL,
  output_schema TEXT NOT NULL,
  idempotent SMALLINT NOT NULL DEFAULT 0,
  side_effects TEXT NOT NULL DEFAULT 'read',
  cache_ttl_seconds BIGINT,
  auth_method TEXT NOT NULL DEFAULT 'none',
  auth_config TEXT NOT NULL DEFAULT '{}',
  tenant_id TEXT NOT NULL DEFAULT 'default',
  created_at TIMESTAMPTZ NOT NULL,
  updated_at TIMESTAMPTZ NOT NULL
);

CREATE TABLE IF NOT EXISTS audit_events (
  id TEXT PRIMARY KEY,
  timestamp TIMESTAMPTZ NOT NULL,
  tool_id TEXT NOT NULL,
  workspace_id TEXT,
  agent_id TEXT,
  tenant_id TEXT NOT NULL DEFAULT 'default',
  arguments_hash TEXT NOT NULL,
  arguments_preview TEXT,
  result_hash TEXT,
  cached SMALLINT NOT NULL DEFAULT 0,
  duration_ms BIGINT NOT NULL,
  cost_estimate_usd DOUBLE PRECISION NOT NULL DEFAULT 0,
  error TEXT,
  prev_hash TEXT,
  event_hash TEXT
);
CREATE INDEX IF NOT EXISTS idx_audit_lookup ON audit_events(workspace_id, tool_id, timestamp DESC);
CREATE INDEX IF NOT EXISTS idx_audit_agent ON audit_events(agent_id, timestamp DESC);
CREATE INDEX IF NOT EXISTS idx_audit_tenant ON audit_events(tenant_id, timestamp DESC);
CREATE INDEX IF NOT EXISTS idx_audit_chain ON audit_events(id, event_hash);
CREATE INDEX IF NOT EXISTS idx_tools_tenant ON tools(tenant_id);

CREATE TABLE IF NOT EXISTS cache_entries (
  cache_key TEXT PRIMARY KEY,
  tool_id TEXT NOT NULL,
  arguments_hash TEXT NOT NULL,
  result TEXT NOT NULL,
  created_at TIMESTAMPTZ NOT NULL,
  expires_at TIMESTAMPTZ NOT NULL,
  hit_count BIGINT NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_cache_expires ON cache_entries(expires_at);
CREATE INDEX IF NOT EXISTS idx_cache_tool ON cache_entries(tool_id);

CREATE TABLE IF NOT EXISTS agent_limits (
  agent_id TEXT PRIMARY KEY,
  rpm BIGINT NOT NULL DEFAULT 60,
  burst BIGINT NOT NULL DEFAULT 20,
  cost_cap_usd_hour DOUBLE PRECISION NOT NULL DEFAULT 1.0,
  cost_cap_usd_day DOUBLE PRECISION NOT NULL DEFAULT 10.0,
  tenant_id TEXT NOT NULL DEFAULT 'default',
  updated_at TIMESTAMPTZ NOT NULL
);

CREATE TABLE IF NOT EXISTS rate_limit_snapshots (
  agent_id TEXT PRIMARY KEY,
  tokens DOUBLE PRECISION NOT NULL,
  last_refill TIMESTAMPTZ NOT NULL
);

CREATE TABLE IF NOT EXISTS oauth_connections (
  id TEXT PRIMARY KEY,
  tenant_id TEXT NOT NULL DEFAULT 'default',
  provider TEXT NOT NULL,
  user_id TEXT NOT NULL,
  user_login TEXT,
  scopes TEXT NOT NULL DEFAULT '[]',
  access_token_encrypted TEXT NOT NULL,
  refresh_token_encrypted TEXT,
  expires_at TIMESTAMPTZ,
  created_at TIMESTAMPTZ NOT NULL,
  last_refreshed_at TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS idx_oauth_tenant ON oauth_connections(tenant_id);
CREATE INDEX IF NOT EXISTS idx_oauth_provider ON oauth_connections(provider);

CREATE TABLE IF NOT EXISTS oauth_states (
  state TEXT PRIMARY KEY,
  provider TEXT NOT NULL,
  redirect_uri TEXT NOT NULL,
  scopes TEXT NOT NULL DEFAULT '[]',
  pkce_verifier TEXT,
  tenant_id TEXT NOT NULL DEFAULT 'default',
  created_at TIMESTAMPTZ NOT NULL,
  used SMALLINT NOT NULL DEFAULT 0
);
"""


__all__ = ["POSTGRES_SCHEMA", "SQLITE_SCHEMA"]
