# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 The Plinth Authors
"""SQL DDL emitted on ``init_schema()`` for the identity service."""

from __future__ import annotations

SQLITE_SCHEMA = """
CREATE TABLE IF NOT EXISTS issued_tokens (
  jti TEXT PRIMARY KEY,
  agent_id TEXT NOT NULL,
  tenant_id TEXT NOT NULL,
  workspace_id TEXT,
  scopes TEXT NOT NULL,
  issued_at TIMESTAMP NOT NULL,
  expires_at TIMESTAMP NOT NULL,
  revoked INTEGER NOT NULL DEFAULT 0,
  revoked_at TIMESTAMP,
  metadata TEXT NOT NULL DEFAULT '{}'
);

CREATE INDEX IF NOT EXISTS idx_tokens_agent
  ON issued_tokens(agent_id, issued_at DESC);
CREATE INDEX IF NOT EXISTS idx_tokens_tenant
  ON issued_tokens(tenant_id, issued_at DESC);
CREATE INDEX IF NOT EXISTS idx_tokens_expires
  ON issued_tokens(expires_at);
CREATE INDEX IF NOT EXISTS idx_tokens_revoked_at
  ON issued_tokens(revoked, revoked_at);

CREATE TABLE IF NOT EXISTS tenants (
  id TEXT PRIMARY KEY,
  name TEXT NOT NULL,
  metadata TEXT NOT NULL DEFAULT '{}',
  created_at TIMESTAMP NOT NULL
);
"""

POSTGRES_SCHEMA = """
CREATE TABLE IF NOT EXISTS issued_tokens (
  jti TEXT PRIMARY KEY,
  agent_id TEXT NOT NULL,
  tenant_id TEXT NOT NULL,
  workspace_id TEXT,
  scopes TEXT NOT NULL,
  issued_at TIMESTAMPTZ NOT NULL,
  expires_at TIMESTAMPTZ NOT NULL,
  revoked SMALLINT NOT NULL DEFAULT 0,
  revoked_at TIMESTAMPTZ,
  metadata TEXT NOT NULL DEFAULT '{}'
);

CREATE INDEX IF NOT EXISTS idx_tokens_agent
  ON issued_tokens(agent_id, issued_at DESC);
CREATE INDEX IF NOT EXISTS idx_tokens_tenant
  ON issued_tokens(tenant_id, issued_at DESC);
CREATE INDEX IF NOT EXISTS idx_tokens_expires
  ON issued_tokens(expires_at);
CREATE INDEX IF NOT EXISTS idx_tokens_revoked_at
  ON issued_tokens(revoked, revoked_at);

CREATE TABLE IF NOT EXISTS tenants (
  id TEXT PRIMARY KEY,
  name TEXT NOT NULL,
  metadata TEXT NOT NULL DEFAULT '{}',
  created_at TIMESTAMPTZ NOT NULL
);
"""


__all__ = ["POSTGRES_SCHEMA", "SQLITE_SCHEMA"]
