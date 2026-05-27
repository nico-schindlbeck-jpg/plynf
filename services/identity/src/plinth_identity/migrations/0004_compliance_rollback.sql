-- Rollback: 0004_compliance
-- Service: identity
--
-- Drops the GDPR compliance tables added by 0004_compliance.sql.
-- DROP IF EXISTS keeps it idempotent across partial-roll states.

DROP INDEX IF EXISTS idx_export_jobs_tenant;
DROP INDEX IF EXISTS idx_delete_jobs_tenant;
DROP INDEX IF EXISTS idx_delete_confirm_tokens_tenant;
DROP TABLE IF EXISTS export_jobs;
DROP TABLE IF EXISTS delete_jobs;
DROP TABLE IF EXISTS delete_confirm_tokens;
