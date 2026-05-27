-- Migration: 0002_limits
-- Service: gateway
-- v0.2 rate limiting + cost caps:
--   agent_limits  — per-agent RPM/burst + cost-cap configuration.
--   rate_limit_snapshots — bucket-state checkpoint for graceful restarts.

CREATE TABLE IF NOT EXISTS agent_limits (
  agent_id TEXT PRIMARY KEY,
  rpm INTEGER NOT NULL DEFAULT 60,
  burst INTEGER NOT NULL DEFAULT 20,
  cost_cap_usd_hour REAL NOT NULL DEFAULT 1.0,
  cost_cap_usd_day REAL NOT NULL DEFAULT 10.0,
  updated_at TIMESTAMP NOT NULL
);

-- Rate-limit state is held in-memory (token buckets per agent) but persisted
-- as a snapshot so a graceful shutdown can restore the bucket level on
-- restart.
CREATE TABLE IF NOT EXISTS rate_limit_snapshots (
  agent_id TEXT PRIMARY KEY,
  tokens REAL NOT NULL,
  last_refill TIMESTAMP NOT NULL
);
