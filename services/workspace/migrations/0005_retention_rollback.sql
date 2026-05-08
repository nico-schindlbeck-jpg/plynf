-- Rollback: 0005_retention
-- Service: workspace
--
-- Drops the retention_policies table created by 0005_retention.sql.
-- All retention configuration (per-workspace TTL / version-cap settings)
-- is lost. The GC engine simply gets back to its v0.3 behaviour: nothing
-- to enforce, every blob lives forever.

DROP TABLE IF EXISTS retention_policies;
