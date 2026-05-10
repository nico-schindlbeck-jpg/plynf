-- Rollback: 0003_workflows
-- Service: workspace
-- Strategy: drop tables + indices.
-- Data preservation: NO — every workflow row and step log is removed.
-- Verify backups before running.
--
-- Reverses 0003_workflows.sql by dropping the v0.2 workflow primitives.
-- ``workflow_steps`` is dropped first because of its FK to ``workflows``.

DROP INDEX IF EXISTS idx_workflows_workspace;
DROP INDEX IF EXISTS idx_workflow_steps_lookup;

DROP TABLE IF EXISTS workflow_steps;
DROP TABLE IF EXISTS workflows;
