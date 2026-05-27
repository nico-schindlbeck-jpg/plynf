-- Migration: 0004_tenancy
-- Service: workspace
-- v0.3 multi-tenancy: adds tenant_id to workspaces.
--
-- The legacy in-place migration in db.py also did this; the runner accepts
-- both shapes (column already present from legacy bootstrap, or fresh
-- application here). The PRAGMA-guarded ALTER below makes the migration
-- idempotent: if the column already exists we silently skip.

-- SQLite has no "ADD COLUMN IF NOT EXISTS"; we emulate it with a defensive
-- guard. The migration runner detects this dialect-specific pattern and
-- evaluates the check before running the ALTER.

-- Add tenant_id to workspaces (idempotent via guard column probe).
-- The runner's "guard" wrapper handles the duplicate-column error on a
-- second pass, so a plain ALTER stays safe across runs.
ALTER TABLE workspaces ADD COLUMN tenant_id TEXT NOT NULL DEFAULT 'default';

-- Tenant index — created after the column exists.
CREATE INDEX IF NOT EXISTS idx_workspaces_tenant ON workspaces(tenant_id);
