-- Migration: 0004_tenancy
-- Service: gateway
-- v0.3 multi-tenancy: adds tenant_id columns to tools, audit_events,
-- agent_limits, oauth_connections, oauth_states. Plus tenant indices on
-- the audit + tool tables.
--
-- The runner emulates "ALTER TABLE ... ADD COLUMN IF NOT EXISTS" by
-- swallowing the SQLite "duplicate column name" error on a re-applied
-- migration, so a plain ALTER stays idempotent across runs.

ALTER TABLE tools ADD COLUMN tenant_id TEXT NOT NULL DEFAULT 'default';
ALTER TABLE audit_events ADD COLUMN tenant_id TEXT NOT NULL DEFAULT 'default';
ALTER TABLE agent_limits ADD COLUMN tenant_id TEXT NOT NULL DEFAULT 'default';
ALTER TABLE oauth_connections ADD COLUMN tenant_id TEXT NOT NULL DEFAULT 'default';
ALTER TABLE oauth_states ADD COLUMN tenant_id TEXT NOT NULL DEFAULT 'default';

CREATE INDEX IF NOT EXISTS idx_audit_tenant
  ON audit_events(tenant_id, timestamp DESC);
CREATE INDEX IF NOT EXISTS idx_tools_tenant ON tools(tenant_id);
CREATE INDEX IF NOT EXISTS idx_oauth_tenant ON oauth_connections(tenant_id);
