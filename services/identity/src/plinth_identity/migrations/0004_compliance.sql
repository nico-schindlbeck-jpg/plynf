-- Migration: 0004_compliance
-- Service: identity
--
-- v1.0 Compliance Scaffolding — GDPR export + delete job tracking.
--
-- ``export_jobs`` records GDPR Article 20 (data portability) requests. Each
-- row tracks one export from "pending" through "ready" → "expired" (or
-- "failed"). The download URL is reconstructed at request time from
-- ``export_id``.
--
-- ``delete_jobs`` records GDPR Article 17 (erasure) cascades. The
-- ``deleted_counts`` column carries a JSON object of ``{table: count}``
-- so callers can audit exactly what got removed.
--
-- ``delete_confirm_tokens`` holds the short-lived opaque tokens minted by
-- ``POST /v1/tenants/{id}/delete-data-confirm`` and consumed by the
-- ``DELETE /v1/tenants/{id}/data?confirm=…`` cascade. Tokens auto-expire
-- via ``expires_at``; one-shot use is enforced by row deletion on
-- consumption.

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

CREATE TABLE IF NOT EXISTS delete_confirm_tokens (
  confirm_token TEXT PRIMARY KEY,
  tenant_id TEXT NOT NULL,
  created_at TIMESTAMP NOT NULL,
  expires_at TIMESTAMP NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_delete_confirm_tokens_tenant
  ON delete_confirm_tokens(tenant_id);
