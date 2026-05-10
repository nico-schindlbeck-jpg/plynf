-- Rollback: 0007_workflow_retries
-- Service: workspace
-- Strategy: drop columns + DLQ table.
-- Data preservation: PARTIAL — the workflow_dlq table is dropped in full
-- (all dead-lettered step snapshots are lost). The retry-policy columns
-- are dropped but the underlying step rows remain; subsequent retries
-- will fall back to the v1.0 single-attempt behaviour.
--
-- SQLite 3.35+ supports ``ALTER TABLE ... DROP COLUMN`` natively;
-- aiosqlite ships with 3.51+ so the plain DROP COLUMN is safe in the
-- supported deploy targets. Note: any in-flight retry currently waiting
-- on ``next_retry_at`` is moved back to "ready" once the column is
-- removed (the lease pending query falls back to its v1.0 form).

DROP INDEX IF EXISTS idx_workflow_dlq_failed_at;
DROP INDEX IF EXISTS idx_workflow_dlq_workflow;
DROP TABLE IF EXISTS workflow_dlq;

ALTER TABLE workflow_steps DROP COLUMN next_retry_at;
ALTER TABLE workflow_steps DROP COLUMN retry_jitter;
ALTER TABLE workflow_steps DROP COLUMN retry_max_delay_seconds;
ALTER TABLE workflow_steps DROP COLUMN retry_initial_delay_seconds;
ALTER TABLE workflow_steps DROP COLUMN retry_policy;
ALTER TABLE workflow_steps DROP COLUMN max_attempts;
