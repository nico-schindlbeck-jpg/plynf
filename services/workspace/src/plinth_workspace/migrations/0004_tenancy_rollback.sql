-- Rollback: 0004_tenancy
-- Service: workspace
--
-- Reverses 0004_tenancy.sql by dropping the tenant index and the
-- ``workspaces.tenant_id`` column. SQLite 3.35+ supports DROP COLUMN
-- natively; the migration runner ships requiring 3.51+ via aiosqlite,
-- so a plain ``ALTER TABLE ... DROP COLUMN`` is safe.
--
-- Every existing workspaces row keeps its data; only the tenancy
-- discriminator goes away. After this rollback, callers running auth
-- modes that rely on tenant filtering will see all rows pooled together
-- under the legacy ``default`` namespace.

DROP INDEX IF EXISTS idx_workspaces_tenant;
ALTER TABLE workspaces DROP COLUMN tenant_id;
