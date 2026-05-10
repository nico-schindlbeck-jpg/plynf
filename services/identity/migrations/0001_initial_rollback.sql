-- Rollback: 0001_initial
-- Service: identity
-- Strategy: drop tables + indices.
-- Data preservation: NO — every issued-token record and tenant row is
-- removed. After rollback, all in-flight tokens become unverifiable
-- (introspection paths return 404). Verify backups before running.
--
-- Reverses 0001_initial.sql by dropping the v0.3 baseline schema.
-- Indices are dropped explicitly so behaviour stays identical across
-- back-ends when this file is replayed against partially-applied envs.

DROP INDEX IF EXISTS idx_tokens_revoked_at;
DROP INDEX IF EXISTS idx_tokens_expires;
DROP INDEX IF EXISTS idx_tokens_tenant;
DROP INDEX IF EXISTS idx_tokens_agent;

DROP TABLE IF EXISTS tenants;
DROP TABLE IF EXISTS issued_tokens;
