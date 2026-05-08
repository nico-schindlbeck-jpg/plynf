-- Rollback: 0004_tenancy
-- Service: gateway
--
-- Reverses 0004_tenancy.sql by dropping the tenancy indices and the
-- ``tenant_id`` columns added to tools, audit_events, agent_limits,
-- oauth_connections, and oauth_states. SQLite 3.35+ supports DROP
-- COLUMN natively; aiosqlite ships with 3.51+, so plain
-- ``ALTER TABLE ... DROP COLUMN`` is safe.

DROP INDEX IF EXISTS idx_audit_tenant;
DROP INDEX IF EXISTS idx_tools_tenant;
DROP INDEX IF EXISTS idx_oauth_tenant;

ALTER TABLE tools DROP COLUMN tenant_id;
ALTER TABLE audit_events DROP COLUMN tenant_id;
ALTER TABLE agent_limits DROP COLUMN tenant_id;
ALTER TABLE oauth_connections DROP COLUMN tenant_id;
ALTER TABLE oauth_states DROP COLUMN tenant_id;
