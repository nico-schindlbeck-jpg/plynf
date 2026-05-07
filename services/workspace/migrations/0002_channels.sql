-- Migration: 0002_channels
-- Service: workspace
-- v0.2 channels primitive: typed, persistent message queues per workspace.
-- Adds channels, channel_messages, channel_consumers tables.

CREATE TABLE IF NOT EXISTS channels (
  workspace_id TEXT NOT NULL,
  name TEXT NOT NULL,
  created_at TIMESTAMP NOT NULL,
  last_send_at TIMESTAMP,
  last_receive_at TIMESTAMP,
  PRIMARY KEY (workspace_id, name),
  FOREIGN KEY (workspace_id) REFERENCES workspaces(id)
);

CREATE TABLE IF NOT EXISTS channel_messages (
  id TEXT PRIMARY KEY,
  workspace_id TEXT NOT NULL,
  channel_name TEXT NOT NULL,
  seq INTEGER NOT NULL,
  payload TEXT NOT NULL,
  sender TEXT,
  type TEXT,
  correlation_id TEXT,
  headers TEXT NOT NULL DEFAULT '{}',
  sent_at TIMESTAMP NOT NULL,
  delivered_at TIMESTAMP,
  FOREIGN KEY (workspace_id, channel_name) REFERENCES channels(workspace_id, name)
);

CREATE INDEX IF NOT EXISTS idx_channel_messages_lookup
  ON channel_messages(workspace_id, channel_name, seq);

CREATE TABLE IF NOT EXISTS channel_consumers (
  workspace_id TEXT NOT NULL,
  channel_name TEXT NOT NULL,
  consumer TEXT NOT NULL,
  cursor INTEGER NOT NULL DEFAULT 0,
  updated_at TIMESTAMP NOT NULL,
  PRIMARY KEY (workspace_id, channel_name, consumer)
);
