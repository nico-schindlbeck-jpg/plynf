-- Rollback: 0006_oauth_metadata
-- Service: gateway
--
-- Drops the ``metadata`` column added by 0006_oauth_metadata.sql.
-- SQLite 3.35+ supports ``ALTER TABLE ... DROP COLUMN`` natively.

ALTER TABLE oauth_connections DROP COLUMN metadata;
