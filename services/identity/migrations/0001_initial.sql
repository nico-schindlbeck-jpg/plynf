-- Migration: 0001_initial
-- Service: identity
-- v0.3 baseline schema:
--   issued_tokens — minted JWT metadata for introspection + revocation.
--   tenants — tenant directory.
-- All statements are idempotent (CREATE ... IF NOT EXISTS) so the runner
-- can apply this against legacy databases that already have the tables.

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
