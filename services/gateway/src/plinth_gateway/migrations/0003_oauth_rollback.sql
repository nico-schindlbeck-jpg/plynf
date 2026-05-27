-- ROLLBACK MIGRATION: 0003_oauth
-- Service: gateway
-- WARNING: This will DROP/REMOVE schema. Data in dropped tables is unrecoverable.
-- Verify backups before running in production.
--
-- Reverses 0003_oauth.sql by dropping the two tables (oauth_connections and
-- oauth_states) plus the index on oauth_connections.provider. All stored
-- third-party OAuth credentials are lost — encrypted access/refresh tokens
-- and any in-flight authorization-code state are gone after this runs.
--
-- Order matters only for the index → table relationship; SQLite drops the
-- index automatically when the table goes, but the explicit DROP keeps
-- behaviour identical across Postgres and SQLite back-ends.

DROP INDEX IF EXISTS idx_oauth_provider;
DROP TABLE IF EXISTS oauth_connections;
DROP TABLE IF EXISTS oauth_states;
