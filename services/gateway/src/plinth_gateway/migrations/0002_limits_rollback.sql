-- Rollback: 0002_limits
-- Service: gateway
-- Strategy: drop tables.
-- Data preservation: NO — every per-agent limit configuration and
-- bucket-state snapshot is removed. After rollback, agents fall back
-- to the in-memory defaults coded in ``plinth_gateway.limits``.
--
-- Reverses 0002_limits.sql by dropping the rate-limit + cost-cap
-- primitives. There are no FKs across these tables; order is therefore
-- irrelevant.

DROP TABLE IF EXISTS rate_limit_snapshots;
DROP TABLE IF EXISTS agent_limits;
