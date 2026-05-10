-- Rollback: 0002_channels
-- Service: workspace
-- Strategy: drop tables + indices.
-- Data preservation: NO — every channel, message, and consumer cursor is
-- lost. Verify backups before running.
--
-- Reverses 0002_channels.sql by dropping the v0.2 channel primitives.
-- ``channel_messages`` is dropped first because of its composite FK to
-- ``channels``. ``channel_consumers`` has no FK so order is irrelevant.

DROP INDEX IF EXISTS idx_channel_messages_lookup;

DROP TABLE IF EXISTS channel_consumers;
DROP TABLE IF EXISTS channel_messages;
DROP TABLE IF EXISTS channels;
