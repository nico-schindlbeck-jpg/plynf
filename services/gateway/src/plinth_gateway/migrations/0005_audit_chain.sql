-- Migration: 0005_audit_chain
-- Service: gateway
--
-- v1.0 Compliance Scaffolding — tamper-evident audit chain.
--
-- Adds two new columns to ``audit_events``:
--   * ``prev_hash`` — sha256 hex of the previous event's ``event_hash`` (or
--     NULL for the chain genesis / pre-v1.0 rows).
--   * ``event_hash`` — sha256 hex of ``prev_hash || canonical_json(event)``.
--
-- Existing rows keep both columns as NULL. The verify endpoint only checks
-- rows that have a non-NULL ``event_hash`` so legacy data doesn't break
-- the chain.

ALTER TABLE audit_events ADD COLUMN prev_hash TEXT;
ALTER TABLE audit_events ADD COLUMN event_hash TEXT;
CREATE INDEX IF NOT EXISTS idx_audit_chain ON audit_events(id, event_hash);
