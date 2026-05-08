# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 The Plinth Authors
"""Workspace channels: persistent, typed message queues.

A channel is a workspace-scoped FIFO of :class:`ChannelMessage` objects with
monotonic per-channel sequence numbers. Channels are created lazily on the
first ``send``; ``receive`` either returns from the start or resumes a named
consumer's cursor on the server.

The public surface is :class:`ChannelsProxy`, reachable via ``ws.channels``.
It maps directly onto the v0.2 Channels API in :doc:`/CONTRACTS.md`:

* :meth:`ChannelsProxy.send` -> ``POST .../channels/{name}/send``
* :meth:`ChannelsProxy.receive` -> ``GET .../channels/{name}/receive``
* :meth:`ChannelsProxy.ack` / :meth:`ChannelsProxy.delete` ->
  ``DELETE .../channels/{name}/messages/{id}``
* :meth:`ChannelsProxy.list` -> ``GET .../channels``
* :meth:`ChannelsProxy.get` -> ``GET .../channels/{name}``
* :meth:`ChannelsProxy.delete_channel` -> ``DELETE .../channels/{name}``

:meth:`ChannelsProxy.wait` is a client-side helper that polls
:meth:`receive` until a message arrives or the timeout elapses.
"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING, Any
from urllib.parse import quote

from .exceptions import ChannelNotFound, MessageNotFound, WorkspaceNotFound
from .models import (
    Channel,
    ChannelMessage,
    ChannelSchema,
    ReplayBatchResult,
    SchemaCheckResult,
)

if TYPE_CHECKING:
    from .workspace import Workspace


def _ec(name: str) -> str:
    """Percent-encode a channel name for safe URL embedding."""
    return quote(name, safe="")


# ---------------------------------------------------------------------------
# ChannelsProxy -- exposed via ``ws.channels``.
# ---------------------------------------------------------------------------


class ChannelsProxy:
    """API surface for the workspace's channels.

    Held as ``ws.channels`` (lazily instantiated on first attribute
    access). All HTTP calls go through the workspace's
    :class:`plinth._http.HTTPClient`, so authentication and error mapping
    are identical to the rest of the SDK.
    """

    def __init__(self, workspace: Workspace) -> None:
        self._ws = workspace

    # ------------------------------------------------------------------
    # send
    # ------------------------------------------------------------------

    def send(
        self,
        channel: str,
        payload: Any,
        *,
        sender: str | None = None,
        type: str | None = None,  # noqa: A002 - mirrors API field name
        correlation_id: str | None = None,
        headers: dict[str, str] | None = None,
    ) -> ChannelMessage:
        """Send ``payload`` on ``channel``.

        The channel is created lazily on first send. The returned
        :class:`ChannelMessage` carries the server-assigned ``id``,
        ``seq`` and ``sent_at`` timestamp.

        Args:
            channel: The channel name. Created on demand.
            payload: Any JSON-serialisable payload.
            sender: Optional descriptive label (agent ID, etc.).
            type: Optional message type for filtering on the receive side.
            correlation_id: Optional correlation key for request/response.
            headers: Optional string-string metadata.

        Returns:
            The newly created :class:`ChannelMessage`.
        """
        body: dict[str, Any] = {"payload": payload}
        if sender is not None:
            body["sender"] = sender
        if type is not None:
            body["type"] = type
        if correlation_id is not None:
            body["correlation_id"] = correlation_id
        if headers is not None:
            body["headers"] = headers

        response = self._ws._http.post(
            f"/v1/workspaces/{self._ws.id}/channels/{_ec(channel)}/send",
            json=body,
            params=self._ws._branch_params(),
            not_found_class=WorkspaceNotFound,
        )
        return ChannelMessage.model_validate(response.json())

    # ------------------------------------------------------------------
    # receive
    # ------------------------------------------------------------------

    def receive(
        self,
        channel: str,
        *,
        consumer: str | None = None,
        since: int | None = None,
        limit: int = 100,
        peek: bool = False,
    ) -> list[ChannelMessage]:
        """Receive a batch of messages from ``channel``.

        Args:
            channel: The channel name.
            consumer: Optional named consumer. The server tracks a cursor
                per consumer so subsequent calls without ``since`` resume
                where the last one left off.
            since: Explicit sequence override -- returns messages with
                ``seq > since``. Combine with ``peek=True`` for explicit
                scans without disturbing cursor state.
            limit: Maximum messages to return (server default 100, max
                1000).
            peek: When ``True``, the consumer cursor is *not* advanced.

        Returns:
            A list of :class:`ChannelMessage` in seq order.

        Raises:
            ChannelNotFound: When the channel does not exist on the server.
        """
        params: dict[str, Any] = {"limit": limit}
        if consumer is not None:
            params["consumer"] = consumer
        if since is not None:
            params["since"] = since
        if peek:
            params["peek"] = "true"

        data = self._ws._http.get_json(
            f"/v1/workspaces/{self._ws.id}/channels/{_ec(channel)}/receive",
            params=params,
            not_found_class=ChannelNotFound,
        )
        return [ChannelMessage.model_validate(m) for m in data.get("messages", [])]

    # ------------------------------------------------------------------
    # ack / delete
    # ------------------------------------------------------------------

    def ack(self, msg_or_id: ChannelMessage | str) -> None:
        """Acknowledge (delete) a message on the server.

        Accepts a full :class:`ChannelMessage` (preferred) so the channel
        name can be read off the model. Passing only a message ID raises
        :class:`ValueError` because the channel name is required to build
        the DELETE URL.

        Args:
            msg_or_id: A :class:`ChannelMessage` or its bare ID string.

        Raises:
            ValueError: When called with a bare message-ID string.
            MessageNotFound: When the server returns 404 for the message.
        """
        if isinstance(msg_or_id, str):
            raise ValueError(
                "ack(message_id) requires a ChannelMessage object; use ack(msg) instead"
            )
        self._ws._http.delete(
            f"/v1/workspaces/{self._ws.id}/channels/{_ec(msg_or_id.channel)}"
            f"/messages/{_ec(msg_or_id.id)}",
            not_found_class=MessageNotFound,
        )

    # Alias -- the spec exposes ``delete`` as a synonym for ``ack``.
    def delete(self, msg_or_id: ChannelMessage | str) -> None:
        """Alias for :meth:`ack` -- deletes the message on the server."""
        self.ack(msg_or_id)

    # ------------------------------------------------------------------
    # wait -- client-side polling helper
    # ------------------------------------------------------------------

    def wait(
        self,
        channel: str,
        *,
        consumer: str | None = None,
        timeout: float = 30.0,
        poll_interval: float = 0.5,
    ) -> ChannelMessage | None:
        """Poll :meth:`receive` until a message arrives or ``timeout`` elapses.

        Args:
            channel: The channel to poll.
            consumer: Optional consumer name (server-tracked cursor).
            timeout: Total wall-clock seconds to wait.
            poll_interval: Seconds to sleep between polls.

        Returns:
            The first :class:`ChannelMessage` received, or ``None`` on
            timeout.
        """
        deadline = time.monotonic() + max(0.0, timeout)
        while True:
            msgs = self.receive(channel, consumer=consumer, limit=1)
            if msgs:
                return msgs[0]
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return None
            time.sleep(min(poll_interval, remaining))

    # ------------------------------------------------------------------
    # channel management
    # ------------------------------------------------------------------

    def list(self) -> list[Channel]:
        """List every channel on the workspace as :class:`Channel` rows."""
        data = self._ws._http.get_json(
            f"/v1/workspaces/{self._ws.id}/channels",
            not_found_class=WorkspaceNotFound,
        )
        return [Channel.model_validate(c) for c in data.get("channels", [])]

    def get(self, channel: str) -> Channel:
        """Fetch a single :class:`Channel` by name (404 -> ChannelNotFound)."""
        data = self._ws._http.get_json(
            f"/v1/workspaces/{self._ws.id}/channels/{_ec(channel)}",
            not_found_class=ChannelNotFound,
        )
        return Channel.model_validate(data)

    def delete_channel(self, channel: str) -> None:
        """Delete a channel and all of its messages."""
        self._ws._http.delete(
            f"/v1/workspaces/{self._ws.id}/channels/{_ec(channel)}",
            not_found_class=ChannelNotFound,
        )

    # ------------------------------------------------------------------
    # v0.5 — typed channels: schema CRUD
    # ------------------------------------------------------------------

    def set_schema(self, channel: str, schema: dict[str, Any]) -> ChannelSchema:
        """Attach a JSON Schema to ``channel``.

        Each call increments the channel's schema version. Subsequent
        sends are validated against the schema; failures land on the
        channel's dead-letter queue and raise
        :class:`SchemaViolation`.

        Args:
            channel: The channel name. The channel does not need to
                exist yet — it will be created lazily on first send.
            schema: A JSON Schema document (Draft 2020-12 compatible).

        Returns:
            The persisted :class:`ChannelSchema` with its bumped version.
        """

        response = self._ws._http.post(
            f"/v1/workspaces/{self._ws.id}/channels/{_ec(channel)}/schema",
            json={"schema": schema},
            not_found_class=WorkspaceNotFound,
        )
        return ChannelSchema.model_validate(response.json())

    def get_schema(self, channel: str) -> ChannelSchema | None:
        """Return the schema attached to ``channel``, or ``None`` if unset.

        404 responses are translated to ``None`` here because "no schema
        attached" is a perfectly normal state — the alternative would
        force every call site into a try/except that doesn't add signal.
        """

        try:
            data = self._ws._http.get_json(
                f"/v1/workspaces/{self._ws.id}/channels/{_ec(channel)}/schema",
                not_found_class=ChannelNotFound,
            )
        except ChannelNotFound:
            # Server returned 404 with the SCHEMA_NOT_FOUND code; we treat
            # both shapes ("no channel", "no schema on this channel") as
            # "no schema is attached". Callers who care about workspace
            # validity get a separate WorkspaceNotFound up front.
            return None
        return ChannelSchema.model_validate(data)

    def delete_schema(self, channel: str) -> None:
        """Detach the schema from ``channel``.

        Idempotent: deleting an already-unset schema is a no-op.
        """

        self._ws._http.delete(
            f"/v1/workspaces/{self._ws.id}/channels/{_ec(channel)}/schema",
            not_found_class=WorkspaceNotFound,
        )

    # ------------------------------------------------------------------
    # v0.5 — dead-letter queue
    # ------------------------------------------------------------------

    def deadletter(
        self,
        channel: str,
        *,
        limit: int = 100,
        since: int | None = None,
    ) -> list[ChannelMessage]:
        """List dead-lettered messages for ``channel``.

        Messages land here when their payload fails JSON Schema
        validation at send time. They carry diagnostic ``x-`` headers
        (``x-original-channel``, ``x-validation-errors``, ``x-failed-at``,
        ``x-schema-version``) so consumers can decide whether to replay,
        rewrite, or drop.
        """

        params: dict[str, Any] = {"limit": limit}
        if since is not None:
            params["since"] = since
        data = self._ws._http.get_json(
            f"/v1/workspaces/{self._ws.id}/channels/{_ec(channel)}/deadletter",
            params=params,
            not_found_class=WorkspaceNotFound,
        )
        return [ChannelMessage.model_validate(m) for m in data.get("messages", [])]

    def replay(
        self,
        channel: str,
        msg_or_id: ChannelMessage | str,
    ) -> ChannelMessage:
        """Re-validate + re-send a DLQ message to its main channel.

        On success the original DLQ row is removed and the freshly-sent
        :class:`ChannelMessage` (with a new ``id`` and ``seq``) is
        returned. If the schema has not been relaxed and validation fails
        again, :class:`SchemaViolation` is raised and the DLQ row stays
        put.
        """

        msg_id = msg_or_id.id if isinstance(msg_or_id, ChannelMessage) else msg_or_id
        response = self._ws._http.post(
            f"/v1/workspaces/{self._ws.id}/channels/{_ec(channel)}/deadletter/{_ec(msg_id)}/replay",
            not_found_class=MessageNotFound,
        )
        return ChannelMessage.model_validate(response.json())

    def drop_deadletter(
        self,
        channel: str,
        msg_or_id: ChannelMessage | str,
    ) -> None:
        """Delete a DLQ message without replay (give-up path)."""

        msg_id = msg_or_id.id if isinstance(msg_or_id, ChannelMessage) else msg_or_id
        self._ws._http.delete(
            f"/v1/workspaces/{self._ws.id}/channels/{_ec(channel)}/deadletter/{_ec(msg_id)}",
            not_found_class=MessageNotFound,
        )

    # ------------------------------------------------------------------
    # v0.6 — channel schema migration helpers
    # ------------------------------------------------------------------

    def check_schema(
        self,
        channel: str,
        schema: dict[str, Any],
        *,
        scope: str = "both",
        limit: int = 1000,
    ) -> SchemaCheckResult:
        """Preview compatibility of a candidate schema against existing rows.

        Validates up to ``limit`` messages (server hard cap: 10 000) drawn
        from the main channel, the DLQ, or both (``scope``). The candidate
        is not persisted — pair this with :meth:`set_schema` once you're
        happy with the report.

        Returns counts + the first 10 failure samples in canonical
        ``{msg_id, errors}`` shape so callers can render diagnostics
        without case-by-case parsing.
        """

        body = {"schema": schema, "scope": scope, "limit": limit}
        response = self._ws._http.post(
            f"/v1/workspaces/{self._ws.id}/channels/{_ec(channel)}/schema/check",
            json=body,
            not_found_class=WorkspaceNotFound,
        )
        return SchemaCheckResult.model_validate(response.json())

    def replay_all_dlq(
        self,
        channel: str,
        *,
        max: int = 1000,  # noqa: A002 - mirrors API param name
        dry_run: bool = False,
    ) -> ReplayBatchResult:
        """Bulk-replay DLQ messages back through the current schema.

        Iterates the DLQ in seq order (up to ``max``, server hard cap
        10 000). Each message is re-validated against the *currently
        attached* schema; successes move to the main channel, failures
        stay in the DLQ. With ``dry_run=True`` no rows are mutated — the
        result still reflects what would happen.

        ``failures`` is bounded server-side to 50 entries (each
        ``{msg_id, reason}``); the totals in ``attempted`` / ``succeeded``
        / ``failed`` are accurate even when the list is truncated.
        """

        params: dict[str, Any] = {"max": max}
        if dry_run:
            params["dry_run"] = "true"
        response = self._ws._http.post(
            f"/v1/workspaces/{self._ws.id}/channels/{_ec(channel)}/deadletter/replay-all",
            params=params,
            not_found_class=WorkspaceNotFound,
        )
        return ReplayBatchResult.model_validate(response.json())

    def purge_dlq(
        self,
        channel: str,
        *,
        older_than_seconds: int = 0,
    ) -> int:
        """Delete DLQ rows older than ``older_than_seconds``; return count.

        ``older_than_seconds=0`` clears the entire DLQ (useful after a
        big schema relax). Channels that never had a DLQ return ``0``
        rather than raising — same idempotency principle as
        :meth:`delete_schema`.
        """

        response = self._ws._http.delete(
            f"/v1/workspaces/{self._ws.id}/channels/{_ec(channel)}/deadletter",
            params={"older_than_seconds": older_than_seconds},
            not_found_class=WorkspaceNotFound,
        )
        body = response.json()
        return int(body.get("purged", 0))


__all__ = ["ChannelsProxy"]
