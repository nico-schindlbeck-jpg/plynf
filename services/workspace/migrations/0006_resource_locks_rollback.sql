-- Rollback: 0006_resource_locks
-- Service: workspace
--
-- Drops the resource_locks table created by 0006_resource_locks.sql.
-- Any in-flight lock state is lost; subsequent calls to the v0.6 lock
-- endpoints will return 404 / 500 until the migration is reapplied.

DROP TABLE IF EXISTS resource_locks;
