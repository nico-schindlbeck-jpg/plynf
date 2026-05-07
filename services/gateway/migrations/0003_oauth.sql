-- Migration: 0003_oauth
-- Service: gateway
-- v0.3 OAuth 2.0 authorization code flow:
--   oauth_connections — encrypted access/refresh tokens for third-party
--     providers. Tokens are AES-256-GCM encrypted at rest with the key from
--     ``Settings.oauth_encryption_key``.
--   oauth_states — short-lived rows holding the PKCE verifier and the
--     caller-supplied redirect_uri across the round-trip.

CREATE TABLE IF NOT EXISTS oauth_connections (
  id TEXT PRIMARY KEY,
  provider TEXT NOT NULL,
  user_id TEXT NOT NULL,
  user_login TEXT,
  scopes TEXT NOT NULL DEFAULT '[]',
  access_token_encrypted TEXT NOT NULL,
  refresh_token_encrypted TEXT,
  expires_at TIMESTAMP,
  created_at TIMESTAMP NOT NULL,
  last_refreshed_at TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_oauth_provider ON oauth_connections(provider);

CREATE TABLE IF NOT EXISTS oauth_states (
  state TEXT PRIMARY KEY,
  provider TEXT NOT NULL,
  redirect_uri TEXT NOT NULL,
  scopes TEXT NOT NULL DEFAULT '[]',
  pkce_verifier TEXT,
  created_at TIMESTAMP NOT NULL,
  used INTEGER NOT NULL DEFAULT 0
);
