-- Rollback: 0001_initial
-- Service: workspace
-- Strategy: drop tables + indices.
-- Data preservation: NO — every workspace, KV entry, file, snapshot, and
-- branch row goes away. Verify backups before running.
--
-- Reverses 0001_initial.sql by dropping the v0.1 baseline schema in
-- reverse-creation order so foreign-key constraints don't trip the
-- rollback. SQLite drops constraint indices automatically alongside the
-- tables; the explicit DROP INDEX lines keep behaviour identical across
-- Postgres and SQLite.

DROP INDEX IF EXISTS idx_branches_workspace;
DROP INDEX IF EXISTS idx_snapshots_workspace;
DROP INDEX IF EXISTS idx_files_lookup;
DROP INDEX IF EXISTS idx_kv_lookup;

DROP TABLE IF EXISTS branches;
DROP TABLE IF EXISTS snapshots;
DROP TABLE IF EXISTS file_entries;
DROP TABLE IF EXISTS kv_entries;
DROP TABLE IF EXISTS workspaces;
