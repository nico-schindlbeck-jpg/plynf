-- Migration: 0006_oauth_metadata
-- Service: gateway
--
-- v1.5 — Per-connection provider metadata.
--
-- Adds a ``metadata`` JSON column to ``oauth_connections`` so per-connection
-- provider-specific state (Atlassian's ``cloudid``, Salesforce's
-- ``instance_url``) can be persisted alongside the encrypted tokens. The
-- gateway proxy reads this back and injects:
--
--   * ``X-Plinth-OAuth-Cloudid`` for Atlassian connections
--   * ``X-Plinth-OAuth-InstanceUrl`` for Salesforce connections
--
-- onto every proxied invocation so MCP servers can address per-org REST
-- bases without re-doing the OAuth dance.
--
-- Existing rows leave ``metadata`` NULL (treated as the empty dict in code).

ALTER TABLE oauth_connections ADD COLUMN metadata TEXT;
