# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 The Plinth Authors
"""SQL DDL emitted on ``init_schema()`` for the identity service."""

from __future__ import annotations

SQLITE_SCHEMA = """
CREATE TABLE IF NOT EXISTS issued_tokens (
  jti TEXT PRIMARY KEY,
  agent_id TEXT NOT NULL,
  tenant_id TEXT NOT NULL,
  workspace_id TEXT,
  scopes TEXT NOT NULL,
  issued_at TIMESTAMP NOT NULL,
  expires_at TIMESTAMP NOT NULL,
  revoked INTEGER NOT NULL DEFAULT 0,
  revoked_at TIMESTAMP,
  metadata TEXT NOT NULL DEFAULT '{}'
);

CREATE INDEX IF NOT EXISTS idx_tokens_agent
  ON issued_tokens(agent_id, issued_at DESC);
CREATE INDEX IF NOT EXISTS idx_tokens_tenant
  ON issued_tokens(tenant_id, issued_at DESC);
CREATE INDEX IF NOT EXISTS idx_tokens_expires
  ON issued_tokens(expires_at);
CREATE INDEX IF NOT EXISTS idx_tokens_revoked_at
  ON issued_tokens(revoked, revoked_at);

CREATE TABLE IF NOT EXISTS tenants (
  id TEXT PRIMARY KEY,
  name TEXT NOT NULL,
  metadata TEXT NOT NULL DEFAULT '{}',
  created_at TIMESTAMP NOT NULL
);

-- v1.0 — per-tenant resource quotas + usage rollup (see 0003_tenant_quotas.sql).
CREATE TABLE IF NOT EXISTS tenant_quotas (
  tenant_id TEXT PRIMARY KEY,
  max_workspaces INTEGER NOT NULL DEFAULT 100,
  max_storage_gb REAL NOT NULL DEFAULT 10.0,
  max_channels_per_workspace INTEGER NOT NULL DEFAULT 50,
  max_workflows_per_workspace INTEGER NOT NULL DEFAULT 100,
  max_active_tokens INTEGER NOT NULL DEFAULT 1000,
  max_oauth_connections INTEGER NOT NULL DEFAULT 50,
  max_cost_usd_day REAL NOT NULL DEFAULT 100.0,
  max_cost_usd_month REAL NOT NULL DEFAULT 2000.0,
  max_invocations_per_minute INTEGER NOT NULL DEFAULT 600,
  updated_at TIMESTAMP NOT NULL
);

CREATE TABLE IF NOT EXISTS tenant_usage (
  tenant_id TEXT PRIMARY KEY,
  workspaces INTEGER NOT NULL DEFAULT 0,
  storage_gb REAL NOT NULL DEFAULT 0.0,
  active_tokens INTEGER NOT NULL DEFAULT 0,
  oauth_connections INTEGER NOT NULL DEFAULT 0,
  cost_usd_day REAL NOT NULL DEFAULT 0.0,
  cost_usd_month REAL NOT NULL DEFAULT 0.0,
  last_invocation_at TIMESTAMP,
  updated_at TIMESTAMP NOT NULL
);

-- v1.0 — GDPR Article 20 (data portability) export jobs.
CREATE TABLE IF NOT EXISTS export_jobs (
  export_id TEXT PRIMARY KEY,
  tenant_id TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'pending',
  requested_at TIMESTAMP NOT NULL,
  completed_at TIMESTAMP,
  expires_at TIMESTAMP,
  size_bytes INTEGER,
  error TEXT
);
CREATE INDEX IF NOT EXISTS idx_export_jobs_tenant
  ON export_jobs(tenant_id, requested_at DESC);

-- v1.0 — GDPR Article 17 (erasure) cascade jobs.
CREATE TABLE IF NOT EXISTS delete_jobs (
  job_id TEXT PRIMARY KEY,
  tenant_id TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'pending',
  requested_at TIMESTAMP NOT NULL,
  completed_at TIMESTAMP,
  deleted_counts TEXT NOT NULL DEFAULT '{}',
  error TEXT
);
CREATE INDEX IF NOT EXISTS idx_delete_jobs_tenant
  ON delete_jobs(tenant_id, requested_at DESC);

-- v1.0 — Two-phase delete confirm tokens. Short-lived (~10 min), one-shot.
CREATE TABLE IF NOT EXISTS delete_confirm_tokens (
  confirm_token TEXT PRIMARY KEY,
  tenant_id TEXT NOT NULL,
  created_at TIMESTAMP NOT NULL,
  expires_at TIMESTAMP NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_delete_confirm_tokens_tenant
  ON delete_confirm_tokens(tenant_id);
"""

POSTGRES_SCHEMA = """
CREATE TABLE IF NOT EXISTS issued_tokens (
  jti TEXT PRIMARY KEY,
  agent_id TEXT NOT NULL,
  tenant_id TEXT NOT NULL,
  workspace_id TEXT,
  scopes TEXT NOT NULL,
  issued_at TIMESTAMPTZ NOT NULL,
  expires_at TIMESTAMPTZ NOT NULL,
  revoked SMALLINT NOT NULL DEFAULT 0,
  revoked_at TIMESTAMPTZ,
  metadata TEXT NOT NULL DEFAULT '{}'
);

CREATE INDEX IF NOT EXISTS idx_tokens_agent
  ON issued_tokens(agent_id, issued_at DESC);
CREATE INDEX IF NOT EXISTS idx_tokens_tenant
  ON issued_tokens(tenant_id, issued_at DESC);
CREATE INDEX IF NOT EXISTS idx_tokens_expires
  ON issued_tokens(expires_at);
CREATE INDEX IF NOT EXISTS idx_tokens_revoked_at
  ON issued_tokens(revoked, revoked_at);

CREATE TABLE IF NOT EXISTS tenants (
  id TEXT PRIMARY KEY,
  name TEXT NOT NULL,
  metadata TEXT NOT NULL DEFAULT '{}',
  created_at TIMESTAMPTZ NOT NULL
);

-- v1.0 — per-tenant resource quotas + usage rollup.
CREATE TABLE IF NOT EXISTS tenant_quotas (
  tenant_id TEXT PRIMARY KEY,
  max_workspaces INTEGER NOT NULL DEFAULT 100,
  max_storage_gb DOUBLE PRECISION NOT NULL DEFAULT 10.0,
  max_channels_per_workspace INTEGER NOT NULL DEFAULT 50,
  max_workflows_per_workspace INTEGER NOT NULL DEFAULT 100,
  max_active_tokens INTEGER NOT NULL DEFAULT 1000,
  max_oauth_connections INTEGER NOT NULL DEFAULT 50,
  max_cost_usd_day DOUBLE PRECISION NOT NULL DEFAULT 100.0,
  max_cost_usd_month DOUBLE PRECISION NOT NULL DEFAULT 2000.0,
  max_invocations_per_minute INTEGER NOT NULL DEFAULT 600,
  updated_at TIMESTAMPTZ NOT NULL
);

CREATE TABLE IF NOT EXISTS tenant_usage (
  tenant_id TEXT PRIMARY KEY,
  workspaces INTEGER NOT NULL DEFAULT 0,
  storage_gb DOUBLE PRECISION NOT NULL DEFAULT 0.0,
  active_tokens INTEGER NOT NULL DEFAULT 0,
  oauth_connections INTEGER NOT NULL DEFAULT 0,
  cost_usd_day DOUBLE PRECISION NOT NULL DEFAULT 0.0,
  cost_usd_month DOUBLE PRECISION NOT NULL DEFAULT 0.0,
  last_invocation_at TIMESTAMPTZ,
  updated_at TIMESTAMPTZ NOT NULL
);

-- v1.0 — GDPR Article 20 (data portability) export jobs.
CREATE TABLE IF NOT EXISTS export_jobs (
  export_id TEXT PRIMARY KEY,
  tenant_id TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'pending',
  requested_at TIMESTAMPTZ NOT NULL,
  completed_at TIMESTAMPTZ,
  expires_at TIMESTAMPTZ,
  size_bytes BIGINT,
  error TEXT
);
CREATE INDEX IF NOT EXISTS idx_export_jobs_tenant
  ON export_jobs(tenant_id, requested_at DESC);

-- v1.0 — GDPR Article 17 (erasure) cascade jobs.
CREATE TABLE IF NOT EXISTS delete_jobs (
  job_id TEXT PRIMARY KEY,
  tenant_id TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'pending',
  requested_at TIMESTAMPTZ NOT NULL,
  completed_at TIMESTAMPTZ,
  deleted_counts TEXT NOT NULL DEFAULT '{}',
  error TEXT
);
CREATE INDEX IF NOT EXISTS idx_delete_jobs_tenant
  ON delete_jobs(tenant_id, requested_at DESC);

-- v1.0 — Two-phase delete confirm tokens. Short-lived (~10 min), one-shot.
CREATE TABLE IF NOT EXISTS delete_confirm_tokens (
  confirm_token TEXT PRIMARY KEY,
  tenant_id TEXT NOT NULL,
  created_at TIMESTAMPTZ NOT NULL,
  expires_at TIMESTAMPTZ NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_delete_confirm_tokens_tenant
  ON delete_confirm_tokens(tenant_id);
"""


__all__ = ["POSTGRES_SCHEMA", "SQLITE_SCHEMA"]
