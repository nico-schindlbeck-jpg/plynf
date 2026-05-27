-- ROLLBACK MIGRATION: 0002_signing_keys
-- Service: identity
-- WARNING: This will DROP/REMOVE schema. Data in dropped tables is unrecoverable.
-- Verify backups before running in production.
--
-- Reverses 0002_signing_keys.sql by dropping the index and the
-- ``signing_keys`` table introduced in v0.4. All RSA private keys (even
-- the encrypted-at-rest ones) and their public PEMs are lost — this
-- effectively reverts the identity service to HS256-only token issuance,
-- which means clients holding RS256 tokens signed by these keys can no
-- longer be verified locally either.
--
-- Restore-from-backup is the only recovery path. Issued tokens stay
-- valid client-side until expiry but the gateway can no longer verify
-- them via JWKS once the table is gone.

DROP INDEX IF EXISTS idx_keys_active;
DROP TABLE IF EXISTS signing_keys;
