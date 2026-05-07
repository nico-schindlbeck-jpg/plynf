# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 The Plinth Authors
"""Channel storage + business logic for the workspace service.

Channels
--------
A channel is a workspace-scoped, durable, monotonic-sequence message queue.
Messages are persisted to SQLite. Per-channel ``seq`` is allocated by
``MAX(seq) + 1`` while holding the connection's transaction.

Consumer cursors
----------------
Each ``consumer`` name maintains a per-channel cursor (the highest ``seq``
the consumer has acknowledged). On a non-peek receive without explicit
``since``, we resume from the cursor and advance it to the highest seq
returned. An explicit ``since`` always overrides the cursor (rewind).

Lazy create
-----------
``send`` creates the channel row on first use. ``receive`` returns 404 if
the channel has never been sent to — this gives clients a useful signal
that the name is wrong rather than silently returning empty pages.

Typed channels + DLQ (v0.5)
---------------------------
A channel that has a schema attached (see
:mod:`plinth_workspace.channel_schemas`) validates every payload at send
time. Failed validations get routed to a hidden ``<channel>.deadletter``
sub-channel and the caller receives a 422 ``SCHEMA_VIOLATION``.
``list_channels`` hides DLQ channels from the standard listing — they're
only reachable via the explicit DLQ endpoints.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import aiosqlite
from ulid import ULID

from .channel_schemas import (
    SchemaStore,
    deadletter_channel_name,
    is_deadletter_channel,
)
from .db import connect, iso, now_utc, parse_ts
from .exceptions import (
    ChannelNotFound,
    InvalidArguments,
    MessageNotFound,
    SchemaViolation,
    WorkspaceNotFound,
)
from .models import Channel, ChannelMessage, ChannelSchema

DEFAULT_LIMIT = 100
MAX_LIMIT = 1000


def _new_message_id() -> str:
    return f"msg_{ULID()}"


def _row_to_channel(row: aiosqlite.Row, message_count: int) -> Channel:
    return Channel(
        name=row["name"],
        workspace_id=row["workspace_id"],
        message_count=message_count,
        created_at=parse_ts(row["created_at"]),  # type: ignore[arg-type]
        last_send_at=parse_ts(row["last_send_at"]),
        last_receive_at=parse_ts(row["last_receive_at"]),
    )


def _row_to_message(row: aiosqlite.Row) -> ChannelMessage:
    return ChannelMessage(
        id=row["id"],
        channel=row["channel_name"],
        workspace_id=row["workspace_id"],
        seq=row["seq"],
        payload=json.loads(row["payload"]),
        sender=row["sender"],
        type=row["type"],
        correlation_id=row["correlation_id"],
        headers=json.loads(row["headers"] or "{}"),
        sent_at=parse_ts(row["sent_at"]),  # type: ignore[arg-type]
        delivered_at=parse_ts(row["delivered_at"]),
    )


class ChannelStore:
    """CRUD + receive-cursor logic for channels."""

    def __init__(
        self,
        db_path: Path,
        schema_store: SchemaStore | None = None,
    ) -> None:
        self.db_path = db_path
        # When ``schema_store`` is None we fall back to a freshly built one
        # against the same DB. Tests that want to inject a stub can pass
        # their own. Both stores share the connection pool because every
        # call opens a new connection.
        self.schema_store = schema_store or SchemaStore(db_path)

    # ------------------------------------------------------------------ helpers

    @staticmethod
    async def _assert_workspace(
        conn: aiosqlite.Connection,
        workspace_id: str,
    ) -> None:
        cur = await conn.execute(
            "SELECT 1 FROM workspaces WHERE id=?",
            (workspace_id,),
        )
        row = await cur.fetchone()
        await cur.close()
        if row is None:
            raise WorkspaceNotFound(workspace_id)

    @staticmethod
    async def _channel_row(
        conn: aiosqlite.Connection,
        workspace_id: str,
        name: str,
    ) -> aiosqlite.Row | None:
        cur = await conn.execute(
            "SELECT * FROM channels WHERE workspace_id=? AND name=?",
            (workspace_id, name),
        )
        row = await cur.fetchone()
        await cur.close()
        return row

    @staticmethod
    async def _message_count(
        conn: aiosqlite.Connection,
        workspace_id: str,
        name: str,
    ) -> int:
        cur = await conn.execute(
            "SELECT COUNT(*) FROM channel_messages "
            "WHERE workspace_id=? AND channel_name=?",
            (workspace_id, name),
        )
        row = await cur.fetchone()
        await cur.close()
        return int(row[0]) if row else 0

    # ------------------------------------------------------------------ send

    async def send(
        self,
        workspace_id: str,
        name: str,
        *,
        payload: Any,
        sender: str | None = None,
        type_: str | None = None,
        correlation_id: str | None = None,
        headers: dict[str, str] | None = None,
    ) -> ChannelMessage:
        """Append a message to ``name`` (lazy-create the channel).

        If the channel has a schema attached, the payload is validated. On
        failure the message is sent to ``<name>.deadletter`` instead and a
        :class:`SchemaViolation` is raised carrying the DLQ message ID +
        the validator errors.
        """

        if not name:
            raise InvalidArguments("channel name must be non-empty")

        # Schema validation gate. We deliberately re-fetch the schema from
        # the DB (rather than caching) so a producer racing with a schema
        # update sees the new schema as soon as the PUT commits.
        schema = await self.schema_store.get(workspace_id, name)
        if schema is not None and not is_deadletter_channel(name):
            errors = SchemaStore.validate(payload, schema)
            if errors is not None:
                dlq_msg = await self._send_to_deadletter(
                    workspace_id,
                    name,
                    payload=payload,
                    sender=sender,
                    type_=type_,
                    correlation_id=correlation_id,
                    headers=headers,
                    validation_errors=errors,
                    schema_version=schema.version,
                )
                raise SchemaViolation(
                    channel=name,
                    errors=errors,
                    deadletter_msg_id=dlq_msg.id,
                    workspace_id=workspace_id,
                )

        return await self._send_raw(
            workspace_id,
            name,
            payload=payload,
            sender=sender,
            type_=type_,
            correlation_id=correlation_id,
            headers=headers,
        )

    # ------------------------------------------------------------------ raw send

    async def _send_raw(
        self,
        workspace_id: str,
        name: str,
        *,
        payload: Any,
        sender: str | None = None,
        type_: str | None = None,
        correlation_id: str | None = None,
        headers: dict[str, str] | None = None,
    ) -> ChannelMessage:
        """Persist a message bypassing schema validation.

        Used directly by the DLQ writer (the message *is* the validation
        failure — re-validating it would be circular) and by the
        already-validated path in :meth:`send`.
        """

        if not name:
            raise InvalidArguments("channel name must be non-empty")

        async with connect(self.db_path) as conn:
            await self._assert_workspace(conn, workspace_id)

            ts = now_utc()
            row = await self._channel_row(conn, workspace_id, name)
            if row is None:
                await conn.execute(
                    "INSERT INTO channels (workspace_id, name, created_at) "
                    "VALUES (?, ?, ?)",
                    (workspace_id, name, iso(ts)),
                )

            cur = await conn.execute(
                "SELECT MAX(seq) FROM channel_messages "
                "WHERE workspace_id=? AND channel_name=?",
                (workspace_id, name),
            )
            seq_row = await cur.fetchone()
            await cur.close()
            next_seq = int((seq_row[0] or 0) + 1)

            msg_id = _new_message_id()
            headers_json = json.dumps(headers or {}, sort_keys=True)
            payload_json = json.dumps(payload, sort_keys=True)

            await conn.execute(
                "INSERT INTO channel_messages "
                "(id, workspace_id, channel_name, seq, payload, sender, "
                " type, correlation_id, headers, sent_at, delivered_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL)",
                (
                    msg_id,
                    workspace_id,
                    name,
                    next_seq,
                    payload_json,
                    sender,
                    type_,
                    correlation_id,
                    headers_json,
                    iso(ts),
                ),
            )
            await conn.execute(
                "UPDATE channels SET last_send_at=? WHERE workspace_id=? AND name=?",
                (iso(ts), workspace_id, name),
            )
            await conn.commit()

            return ChannelMessage(
                id=msg_id,
                channel=name,
                workspace_id=workspace_id,
                seq=next_seq,
                payload=payload,
                sender=sender,
                type=type_,
                correlation_id=correlation_id,
                headers=headers or {},
                sent_at=ts,
                delivered_at=None,
            )

    # ------------------------------------------------------------------ DLQ writer

    async def _send_to_deadletter(
        self,
        workspace_id: str,
        channel_name: str,
        *,
        payload: Any,
        sender: str | None,
        type_: str | None,
        correlation_id: str | None,
        headers: dict[str, str] | None,
        validation_errors: list[dict[str, Any]],
        schema_version: int | None = None,
    ) -> ChannelMessage:
        """Persist a failed-validation message to ``<channel>.deadletter``.

        The DLQ message preserves the original headers (so consumers can
        replay with provenance) plus a few ``x-`` prefixed extras that
        make the failure self-describing:

        * ``x-original-channel`` — the channel the producer aimed at.
        * ``x-validation-errors`` — JSON-serialised :func:`validation_errors`.
        * ``x-failed-at`` — the wall-clock time of the failure.
        * ``x-schema-version`` — the schema version that rejected the payload.
        """

        dlq_name = deadletter_channel_name(channel_name)
        merged_headers: dict[str, str] = {}
        if headers:
            merged_headers.update(headers)
        merged_headers["x-original-channel"] = channel_name
        merged_headers["x-validation-errors"] = json.dumps(validation_errors)
        merged_headers["x-failed-at"] = iso(now_utc())
        if schema_version is not None:
            merged_headers["x-schema-version"] = str(schema_version)

        return await self._send_raw(
            workspace_id,
            dlq_name,
            payload=payload,
            sender=sender,
            type_=type_,
            correlation_id=correlation_id,
            headers=merged_headers,
        )

    # ------------------------------------------------------------------ receive

    async def receive(
        self,
        workspace_id: str,
        name: str,
        *,
        since: int | None = None,
        limit: int | None = None,
        consumer: str | None = None,
        peek: bool = False,
    ) -> list[ChannelMessage]:
        """Return messages with ``seq > effective_since`` (ordered)."""

        if limit is None:
            limit = DEFAULT_LIMIT
        if limit < 1:
            raise InvalidArguments("limit must be >= 1")
        if limit > MAX_LIMIT:
            limit = MAX_LIMIT

        async with connect(self.db_path) as conn:
            await self._assert_workspace(conn, workspace_id)

            ch_row = await self._channel_row(conn, workspace_id, name)
            if ch_row is None:
                raise ChannelNotFound(workspace_id, name)

            # Resolve effective_since: explicit `since` wins, else cursor, else 0.
            effective_since = 0
            if since is not None:
                effective_since = since
            elif consumer is not None:
                cur = await conn.execute(
                    "SELECT cursor FROM channel_consumers "
                    "WHERE workspace_id=? AND channel_name=? AND consumer=?",
                    (workspace_id, name, consumer),
                )
                row = await cur.fetchone()
                await cur.close()
                if row is not None:
                    effective_since = int(row["cursor"])

            cur = await conn.execute(
                "SELECT * FROM channel_messages "
                "WHERE workspace_id=? AND channel_name=? AND seq > ? "
                "ORDER BY seq ASC LIMIT ?",
                (workspace_id, name, effective_since, limit),
            )
            rows = await cur.fetchall()
            await cur.close()

            messages = [_row_to_message(r) for r in rows]
            if not messages:
                return messages

            ts = now_utc()
            highest_seq = messages[-1].seq

            if not peek:
                # Set delivered_at on rows that haven't been delivered yet.
                undelivered_ids = [m.id for m in messages if m.delivered_at is None]
                if undelivered_ids:
                    placeholders = ",".join("?" for _ in undelivered_ids)
                    await conn.execute(
                        "UPDATE channel_messages SET delivered_at=? "
                        f"WHERE id IN ({placeholders}) AND delivered_at IS NULL",
                        (iso(ts), *undelivered_ids),
                    )
                    # Mirror the write back into our in-memory copy so callers
                    # see the same timestamp the DB will return next time.
                    for m in messages:
                        if m.delivered_at is None:
                            m.delivered_at = ts

                if consumer is not None:
                    await conn.execute(
                        "INSERT INTO channel_consumers "
                        "(workspace_id, channel_name, consumer, cursor, updated_at) "
                        "VALUES (?, ?, ?, ?, ?) "
                        "ON CONFLICT(workspace_id, channel_name, consumer) DO UPDATE SET "
                        "  cursor=excluded.cursor, updated_at=excluded.updated_at",
                        (workspace_id, name, consumer, highest_seq, iso(ts)),
                    )

            await conn.execute(
                "UPDATE channels SET last_receive_at=? "
                "WHERE workspace_id=? AND name=?",
                (iso(ts), workspace_id, name),
            )
            await conn.commit()
            return messages

    # ------------------------------------------------------------------ delete msg

    async def delete_message(
        self,
        workspace_id: str,
        name: str,
        message_id: str,
    ) -> None:
        async with connect(self.db_path) as conn:
            await self._assert_workspace(conn, workspace_id)

            cur = await conn.execute(
                "DELETE FROM channel_messages "
                "WHERE id=? AND workspace_id=? AND channel_name=?",
                (message_id, workspace_id, name),
            )
            deleted = cur.rowcount
            await cur.close()
            await conn.commit()
            if not deleted:
                raise MessageNotFound(workspace_id, name, message_id)

    # ------------------------------------------------------------------ list / get

    async def list_channels(
        self,
        workspace_id: str,
        *,
        include_deadletters: bool = False,
    ) -> list[Channel]:
        """List all channels in ``workspace_id``.

        DLQ sub-channels (``<name>.deadletter``) are filtered out by default
        — they're only reachable via the explicit ``deadletter`` endpoint.
        Set ``include_deadletters=True`` for an exhaustive listing (used by
        admin tools and tests).
        """

        async with connect(self.db_path) as conn:
            await self._assert_workspace(conn, workspace_id)

            cur = await conn.execute(
                "SELECT c.workspace_id, c.name, c.created_at, c.last_send_at, "
                "       c.last_receive_at, "
                "       COALESCE(m.cnt, 0) AS message_count "
                "FROM channels c "
                "LEFT JOIN ( "
                "  SELECT workspace_id, channel_name, COUNT(*) AS cnt "
                "  FROM channel_messages "
                "  WHERE workspace_id=? "
                "  GROUP BY workspace_id, channel_name "
                ") m ON m.workspace_id=c.workspace_id AND m.channel_name=c.name "
                "WHERE c.workspace_id=? "
                "ORDER BY c.created_at ASC",
                (workspace_id, workspace_id),
            )
            rows = await cur.fetchall()
            await cur.close()

            out: list[Channel] = []
            for row in rows:
                if not include_deadletters and is_deadletter_channel(row["name"]):
                    continue
                out.append(
                    Channel(
                        name=row["name"],
                        workspace_id=row["workspace_id"],
                        message_count=int(row["message_count"]),
                        created_at=parse_ts(row["created_at"]),  # type: ignore[arg-type]
                        last_send_at=parse_ts(row["last_send_at"]),
                        last_receive_at=parse_ts(row["last_receive_at"]),
                    )
                )
            return out

    # ------------------------------------------------------------------ DLQ APIs

    async def list_deadletters(
        self,
        workspace_id: str,
        channel_name: str,
        *,
        since: int | None = None,
        limit: int | None = None,
    ) -> list[ChannelMessage]:
        """Return DLQ messages for ``channel_name``.

        Returns an empty list when the DLQ channel does not exist (i.e. no
        message has ever failed validation), avoiding a noisy 404 for the
        common "is the DLQ empty?" check. ``WorkspaceNotFound`` still
        propagates because that's a real client mistake.
        """

        if limit is None:
            limit = DEFAULT_LIMIT
        if limit < 1:
            raise InvalidArguments("limit must be >= 1")
        if limit > MAX_LIMIT:
            limit = MAX_LIMIT

        dlq_name = deadletter_channel_name(channel_name)
        effective_since = since or 0

        async with connect(self.db_path) as conn:
            await self._assert_workspace(conn, workspace_id)
            ch_row = await self._channel_row(conn, workspace_id, dlq_name)
            if ch_row is None:
                return []

            cur = await conn.execute(
                "SELECT * FROM channel_messages "
                "WHERE workspace_id=? AND channel_name=? AND seq > ? "
                "ORDER BY seq ASC LIMIT ?",
                (workspace_id, dlq_name, effective_since, limit),
            )
            rows = await cur.fetchall()
            await cur.close()
            return [_row_to_message(r) for r in rows]

    async def get_deadletter_message(
        self,
        workspace_id: str,
        channel_name: str,
        message_id: str,
    ) -> ChannelMessage:
        """Fetch a single DLQ message; 404s on miss."""

        dlq_name = deadletter_channel_name(channel_name)
        async with connect(self.db_path) as conn:
            await self._assert_workspace(conn, workspace_id)
            cur = await conn.execute(
                "SELECT * FROM channel_messages "
                "WHERE id=? AND workspace_id=? AND channel_name=?",
                (message_id, workspace_id, dlq_name),
            )
            row = await cur.fetchone()
            await cur.close()
            if row is None:
                raise MessageNotFound(workspace_id, dlq_name, message_id)
            return _row_to_message(row)

    async def replay_deadletter(
        self,
        workspace_id: str,
        channel_name: str,
        message_id: str,
    ) -> ChannelMessage:
        """Re-validate + re-send a DLQ message to its main channel.

        The schema is re-fetched at replay time, so a relaxation of the
        rules between failure and replay is honoured. If validation still
        fails the message stays in the DLQ and we raise ``SchemaViolation``
        with the new errors. On success the original DLQ row is removed.
        """

        dlq_msg = await self.get_deadletter_message(
            workspace_id, channel_name, message_id
        )

        schema = await self.schema_store.get(workspace_id, channel_name)
        if schema is not None:
            errors = SchemaStore.validate(dlq_msg.payload, schema)
            if errors is not None:
                raise SchemaViolation(
                    channel=channel_name,
                    errors=errors,
                    deadletter_msg_id=message_id,
                    workspace_id=workspace_id,
                )

        # Strip the synthetic DLQ headers before resending so the replayed
        # message looks like an organic send (preserve original headers
        # only).
        replay_headers = {
            k: v
            for k, v in (dlq_msg.headers or {}).items()
            if not k.startswith("x-")
            or k
            not in {
                "x-original-channel",
                "x-validation-errors",
                "x-failed-at",
                "x-schema-version",
            }
        }

        new_msg = await self._send_raw(
            workspace_id,
            channel_name,
            payload=dlq_msg.payload,
            sender=dlq_msg.sender,
            type_=dlq_msg.type,
            correlation_id=dlq_msg.correlation_id,
            headers=replay_headers,
        )

        # Remove the message from the DLQ now that it lives on the main
        # channel. Best-effort: a missing row is fine (someone already
        # dropped it concurrently).
        await self._delete_message_silent(
            workspace_id, deadletter_channel_name(channel_name), message_id
        )
        return new_msg

    async def drop_deadletter(
        self,
        workspace_id: str,
        channel_name: str,
        message_id: str,
    ) -> None:
        """Delete a DLQ message without replaying it."""

        # Reuse the standard delete path but scoped to the DLQ name so a
        # caller can't accidentally drop a main-channel message via this
        # entrypoint.
        await self.delete_message(
            workspace_id,
            deadletter_channel_name(channel_name),
            message_id,
        )

    async def _delete_message_silent(
        self,
        workspace_id: str,
        name: str,
        message_id: str,
    ) -> None:
        """``delete_message`` that swallows :class:`MessageNotFound`."""

        try:
            await self.delete_message(workspace_id, name, message_id)
        except MessageNotFound:
            pass

    # ------------------------------------------------------------------ schema convenience

    async def get_schema(
        self,
        workspace_id: str,
        channel_name: str,
    ) -> ChannelSchema | None:
        """Pass-through to the schema store; kept here for API symmetry."""
        return await self.schema_store.get(workspace_id, channel_name)

    async def set_schema(
        self,
        workspace_id: str,
        channel_name: str,
        schema_doc: dict[str, Any],
    ) -> ChannelSchema:
        """Pass-through to the schema store; kept here for API symmetry."""
        return await self.schema_store.set(workspace_id, channel_name, schema_doc)

    async def delete_schema(
        self,
        workspace_id: str,
        channel_name: str,
    ) -> bool:
        """Pass-through to the schema store; kept here for API symmetry."""
        return await self.schema_store.delete(workspace_id, channel_name)

    async def get_channel(self, workspace_id: str, name: str) -> Channel:
        async with connect(self.db_path) as conn:
            await self._assert_workspace(conn, workspace_id)
            row = await self._channel_row(conn, workspace_id, name)
            if row is None:
                raise ChannelNotFound(workspace_id, name)
            count = await self._message_count(conn, workspace_id, name)
            return _row_to_channel(row, count)

    async def delete_channel(self, workspace_id: str, name: str) -> None:
        async with connect(self.db_path) as conn:
            await self._assert_workspace(conn, workspace_id)
            row = await self._channel_row(conn, workspace_id, name)
            if row is None:
                raise ChannelNotFound(workspace_id, name)

            await conn.execute(
                "DELETE FROM channel_messages "
                "WHERE workspace_id=? AND channel_name=?",
                (workspace_id, name),
            )
            await conn.execute(
                "DELETE FROM channel_consumers "
                "WHERE workspace_id=? AND channel_name=?",
                (workspace_id, name),
            )
            await conn.execute(
                "DELETE FROM channels WHERE workspace_id=? AND name=?",
                (workspace_id, name),
            )
            await conn.commit()
