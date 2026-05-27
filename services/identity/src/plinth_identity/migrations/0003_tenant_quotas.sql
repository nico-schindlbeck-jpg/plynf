-- Migration: 0003_tenant_quotas
-- Service: identity
-- v1.0 — Per-tenant resource quotas + usage tracking.
--
-- Tenants get an enforceable quota envelope (max workspaces, storage, channels,
-- workflows, tokens, OAuth connections, cost caps, RPM). Quotas live in
-- Identity (the source of tenant truth) and are read by Workspace + Gateway
-- when accepting create/invoke calls.
--
-- The ``tenant_usage`` table is a rollup that downstream services PUT into
-- (it's the source of truth for "how much of the quota is used right now").
-- Identity just hosts the table — the row contents are authored by Workspace
-- + Gateway via the rollup endpoints. All columns nullable-with-defaults so
-- a freshly seeded usage row reads cleanly without callers having to compute
-- every field.

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
