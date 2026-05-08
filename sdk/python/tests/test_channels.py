# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 The Plinth Authors
"""Tests for ``plinth.channels`` -- the v0.2 channels SDK surface."""

from __future__ import annotations

import json
import threading
import time

import httpx
import pytest
import respx

from plinth import (
    Channel,
    ChannelMessage,
    ChannelNotFound,
    MessageNotFound,
    Plinth,
    Workspace,
)

from .conftest import (
    error_envelope,
    make_channel,
    make_channel_message,
    make_workspace,
)

# ---------------------------------------------------------------------------
# Helper -- a ready-to-use workspace handle.
# ---------------------------------------------------------------------------


@pytest.fixture
def ws(client: Plinth, workspace_mock: respx.MockRouter) -> Workspace:
    """Return a Workspace bound to ws_01TESTWORKSPACE."""
    workspace_mock.get("/v1/workspaces").mock(
        return_value=httpx.Response(200, json={"workspaces": [make_workspace()]})
    )
    return client.workspace("research-task-1")


# ---------------------------------------------------------------------------
# send
# ---------------------------------------------------------------------------


def test_send_returns_channel_message(ws: Workspace, workspace_mock: respx.MockRouter):
    payload = {"sources": ["a", "b"], "facts": {"x": 1}}
    msg = make_channel_message(
        msg_id="msg_01ABC",
        channel="research-out",
        seq=1,
        payload=payload,
        sender="researcher",
        type="research.complete",
    )
    route = workspace_mock.post(
        f"/v1/workspaces/{ws.id}/channels/research-out/send"
    ).mock(return_value=httpx.Response(201, json=msg))

    out = ws.channels.send(
        "research-out",
        payload,
        sender="researcher",
        type="research.complete",
    )

    assert isinstance(out, ChannelMessage)
    assert out.id == "msg_01ABC"
    assert out.seq == 1
    assert out.sent_at is not None
    assert out.payload == payload
    assert out.sender == "researcher"
    assert out.type == "research.complete"

    # Body shape -- only the populated optional fields go on the wire.
    body = json.loads(route.calls.last.request.read())
    assert body["payload"] == payload
    assert body["sender"] == "researcher"
    assert body["type"] == "research.complete"
    assert "correlation_id" not in body
    assert "headers" not in body


def test_send_includes_optional_fields_when_provided(
    ws: Workspace, workspace_mock: respx.MockRouter
):
    msg = make_channel_message(channel="hand-off", correlation_id="corr_1")
    route = workspace_mock.post(
        f"/v1/workspaces/{ws.id}/channels/hand-off/send"
    ).mock(return_value=httpx.Response(201, json=msg))

    ws.channels.send(
        "hand-off",
        {"data": 1},
        correlation_id="corr_1",
        headers={"k": "v"},
    )

    body = json.loads(route.calls.last.request.read())
    assert body["correlation_id"] == "corr_1"
    assert body["headers"] == {"k": "v"}


def test_send_url_encodes_channel_name(ws: Workspace, workspace_mock: respx.MockRouter):
    # "with spaces" must end up percent-encoded in the URL.
    encoded = "with%20spaces"
    msg = make_channel_message(channel="with spaces")
    route = workspace_mock.post(
        f"/v1/workspaces/{ws.id}/channels/{encoded}/send"
    ).mock(return_value=httpx.Response(201, json=msg))

    ws.channels.send("with spaces", {"x": 1})

    assert route.called


# ---------------------------------------------------------------------------
# receive
# ---------------------------------------------------------------------------


def test_receive_returns_messages_with_payload_roundtrip(
    ws: Workspace, workspace_mock: respx.MockRouter
):
    payloads = [{"i": 0}, {"i": 1, "nested": {"k": "v"}}, {"i": 2}]
    body = {
        "messages": [
            make_channel_message(msg_id=f"msg_{i}", seq=i + 1, payload=p)
            for i, p in enumerate(payloads)
        ]
    }
    workspace_mock.get(
        f"/v1/workspaces/{ws.id}/channels/research-out/receive"
    ).mock(return_value=httpx.Response(200, json=body))

    msgs = ws.channels.receive("research-out")

    assert [m.payload for m in msgs] == payloads
    assert [m.seq for m in msgs] == [1, 2, 3]


def test_receive_passes_consumer_since_limit_peek(
    ws: Workspace, workspace_mock: respx.MockRouter
):
    captured: dict = {}

    def handler(request):
        captured["params"] = dict(request.url.params)
        return httpx.Response(200, json={"messages": []})

    workspace_mock.get(
        f"/v1/workspaces/{ws.id}/channels/research-out/receive"
    ).mock(side_effect=handler)

    ws.channels.receive(
        "research-out",
        consumer="writer",
        since=42,
        limit=10,
        peek=True,
    )

    params = captured["params"]
    assert params["consumer"] == "writer"
    assert params["since"] == "42"
    assert params["limit"] == "10"
    assert params["peek"] == "true"


def test_receive_default_omits_optional_params(
    ws: Workspace, workspace_mock: respx.MockRouter
):
    captured: dict = {}

    def handler(request):
        captured["params"] = dict(request.url.params)
        return httpx.Response(200, json={"messages": []})

    workspace_mock.get(
        f"/v1/workspaces/{ws.id}/channels/research-out/receive"
    ).mock(side_effect=handler)

    ws.channels.receive("research-out")

    # ``limit`` always present (default 100). consumer/since/peek omitted.
    assert captured["params"] == {"limit": "100"}


def test_receive_peek_does_not_advance_cursor(
    ws: Workspace, workspace_mock: respx.MockRouter
):
    captured = []

    def handler(request):
        captured.append(dict(request.url.params))
        return httpx.Response(
            200,
            json={"messages": [make_channel_message(msg_id="msg_1", seq=1)]},
        )

    workspace_mock.get(
        f"/v1/workspaces/{ws.id}/channels/research-out/receive"
    ).mock(side_effect=handler)

    first = ws.channels.receive("research-out", consumer="writer", peek=True)
    second = ws.channels.receive("research-out", consumer="writer", peek=True)

    # Peek=true should produce identical results because cursor hasn't moved.
    assert first[0].id == second[0].id == "msg_1"
    assert all(c["peek"] == "true" for c in captured)


def test_receive_empty_channel_returns_empty_list(
    ws: Workspace, workspace_mock: respx.MockRouter
):
    workspace_mock.get(
        f"/v1/workspaces/{ws.id}/channels/research-out/receive"
    ).mock(return_value=httpx.Response(200, json={"messages": []}))

    assert ws.channels.receive("research-out") == []


def test_receive_404_raises_channel_not_found(
    ws: Workspace, workspace_mock: respx.MockRouter
):
    workspace_mock.get(
        f"/v1/workspaces/{ws.id}/channels/nope/receive"
    ).mock(
        return_value=httpx.Response(
            404, json=error_envelope("CHANNEL_NOT_FOUND", "no such channel")
        )
    )

    with pytest.raises(ChannelNotFound):
        ws.channels.receive("nope")


# ---------------------------------------------------------------------------
# ack / delete
# ---------------------------------------------------------------------------


def test_ack_deletes_via_message_object(
    ws: Workspace, workspace_mock: respx.MockRouter
):
    workspace_mock.get(
        f"/v1/workspaces/{ws.id}/channels/research-out/receive"
    ).mock(
        return_value=httpx.Response(
            200, json={"messages": [make_channel_message(msg_id="msg_X", seq=1)]}
        )
    )
    delete_route = workspace_mock.delete(
        f"/v1/workspaces/{ws.id}/channels/research-out/messages/msg_X"
    ).mock(return_value=httpx.Response(204))

    msgs = ws.channels.receive("research-out")
    ws.channels.ack(msgs[0])

    assert delete_route.called


def test_ack_with_string_id_raises_value_error(ws: Workspace):
    # The DELETE URL needs the channel name; a bare ID is insufficient.
    with pytest.raises(ValueError, match="ChannelMessage"):
        ws.channels.ack("msg_X")


def test_delete_is_alias_for_ack(
    ws: Workspace, workspace_mock: respx.MockRouter
):
    msg = ChannelMessage.model_validate(make_channel_message(msg_id="msg_Z", seq=1))
    delete_route = workspace_mock.delete(
        f"/v1/workspaces/{ws.id}/channels/research-out/messages/msg_Z"
    ).mock(return_value=httpx.Response(204))

    ws.channels.delete(msg)

    assert delete_route.called


def test_ack_404_raises_message_not_found(
    ws: Workspace, workspace_mock: respx.MockRouter
):
    msg = ChannelMessage.model_validate(make_channel_message(msg_id="msg_X"))
    workspace_mock.delete(
        f"/v1/workspaces/{ws.id}/channels/research-out/messages/msg_X"
    ).mock(
        return_value=httpx.Response(
            404, json=error_envelope("MESSAGE_NOT_FOUND", "gone")
        )
    )

    with pytest.raises(MessageNotFound):
        ws.channels.ack(msg)


# ---------------------------------------------------------------------------
# wait -- polling helper
# ---------------------------------------------------------------------------


def test_wait_returns_none_on_timeout(
    ws: Workspace, workspace_mock: respx.MockRouter
):
    workspace_mock.get(
        f"/v1/workspaces/{ws.id}/channels/research-out/receive"
    ).mock(return_value=httpx.Response(200, json={"messages": []}))

    start = time.monotonic()
    result = ws.channels.wait(
        "research-out",
        consumer="writer",
        timeout=0.2,
        poll_interval=0.05,
    )
    elapsed = time.monotonic() - start

    assert result is None
    # Sanity check we actually polled rather than returning instantly.
    assert elapsed >= 0.15


def test_wait_returns_message_when_one_arrives_mid_poll(
    ws: Workspace, workspace_mock: respx.MockRouter
):
    state = {"calls": 0}

    def handler(request):
        state["calls"] += 1
        if state["calls"] < 3:
            return httpx.Response(200, json={"messages": []})
        return httpx.Response(
            200,
            json={"messages": [make_channel_message(msg_id="msg_late", seq=7)]},
        )

    workspace_mock.get(
        f"/v1/workspaces/{ws.id}/channels/research-out/receive"
    ).mock(side_effect=handler)

    msg = ws.channels.wait(
        "research-out",
        consumer="writer",
        timeout=2.0,
        poll_interval=0.01,
    )

    assert msg is not None
    assert msg.id == "msg_late"
    assert msg.seq == 7


def test_wait_returns_immediately_when_message_present(
    ws: Workspace, workspace_mock: respx.MockRouter
):
    workspace_mock.get(
        f"/v1/workspaces/{ws.id}/channels/research-out/receive"
    ).mock(
        return_value=httpx.Response(
            200, json={"messages": [make_channel_message(msg_id="msg_now", seq=1)]}
        )
    )

    start = time.monotonic()
    msg = ws.channels.wait("research-out", timeout=5.0, poll_interval=0.5)
    elapsed = time.monotonic() - start

    assert msg is not None
    assert msg.id == "msg_now"
    # Should not have slept its way through the full poll interval.
    assert elapsed < 0.5


def test_wait_with_concurrent_arrival(
    ws: Workspace, workspace_mock: respx.MockRouter
):
    """A message arriving from another thread is still picked up.

    Exercises the "real-world" pattern where another agent ``send``s
    while the consumer is in ``wait``.
    """
    state = {"queued": False}

    def handler(request):
        if state["queued"]:
            return httpx.Response(
                200,
                json={"messages": [make_channel_message(msg_id="msg_thr", seq=1)]},
            )
        return httpx.Response(200, json={"messages": []})

    workspace_mock.get(
        f"/v1/workspaces/{ws.id}/channels/research-out/receive"
    ).mock(side_effect=handler)

    def producer() -> None:
        time.sleep(0.05)
        state["queued"] = True

    threading.Thread(target=producer, daemon=True).start()

    msg = ws.channels.wait(
        "research-out",
        consumer="writer",
        timeout=2.0,
        poll_interval=0.02,
    )

    assert msg is not None
    assert msg.id == "msg_thr"


# ---------------------------------------------------------------------------
# list / get / delete_channel
# ---------------------------------------------------------------------------


def test_list_returns_channels(ws: Workspace, workspace_mock: respx.MockRouter):
    body = {
        "channels": [
            make_channel(name="research-out", message_count=3),
            make_channel(name="reviews", message_count=0),
        ]
    }
    workspace_mock.get(f"/v1/workspaces/{ws.id}/channels").mock(
        return_value=httpx.Response(200, json=body)
    )

    channels = ws.channels.list()

    assert all(isinstance(c, Channel) for c in channels)
    assert {c.name for c in channels} == {"research-out", "reviews"}
    assert next(c for c in channels if c.name == "research-out").message_count == 3


def test_get_returns_channel(ws: Workspace, workspace_mock: respx.MockRouter):
    workspace_mock.get(f"/v1/workspaces/{ws.id}/channels/research-out").mock(
        return_value=httpx.Response(200, json=make_channel(message_count=5))
    )

    channel = ws.channels.get("research-out")

    assert isinstance(channel, Channel)
    assert channel.name == "research-out"
    assert channel.message_count == 5


def test_get_404_raises_channel_not_found(
    ws: Workspace, workspace_mock: respx.MockRouter
):
    workspace_mock.get(f"/v1/workspaces/{ws.id}/channels/nope").mock(
        return_value=httpx.Response(
            404, json=error_envelope("CHANNEL_NOT_FOUND", "nope")
        )
    )

    with pytest.raises(ChannelNotFound):
        ws.channels.get("nope")


def test_delete_channel(ws: Workspace, workspace_mock: respx.MockRouter):
    route = workspace_mock.delete(
        f"/v1/workspaces/{ws.id}/channels/research-out"
    ).mock(return_value=httpx.Response(204))

    ws.channels.delete_channel("research-out")

    assert route.called


def test_delete_channel_404_raises_channel_not_found(
    ws: Workspace, workspace_mock: respx.MockRouter
):
    workspace_mock.delete(f"/v1/workspaces/{ws.id}/channels/nope").mock(
        return_value=httpx.Response(
            404, json=error_envelope("CHANNEL_NOT_FOUND", "no such channel")
        )
    )

    with pytest.raises(ChannelNotFound):
        ws.channels.delete_channel("nope")


# ---------------------------------------------------------------------------
# Cursor consumer -- end-to-end roundtrip exercising send/receive/ack.
# ---------------------------------------------------------------------------


def test_consumer_cursor_roundtrip(
    ws: Workspace, workspace_mock: respx.MockRouter
):
    """A 2-step pipeline -- send, then a named consumer receive + ack."""
    sent_msg = make_channel_message(
        msg_id="msg_pipe", channel="pipe", seq=1, payload={"x": 1}
    )

    workspace_mock.post(
        f"/v1/workspaces/{ws.id}/channels/pipe/send"
    ).mock(return_value=httpx.Response(201, json=sent_msg))
    workspace_mock.get(
        f"/v1/workspaces/{ws.id}/channels/pipe/receive"
    ).mock(return_value=httpx.Response(200, json={"messages": [sent_msg]}))
    delete_route = workspace_mock.delete(
        f"/v1/workspaces/{ws.id}/channels/pipe/messages/msg_pipe"
    ).mock(return_value=httpx.Response(204))

    msg = ws.channels.send("pipe", {"x": 1})
    assert msg.id == "msg_pipe"

    received = ws.channels.receive("pipe", consumer="writer")
    assert received[0].id == "msg_pipe"

    ws.channels.ack(received[0])
    assert delete_route.called


# ---------------------------------------------------------------------------
# v0.5 — typed channels: schema CRUD + DLQ surface
# ---------------------------------------------------------------------------


from plinth import ChannelSchema, SchemaViolation  # noqa: E402


def _schema_doc() -> dict:
    return {
        "type": "object",
        "required": ["topic", "sources"],
        "properties": {
            "topic": {"type": "string"},
            "sources": {"type": "array", "items": {"type": "string"}},
        },
    }


def _make_channel_schema(
    *,
    workspace_id: str = "ws_01TESTWORKSPACE",
    channel_name: str = "research-out",
    schema_json: dict | None = None,
    version: int = 1,
) -> dict:
    return {
        "workspace_id": workspace_id,
        "channel_name": channel_name,
        "schema_json": schema_json or _schema_doc(),
        "version": version,
        "updated_at": "2026-05-07T16:30:00Z",
    }


def test_set_schema_returns_channel_schema(
    ws: Workspace, workspace_mock: respx.MockRouter
):
    """``set_schema`` POSTs a ``{schema}`` body and returns a ChannelSchema."""
    persisted = _make_channel_schema(channel_name="research-out", version=1)
    route = workspace_mock.post(
        f"/v1/workspaces/{ws.id}/channels/research-out/schema"
    ).mock(return_value=httpx.Response(200, json=persisted))

    out = ws.channels.set_schema("research-out", _schema_doc())

    assert isinstance(out, ChannelSchema)
    assert out.channel_name == "research-out"
    assert out.version == 1
    assert out.schema_json == _schema_doc()

    body = json.loads(route.calls.last.request.read())
    assert body == {"schema": _schema_doc()}


def test_get_schema_returns_channel_schema(
    ws: Workspace, workspace_mock: respx.MockRouter
):
    workspace_mock.get(
        f"/v1/workspaces/{ws.id}/channels/research-out/schema"
    ).mock(return_value=httpx.Response(200, json=_make_channel_schema(version=2)))

    out = ws.channels.get_schema("research-out")

    assert isinstance(out, ChannelSchema)
    assert out.version == 2


def test_get_schema_404_returns_none(
    ws: Workspace, workspace_mock: respx.MockRouter
):
    """Missing schema → ``None``, not a raised exception."""
    workspace_mock.get(
        f"/v1/workspaces/{ws.id}/channels/no-schema/schema"
    ).mock(
        return_value=httpx.Response(
            404, json=error_envelope("SCHEMA_NOT_FOUND", "no schema")
        )
    )

    assert ws.channels.get_schema("no-schema") is None


def test_delete_schema(ws: Workspace, workspace_mock: respx.MockRouter):
    route = workspace_mock.delete(
        f"/v1/workspaces/{ws.id}/channels/research-out/schema"
    ).mock(return_value=httpx.Response(204))

    ws.channels.delete_schema("research-out")

    assert route.called


def test_send_invalid_payload_raises_schema_violation(
    ws: Workspace, workspace_mock: respx.MockRouter
):
    """422 SCHEMA_VIOLATION → ``SchemaViolation`` exposing errors + DLQ id."""
    envelope = {
        "error": {
            "code": "SCHEMA_VIOLATION",
            "message": "Payload does not match channel schema",
            "details": {
                "channel": "research-out",
                "errors": [
                    {"message": "'sources' is a required property", "path": []}
                ],
                "deadletter_msg_id": "msg_dlq_01",
            },
        }
    }
    workspace_mock.post(
        f"/v1/workspaces/{ws.id}/channels/research-out/send"
    ).mock(return_value=httpx.Response(422, json=envelope))

    with pytest.raises(SchemaViolation) as exc_info:
        ws.channels.send("research-out", {"topic": "ai"})

    err = exc_info.value
    assert err.code == "SCHEMA_VIOLATION"
    assert err.deadletter_msg_id == "msg_dlq_01"
    assert err.channel == "research-out"
    assert err.errors and err.errors[0]["message"]


def test_deadletter_lists_messages(
    ws: Workspace, workspace_mock: respx.MockRouter
):
    msgs = [
        make_channel_message(
            msg_id="msg_d1",
            channel="research-out.deadletter",
            seq=1,
            payload={"topic": "x"},
            headers={"x-original-channel": "research-out"},
        ),
        make_channel_message(
            msg_id="msg_d2",
            channel="research-out.deadletter",
            seq=2,
            payload={"topic": "y"},
            headers={"x-original-channel": "research-out"},
        ),
    ]
    route = workspace_mock.get(
        f"/v1/workspaces/{ws.id}/channels/research-out/deadletter"
    ).mock(return_value=httpx.Response(200, json={"messages": msgs}))

    out = ws.channels.deadletter("research-out", limit=50)

    assert len(out) == 2
    assert all(isinstance(m, ChannelMessage) for m in out)
    assert out[0].id == "msg_d1"
    assert route.calls.last.request.url.params["limit"] == "50"


def test_deadletter_passes_since(ws: Workspace, workspace_mock: respx.MockRouter):
    route = workspace_mock.get(
        f"/v1/workspaces/{ws.id}/channels/research-out/deadletter"
    ).mock(return_value=httpx.Response(200, json={"messages": []}))

    ws.channels.deadletter("research-out", since=10, limit=5)
    params = route.calls.last.request.url.params
    assert params["since"] == "10"
    assert params["limit"] == "5"


def test_replay_returns_new_message(
    ws: Workspace, workspace_mock: respx.MockRouter
):
    """A successful replay returns the freshly-sent main-channel message."""
    new_msg = make_channel_message(
        msg_id="msg_new", channel="research-out", seq=1, payload={"topic": "x"}
    )
    route = workspace_mock.post(
        f"/v1/workspaces/{ws.id}/channels/research-out/deadletter/msg_d1/replay"
    ).mock(return_value=httpx.Response(200, json=new_msg))

    out = ws.channels.replay("research-out", "msg_d1")

    assert isinstance(out, ChannelMessage)
    assert out.id == "msg_new"
    assert route.called


def test_replay_accepts_message_object(
    ws: Workspace, workspace_mock: respx.MockRouter
):
    """``replay`` reads the ID off a passed-in ChannelMessage."""
    dlq_msg = ChannelMessage.model_validate(
        make_channel_message(
            msg_id="msg_d1",
            channel="research-out.deadletter",
            seq=1,
            payload={"topic": "x"},
        )
    )
    new_msg = make_channel_message(
        msg_id="msg_new", channel="research-out", seq=1, payload={"topic": "x"}
    )
    workspace_mock.post(
        f"/v1/workspaces/{ws.id}/channels/research-out/deadletter/msg_d1/replay"
    ).mock(return_value=httpx.Response(200, json=new_msg))

    out = ws.channels.replay("research-out", dlq_msg)
    assert out.id == "msg_new"


def test_replay_still_invalid_raises_schema_violation(
    ws: Workspace, workspace_mock: respx.MockRouter
):
    envelope = {
        "error": {
            "code": "SCHEMA_VIOLATION",
            "message": "still invalid",
            "details": {
                "channel": "research-out",
                "errors": [{"message": "boom", "path": []}],
                "deadletter_msg_id": "msg_d1",
            },
        }
    }
    workspace_mock.post(
        f"/v1/workspaces/{ws.id}/channels/research-out/deadletter/msg_d1/replay"
    ).mock(return_value=httpx.Response(422, json=envelope))

    with pytest.raises(SchemaViolation) as exc_info:
        ws.channels.replay("research-out", "msg_d1")
    assert exc_info.value.deadletter_msg_id == "msg_d1"


def test_drop_deadletter_via_id(ws: Workspace, workspace_mock: respx.MockRouter):
    route = workspace_mock.delete(
        f"/v1/workspaces/{ws.id}/channels/research-out/deadletter/msg_d1"
    ).mock(return_value=httpx.Response(204))

    ws.channels.drop_deadletter("research-out", "msg_d1")
    assert route.called


def test_drop_deadletter_via_message_object(
    ws: Workspace, workspace_mock: respx.MockRouter
):
    dlq_msg = ChannelMessage.model_validate(
        make_channel_message(
            msg_id="msg_d1",
            channel="research-out.deadletter",
            seq=1,
            payload={"topic": "x"},
        )
    )
    route = workspace_mock.delete(
        f"/v1/workspaces/{ws.id}/channels/research-out/deadletter/msg_d1"
    ).mock(return_value=httpx.Response(204))

    ws.channels.drop_deadletter("research-out", dlq_msg)
    assert route.called


def test_drop_deadletter_404_raises_message_not_found(
    ws: Workspace, workspace_mock: respx.MockRouter
):
    workspace_mock.delete(
        f"/v1/workspaces/{ws.id}/channels/research-out/deadletter/msg_unknown"
    ).mock(
        return_value=httpx.Response(
            404, json=error_envelope("MESSAGE_NOT_FOUND", "no such msg")
        )
    )

    with pytest.raises(MessageNotFound):
        ws.channels.drop_deadletter("research-out", "msg_unknown")


def test_schema_url_encodes_channel_name(
    ws: Workspace, workspace_mock: respx.MockRouter
):
    """Channel names with characters that need percent-encoding round-trip."""
    workspace_mock.post(
        f"/v1/workspaces/{ws.id}/channels/with%20space/schema"
    ).mock(return_value=httpx.Response(200, json=_make_channel_schema(channel_name="with space")))
    out = ws.channels.set_schema("with space", _schema_doc())
    assert out.channel_name == "with space"


# ---------------------------------------------------------------------------
# v0.6 — schema migration helpers (check / replay-all / purge)
# ---------------------------------------------------------------------------


from plinth import ReplayBatchResult, SchemaCheckResult  # noqa: E402


def test_check_schema_returns_typed_result(
    ws: Workspace, workspace_mock: respx.MockRouter
):
    """``check_schema`` POSTs schema/scope/limit and returns a typed result."""
    body = {
        "channel": "research-out",
        "scope": "both",
        "checked": 7,
        "valid": 5,
        "invalid": 2,
        "sample_failures": [
            {"msg_id": "msg_d1", "errors": [{"path": [], "message": "bad"}]},
        ],
    }
    route = workspace_mock.post(
        f"/v1/workspaces/{ws.id}/channels/research-out/schema/check"
    ).mock(return_value=httpx.Response(200, json=body))

    result = ws.channels.check_schema(
        "research-out", _schema_doc(), scope="both", limit=500
    )

    assert isinstance(result, SchemaCheckResult)
    assert result.checked == 7
    assert result.valid == 5
    assert result.invalid == 2
    assert result.sample_failures[0]["msg_id"] == "msg_d1"

    sent = json.loads(route.calls.last.request.read())
    assert sent == {"schema": _schema_doc(), "scope": "both", "limit": 500}


def test_check_schema_default_scope_and_limit(
    ws: Workspace, workspace_mock: respx.MockRouter
):
    """Defaults: ``scope='both'``, ``limit=1000``."""
    body = {
        "channel": "out",
        "scope": "both",
        "checked": 0,
        "valid": 0,
        "invalid": 0,
        "sample_failures": [],
    }
    route = workspace_mock.post(
        f"/v1/workspaces/{ws.id}/channels/out/schema/check"
    ).mock(return_value=httpx.Response(200, json=body))

    ws.channels.check_schema("out", {"type": "object"})

    sent = json.loads(route.calls.last.request.read())
    assert sent["scope"] == "both"
    assert sent["limit"] == 1000


def test_replay_all_dlq_dry_run_flag(
    ws: Workspace, workspace_mock: respx.MockRouter
):
    """``dry_run=True`` lands as a query param ``dry_run=true``."""
    body = {
        "channel": "out",
        "attempted": 3,
        "succeeded": 3,
        "failed": 0,
        "failures": [],
        "dry_run": True,
    }
    route = workspace_mock.post(
        f"/v1/workspaces/{ws.id}/channels/out/deadletter/replay-all"
    ).mock(return_value=httpx.Response(200, json=body))

    result = ws.channels.replay_all_dlq("out", dry_run=True, max=50)

    assert isinstance(result, ReplayBatchResult)
    assert result.dry_run is True
    assert result.attempted == 3
    assert result.succeeded == 3

    params = route.calls.last.request.url.params
    assert params["dry_run"] == "true"
    assert params["max"] == "50"


def test_replay_all_dlq_actual_run(
    ws: Workspace, workspace_mock: respx.MockRouter
):
    """No ``dry_run`` query param when ``dry_run=False``."""
    body = {
        "channel": "out",
        "attempted": 5,
        "succeeded": 4,
        "failed": 1,
        "failures": [{"msg_id": "msg_x", "reason": "still bad"}],
        "dry_run": False,
    }
    route = workspace_mock.post(
        f"/v1/workspaces/{ws.id}/channels/out/deadletter/replay-all"
    ).mock(return_value=httpx.Response(200, json=body))

    result = ws.channels.replay_all_dlq("out")

    assert result.failed == 1
    assert result.failures[0]["reason"] == "still bad"

    params = route.calls.last.request.url.params
    # ``dry_run`` defaults to false → not sent.
    assert "dry_run" not in params
    assert params["max"] == "1000"


def test_purge_dlq_returns_count(
    ws: Workspace, workspace_mock: respx.MockRouter
):
    """``purge_dlq`` returns the integer count from the response body."""
    route = workspace_mock.delete(
        f"/v1/workspaces/{ws.id}/channels/out/deadletter"
    ).mock(return_value=httpx.Response(200, json={"purged": 3}))

    count = ws.channels.purge_dlq("out", older_than_seconds=0)

    assert count == 3
    params = route.calls.last.request.url.params
    assert params["older_than_seconds"] == "0"


def test_purge_dlq_passes_older_than(
    ws: Workspace, workspace_mock: respx.MockRouter
):
    """``older_than_seconds`` is forwarded as a query param."""
    workspace_mock.delete(
        f"/v1/workspaces/{ws.id}/channels/out/deadletter",
        params={"older_than_seconds": "86400"},
    ).mock(return_value=httpx.Response(200, json={"purged": 0}))

    count = ws.channels.purge_dlq("out", older_than_seconds=86400)
    assert count == 0


def test_check_schema_workspace_not_found_raises(
    ws: Workspace, workspace_mock: respx.MockRouter
):
    """A 404 with WORKSPACE_NOT_FOUND surfaces as a typed exception."""
    from plinth import WorkspaceNotFound

    workspace_mock.post(
        f"/v1/workspaces/{ws.id}/channels/out/schema/check"
    ).mock(
        return_value=httpx.Response(
            404,
            json=error_envelope("WORKSPACE_NOT_FOUND", "no such ws"),
        )
    )

    with pytest.raises(WorkspaceNotFound):
        ws.channels.check_schema("out", {"type": "object"})


def test_replay_all_url_encodes_channel_name(
    ws: Workspace, workspace_mock: respx.MockRouter
):
    """Channel names round-trip through the URL encoder."""
    workspace_mock.post(
        f"/v1/workspaces/{ws.id}/channels/with%20space/deadletter/replay-all"
    ).mock(
        return_value=httpx.Response(
            200,
            json={
                "channel": "with space",
                "attempted": 0,
                "succeeded": 0,
                "failed": 0,
                "failures": [],
                "dry_run": False,
            },
        )
    )

    out = ws.channels.replay_all_dlq("with space")
    assert out.channel == "with space"
