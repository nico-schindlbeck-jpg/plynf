-- Migration: 0002_signing_keys
-- Service: identity
-- v0.4 RS256 capability tokens with key rotation:
--   signing_keys — encrypted private RSA-2048 + corresponding public PEMs.
--
-- The ``private_key_pem_encrypted`` column stores AES-256-GCM ciphertext
-- (base64 of nonce || ciphertext) wrapped with the key from
-- ``Settings.identity_keys_encryption_key``.

CREATE TABLE IF NOT EXISTS signing_keys (
  kid TEXT PRIMARY KEY,
  alg TEXT NOT NULL,
  public_key_pem TEXT NOT NULL,
  private_key_pem_encrypted TEXT NOT NULL,
  created_at TIMESTAMP NOT NULL,
  rotated_in_at TIMESTAMP,
  expires_at TIMESTAMP NOT NULL,
  active INTEGER NOT NULL DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_keys_active
  ON signing_keys(active, expires_at);
