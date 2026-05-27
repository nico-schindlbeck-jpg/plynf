-- Rollback: 0001_initial
-- Service: gateway
-- Strategy: drop tables + indices.
-- Data preservation: NO — every registered tool, audit event, and cache
-- entry is removed. Verify backups before running.
--
-- Reverses 0001_initial.sql by dropping the v0.1 baseline schema.
-- Indices are dropped explicitly even though SQLite/Postgres clean them
-- up alongside the tables — keeps behaviour identical when this file is
-- replayed against partially-applied environments.

DROP INDEX IF EXISTS idx_cache_tool;
DROP INDEX IF EXISTS idx_cache_expires;
DROP INDEX IF EXISTS idx_audit_agent;
DROP INDEX IF EXISTS idx_audit_lookup;

DROP TABLE IF EXISTS cache_entries;
DROP TABLE IF EXISTS audit_events;
DROP TABLE IF EXISTS tools;
