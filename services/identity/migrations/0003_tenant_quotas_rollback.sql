-- Rollback for 0003_tenant_quotas
-- Drops tenant_quotas and tenant_usage tables.
DROP TABLE IF EXISTS tenant_quotas;
DROP TABLE IF EXISTS tenant_usage;
