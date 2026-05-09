-- Rollback: 0005_audit_chain
-- Service: gateway
--
-- Drops the audit-chain columns and index added by 0005_audit_chain.sql.
-- Safe even if the columns/indexes don't exist; SQLite 3.35+ supports
-- ``ALTER TABLE ... DROP COLUMN`` natively.

DROP INDEX IF EXISTS idx_audit_chain;
ALTER TABLE audit_events DROP COLUMN event_hash;
ALTER TABLE audit_events DROP COLUMN prev_hash;
