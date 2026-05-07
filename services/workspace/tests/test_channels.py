# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 The Plinth Authors
"""End-to-end tests for the v0.2 Channels API."""

from __future__ import annotations

from urllib.parse import quote

import httpx


# ---------------------------------------------------------------------------
# Send + receive basics


async def test_send_then_receive(
    client: httpx.AsyncClient, workspace_id: str
) -> None:
    resp = await client.post(
        f"/v1/workspaces/{workspace_id}/channels/handoff/send",
        json={"payload": {"hello": "world"}, "sender": "alice"},
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["id"].startswith("msg_")
    assert body["channel"] == "handoff"
    assert body["seq"] == 1
    assert body["payload"] == {"hello": "world"}
    assert body["sender"] == "alice"
    assert body["sent_at"]
    assert body["delivered_at"] is None

    resp = await client.get(
        f"/v1/workspaces/{workspace_id}/channels/handoff/receive"
    )
    assert resp.status_code == 200
    msgs = resp.json()["messages"]
    assert len(msgs) == 1
    assert msgs[0]["payload"] == {"hello": "world"}
    assert msgs[0]["delivered_at"] is not None  # set on first non-peek receive


async def test_send_with_full_envelope(
    client: httpx.AsyncClient, workspace_id: str
) -> None:
    resp = await client.post(
        f"/v1/workspaces/{workspace_id}/channels/h/send",
        json={
            "payload": [1, 2, 3],
            "sender": "alice",
            "type": "research-complete",
            "correlation_id": "corr-1",
            "headers": {"trace": "abc"},
        },
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["type"] == "research-complete"
    assert body["correlation_id"] == "corr-1"
    assert body["headers"] == {"trace": "abc"}


async def test_send_workspace_404(client: httpx.AsyncClient) -> None:
    resp = await client.post(
        "/v1/workspaces/ws_nope/channels/x/send",
        json={"payload": "hi"},
    )
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "WORKSPACE_NOT_FOUND"


# ---------------------------------------------------------------------------
# seq is monotonic per channel


async def test_seq_monotonic_per_channel(
    client: httpx.AsyncClient, workspace_id: str
) -> None:
    seqs = []
    for i in range(5):
        resp = await client.post(
            f"/v1/workspaces/{workspace_id}/channels/orders/send",
            json={"payload": {"i": i}},
        )
        assert resp.status_code == 201
        seqs.append(resp.json()["seq"])
    assert seqs == [1, 2, 3, 4, 5]

    # Different channel restarts seq.
    other = await client.post(
        f"/v1/workspaces/{workspace_id}/channels/other/send",
        json={"payload": "x"},
    )
    assert other.json()["seq"] == 1


# ---------------------------------------------------------------------------
# `since` filtering


async def test_receive_since_filters(
    client: httpx.AsyncClient, workspace_id: str
) -> None:
    for i in range(3):
        await client.post(
            f"/v1/workspaces/{workspace_id}/channels/q/send",
            json={"payload": i},
        )

    resp = await client.get(
        f"/v1/workspaces/{workspace_id}/channels/q/receive?since=1"
    )
    assert resp.status_code == 200
    msgs = resp.json()["messages"]
    assert [m["seq"] for m in msgs] == [2, 3]


async def test_receive_limit(
    client: httpx.AsyncClient, workspace_id: str
) -> None:
    for i in range(5):
        await client.post(
            f"/v1/workspaces/{workspace_id}/channels/q/send",
            json={"payload": i},
        )
    resp = await client.get(
        f"/v1/workspaces/{workspace_id}/channels/q/receive?limit=2"
    )
    assert resp.status_code == 200
    msgs = resp.json()["messages"]
    assert [m["seq"] for m in msgs] == [1, 2]


# ---------------------------------------------------------------------------
# Consumer cursor


async def test_consumer_cursor_advances(
    client: httpx.AsyncClient, workspace_id: str
) -> None:
    for i in range(3):
        await client.post(
            f"/v1/workspaces/{workspace_id}/channels/q/send",
            json={"payload": i},
        )

    # First receive — gets all 3, cursor → 3.
    resp = await client.get(
        f"/v1/workspaces/{workspace_id}/channels/q/receive?consumer=writer"
    )
    assert resp.status_code == 200
    assert len(resp.json()["messages"]) == 3

    # Second receive — cursor is at 3, no new messages.
    resp = await client.get(
        f"/v1/workspaces/{workspace_id}/channels/q/receive?consumer=writer"
    )
    assert resp.status_code == 200
    assert resp.json()["messages"] == []

    # Send one more, cursor catches up.
    await client.post(
        f"/v1/workspaces/{workspace_id}/channels/q/send",
        json={"payload": "tail"},
    )
    resp = await client.get(
        f"/v1/workspaces/{workspace_id}/channels/q/receive?consumer=writer"
    )
    assert resp.status_code == 200
    msgs = resp.json()["messages"]
    assert len(msgs) == 1
    assert msgs[0]["seq"] == 4


async def test_explicit_since_overrides_consumer_cursor(
    client: httpx.AsyncClient, workspace_id: str
) -> None:
    for i in range(3):
        await client.post(
            f"/v1/workspaces/{workspace_id}/channels/q/send",
            json={"payload": i},
        )
    # advance cursor to 3
    await client.get(
        f"/v1/workspaces/{workspace_id}/channels/q/receive?consumer=writer"
    )

    # explicit since=0 should rewind, returning all messages.
    resp = await client.get(
        f"/v1/workspaces/{workspace_id}/channels/q/receive"
        "?consumer=writer&since=0"
    )
    assert resp.status_code == 200
    msgs = resp.json()["messages"]
    assert [m["seq"] for m in msgs] == [1, 2, 3]


async def test_two_consumers_have_independent_cursors(
    client: httpx.AsyncClient, workspace_id: str
) -> None:
    for i in range(3):
        await client.post(
            f"/v1/workspaces/{workspace_id}/channels/q/send",
            json={"payload": i},
        )

    a = await client.get(
        f"/v1/workspaces/{workspace_id}/channels/q/receive?consumer=A"
    )
    b = await client.get(
        f"/v1/workspaces/{workspace_id}/channels/q/receive?consumer=B"
    )
    assert len(a.json()["messages"]) == 3
    assert len(b.json()["messages"]) == 3


# ---------------------------------------------------------------------------
# Peek


async def test_peek_does_not_advance_cursor(
    client: httpx.AsyncClient, workspace_id: str
) -> None:
    for i in range(3):
        await client.post(
            f"/v1/workspaces/{workspace_id}/channels/q/send",
            json={"payload": i},
        )

    # peek=true: don't move cursor, don't set delivered_at.
    resp = await client.get(
        f"/v1/workspaces/{workspace_id}/channels/q/receive"
        "?consumer=writer&peek=true"
    )
    assert resp.status_code == 200
    msgs = resp.json()["messages"]
    assert len(msgs) == 3
    for m in msgs:
        assert m["delivered_at"] is None

    # Real receive still gets all 3.
    resp = await client.get(
        f"/v1/workspaces/{workspace_id}/channels/q/receive?consumer=writer"
    )
    assert len(resp.json()["messages"]) == 3


async def test_peek_does_not_set_delivered_at(
    client: httpx.AsyncClient, workspace_id: str
) -> None:
    await client.post(
        f"/v1/workspaces/{workspace_id}/channels/q/send",
        json={"payload": "hi"},
    )
    # Peek -> still NULL.
    resp = await client.get(
        f"/v1/workspaces/{workspace_id}/channels/q/receive?peek=true"
    )
    assert resp.json()["messages"][0]["delivered_at"] is None

    # Real receive -> filled.
    resp = await client.get(
        f"/v1/workspaces/{workspace_id}/channels/q/receive"
    )
    assert resp.json()["messages"][0]["delivered_at"] is not None


# ---------------------------------------------------------------------------
# Receive errors


async def test_receive_unknown_channel_404(
    client: httpx.AsyncClient, workspace_id: str
) -> None:
    resp = await client.get(
        f"/v1/workspaces/{workspace_id}/channels/never-sent/receive"
    )
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "CHANNEL_NOT_FOUND"


async def test_receive_workspace_404(client: httpx.AsyncClient) -> None:
    resp = await client.get("/v1/workspaces/ws_nope/channels/q/receive")
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "WORKSPACE_NOT_FOUND"


# ---------------------------------------------------------------------------
# Delete a single message


async def test_delete_message(
    client: httpx.AsyncClient, workspace_id: str
) -> None:
    a = await client.post(
        f"/v1/workspaces/{workspace_id}/channels/q/send",
        json={"payload": "a"},
    )
    msg_id = a.json()["id"]

    resp = await client.delete(
        f"/v1/workspaces/{workspace_id}/channels/q/messages/{msg_id}"
    )
    assert resp.status_code == 204

    # Now receive returns nothing.
    resp = await client.get(
        f"/v1/workspaces/{workspace_id}/channels/q/receive"
    )
    assert resp.json()["messages"] == []


async def test_delete_message_404(
    client: httpx.AsyncClient, workspace_id: str
) -> None:
    # Need to lazy-create the channel first.
    await client.post(
        f"/v1/workspaces/{workspace_id}/channels/q/send", json={"payload": 1}
    )
    resp = await client.delete(
        f"/v1/workspaces/{workspace_id}/channels/q/messages/msg_nope"
    )
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "MESSAGE_NOT_FOUND"


# ---------------------------------------------------------------------------
# List + get


async def test_list_channels_empty(
    client: httpx.AsyncClient, workspace_id: str
) -> None:
    resp = await client.get(f"/v1/workspaces/{workspace_id}/channels")
    assert resp.status_code == 200
    assert resp.json()["channels"] == []


async def test_list_channels(
    client: httpx.AsyncClient, workspace_id: str
) -> None:
    await client.post(
        f"/v1/workspaces/{workspace_id}/channels/a/send",
        json={"payload": 1},
    )
    await client.post(
        f"/v1/workspaces/{workspace_id}/channels/a/send",
        json={"payload": 2},
    )
    await client.post(
        f"/v1/workspaces/{workspace_id}/channels/b/send",
        json={"payload": 1},
    )
    resp = await client.get(f"/v1/workspaces/{workspace_id}/channels")
    assert resp.status_code == 200
    items = {c["name"]: c for c in resp.json()["channels"]}
    assert set(items) == {"a", "b"}
    assert items["a"]["message_count"] == 2
    assert items["b"]["message_count"] == 1
    assert items["a"]["last_send_at"]


async def test_get_channel(
    client: httpx.AsyncClient, workspace_id: str
) -> None:
    await client.post(
        f"/v1/workspaces/{workspace_id}/channels/single/send",
        json={"payload": 1},
    )
    resp = await client.get(
        f"/v1/workspaces/{workspace_id}/channels/single"
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["name"] == "single"
    assert body["message_count"] == 1


async def test_get_channel_404(
    client: httpx.AsyncClient, workspace_id: str
) -> None:
    resp = await client.get(f"/v1/workspaces/{workspace_id}/channels/nope")
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "CHANNEL_NOT_FOUND"


# ---------------------------------------------------------------------------
# Delete channel cascades


async def test_delete_channel_cascades(
    client: httpx.AsyncClient, workspace_id: str
) -> None:
    for i in range(3):
        await client.post(
            f"/v1/workspaces/{workspace_id}/channels/q/send",
            json={"payload": i},
        )
    # advance a consumer cursor so we have a row in channel_consumers
    await client.get(
        f"/v1/workspaces/{workspace_id}/channels/q/receive?consumer=writer"
    )

    resp = await client.delete(
        f"/v1/workspaces/{workspace_id}/channels/q"
    )
    assert resp.status_code == 204

    # All gone — get returns 404, list excludes it.
    resp = await client.get(f"/v1/workspaces/{workspace_id}/channels/q")
    assert resp.status_code == 404
    resp = await client.get(f"/v1/workspaces/{workspace_id}/channels")
    assert resp.json()["channels"] == []

    # Re-sending recreates the channel and seq starts back at 1.
    fresh = await client.post(
        f"/v1/workspaces/{workspace_id}/channels/q/send",
        json={"payload": "again"},
    )
    assert fresh.json()["seq"] == 1


async def test_delete_channel_404(
    client: httpx.AsyncClient, workspace_id: str
) -> None:
    resp = await client.delete(
        f"/v1/workspaces/{workspace_id}/channels/never-existed"
    )
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# Special chars in channel names


async def test_channel_name_special_chars(
    client: httpx.AsyncClient, workspace_id: str
) -> None:
    name = "agent-1_writer.v2"
    resp = await client.post(
        f"/v1/workspaces/{workspace_id}/channels/{quote(name, safe='')}/send",
        json={"payload": "hi"},
    )
    assert resp.status_code == 201
    assert resp.json()["channel"] == name

    resp = await client.get(
        f"/v1/workspaces/{workspace_id}/channels/{quote(name, safe='')}"
    )
    assert resp.status_code == 200
    assert resp.json()["name"] == name


# ---------------------------------------------------------------------------
# Payload variations


async def test_payload_can_be_anything(
    client: httpx.AsyncClient, workspace_id: str
) -> None:
    for payload in [
        "string",
        42,
        3.14,
        True,
        None,
        [1, 2, 3],
        {"nested": {"deep": [1, 2]}},
    ]:
        resp = await client.post(
            f"/v1/workspaces/{workspace_id}/channels/p/send",
            json={"payload": payload},
        )
        assert resp.status_code == 201
        assert resp.json()["payload"] == payload


# ---------------------------------------------------------------------------
# Limit clamping


async def test_limit_above_max_is_clamped(
    client: httpx.AsyncClient, workspace_id: str
) -> None:
    # FastAPI Query(le=1000) rejects beyond 1000.
    await client.post(
        f"/v1/workspaces/{workspace_id}/channels/q/send",
        json={"payload": 1},
    )
    resp = await client.get(
        f"/v1/workspaces/{workspace_id}/channels/q/receive?limit=10000"
    )
    assert resp.status_code == 400


async def test_limit_zero_rejected(
    client: httpx.AsyncClient, workspace_id: str
) -> None:
    await client.post(
        f"/v1/workspaces/{workspace_id}/channels/q/send",
        json={"payload": 1},
    )
    resp = await client.get(
        f"/v1/workspaces/{workspace_id}/channels/q/receive?limit=0"
    )
    assert resp.status_code == 400


# ---------------------------------------------------------------------------
# Receive sets last_receive_at on channel


async def test_receive_updates_last_receive_at(
    client: httpx.AsyncClient, workspace_id: str
) -> None:
    await client.post(
        f"/v1/workspaces/{workspace_id}/channels/q/send",
        json={"payload": 1},
    )

    before = (
        await client.get(f"/v1/workspaces/{workspace_id}/channels/q")
    ).json()
    assert before["last_receive_at"] is None

    await client.get(f"/v1/workspaces/{workspace_id}/channels/q/receive")
    after = (
        await client.get(f"/v1/workspaces/{workspace_id}/channels/q")
    ).json()
    assert after["last_receive_at"] is not None


async def test_delete_channel_workspace_404(client: httpx.AsyncClient) -> None:
    resp = await client.delete("/v1/workspaces/ws_nope/channels/x")
    assert resp.status_code == 404


async def test_delete_message_workspace_404(client: httpx.AsyncClient) -> None:
    resp = await client.delete(
        "/v1/workspaces/ws_nope/channels/x/messages/msg_nope"
    )
    assert resp.status_code == 404


async def test_list_channels_workspace_404(client: httpx.AsyncClient) -> None:
    resp = await client.get("/v1/workspaces/ws_nope/channels")
    assert resp.status_code == 404


async def test_get_channel_workspace_404(client: httpx.AsyncClient) -> None:
    resp = await client.get("/v1/workspaces/ws_nope/channels/x")
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# Spec scenario: send 5 → receive (consumer="a") → receive (consumer="a")
# returns 0 → send → receive (consumer="a") returns 1.


async def test_consumer_cursor_5_0_1_progression(
    client: httpx.AsyncClient, workspace_id: str
) -> None:
    for i in range(5):
        await client.post(
            f"/v1/workspaces/{workspace_id}/channels/q/send",
            json={"payload": i},
        )

    # First receive — should return all 5.
    resp = await client.get(
        f"/v1/workspaces/{workspace_id}/channels/q/receive?consumer=a"
    )
    assert len(resp.json()["messages"]) == 5

    # Second receive — cursor advanced, returns 0.
    resp = await client.get(
        f"/v1/workspaces/{workspace_id}/channels/q/receive?consumer=a"
    )
    assert resp.json()["messages"] == []

    # New send → next receive returns just 1.
    await client.post(
        f"/v1/workspaces/{workspace_id}/channels/q/send",
        json={"payload": "tail"},
    )
    resp = await client.get(
        f"/v1/workspaces/{workspace_id}/channels/q/receive?consumer=a"
    )
    msgs = resp.json()["messages"]
    assert len(msgs) == 1
    assert msgs[0]["payload"] == "tail"


async def test_channel_auto_created_on_first_send(
    client: httpx.AsyncClient, workspace_id: str
) -> None:
    """Sending to a brand-new name implicitly creates the channel row."""

    # Sanity: list is empty before send.
    resp = await client.get(f"/v1/workspaces/{workspace_id}/channels")
    assert resp.json()["channels"] == []

    await client.post(
        f"/v1/workspaces/{workspace_id}/channels/auto/send",
        json={"payload": "x"},
    )

    resp = await client.get(f"/v1/workspaces/{workspace_id}/channels/auto")
    assert resp.status_code == 200
    assert resp.json()["name"] == "auto"
    assert resp.json()["message_count"] == 1


async def test_multiple_senders_ordered_by_seq(
    client: httpx.AsyncClient, workspace_id: str
) -> None:
    """Independent senders writing to the same channel see a single order."""

    senders = ["alice", "bob", "carol", "alice", "bob"]
    for s in senders:
        await client.post(
            f"/v1/workspaces/{workspace_id}/channels/q/send",
            json={"payload": s, "sender": s},
        )
    resp = await client.get(
        f"/v1/workspaces/{workspace_id}/channels/q/receive"
    )
    msgs = resp.json()["messages"]
    assert [m["sender"] for m in msgs] == senders
    assert [m["seq"] for m in msgs] == [1, 2, 3, 4, 5]
