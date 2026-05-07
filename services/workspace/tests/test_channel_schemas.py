# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 The Plinth Authors
"""End-to-end tests for v0.5 typed channels + dead-letter queue."""

from __future__ import annotations

from typing import Any

import httpx
import pytest


# ---------------------------------------------------------------------------
# Helpers


SIMPLE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["topic", "sources"],
    "properties": {
        "topic": {"type": "string"},
        "sources": {"type": "array", "items": {"type": "string"}},
    },
    "additionalProperties": False,
}


async def _make_workspace(client: httpx.AsyncClient, name: str = "ws") -> str:
    resp = await client.post("/v1/workspaces", json={"name": name})
    assert resp.status_code == 201, resp.text
    return resp.json()["id"]


async def _set_schema(
    client: httpx.AsyncClient,
    ws: str,
    channel: str,
    schema: dict[str, Any],
) -> dict[str, Any]:
    resp = await client.post(
        f"/v1/workspaces/{ws}/channels/{channel}/schema",
        json={"schema": schema},
    )
    assert resp.status_code == 200, resp.text
    return resp.json()


# ---------------------------------------------------------------------------
# Schema CRUD


async def test_set_get_delete_schema(
    client: httpx.AsyncClient, workspace_id: str
) -> None:
    """PUT a schema, read it back, then delete it."""
    body = await _set_schema(client, workspace_id, "research-out", SIMPLE_SCHEMA)
    assert body["channel_name"] == "research-out"
    assert body["workspace_id"] == workspace_id
    assert body["version"] == 1
    assert body["schema_json"] == SIMPLE_SCHEMA

    resp = await client.get(
        f"/v1/workspaces/{workspace_id}/channels/research-out/schema"
    )
    assert resp.status_code == 200
    assert resp.json()["version"] == 1
    assert resp.json()["schema_json"] == SIMPLE_SCHEMA

    resp = await client.delete(
        f"/v1/workspaces/{workspace_id}/channels/research-out/schema"
    )
    assert resp.status_code == 204

    resp = await client.get(
        f"/v1/workspaces/{workspace_id}/channels/research-out/schema"
    )
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "SCHEMA_NOT_FOUND"


async def test_get_schema_when_unset_returns_404(
    client: httpx.AsyncClient, workspace_id: str
) -> None:
    resp = await client.get(
        f"/v1/workspaces/{workspace_id}/channels/never-typed/schema"
    )
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "SCHEMA_NOT_FOUND"


async def test_set_schema_increments_version(
    client: httpx.AsyncClient, workspace_id: str
) -> None:
    """Each PUT bumps the version monotonically."""
    body1 = await _set_schema(client, workspace_id, "out", SIMPLE_SCHEMA)
    assert body1["version"] == 1

    body2 = await _set_schema(
        client,
        workspace_id,
        "out",
        {**SIMPLE_SCHEMA, "title": "v2"},
    )
    assert body2["version"] == 2

    body3 = await _set_schema(
        client,
        workspace_id,
        "out",
        {"type": "object"},
    )
    assert body3["version"] == 3


async def test_set_invalid_schema_rejected(
    client: httpx.AsyncClient, workspace_id: str
) -> None:
    """A structurally bad schema document is rejected with 400."""
    resp = await client.post(
        f"/v1/workspaces/{workspace_id}/channels/x/schema",
        json={"schema": {"type": "not-a-real-type"}},
    )
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "INVALID_ARGUMENTS"


async def test_delete_unknown_schema_is_idempotent(
    client: httpx.AsyncClient, workspace_id: str
) -> None:
    """DELETE on a channel without a schema still returns 204."""
    resp = await client.delete(
        f"/v1/workspaces/{workspace_id}/channels/no-schema/schema"
    )
    assert resp.status_code == 204


# ---------------------------------------------------------------------------
# Send-time validation


async def test_valid_payload_goes_to_main_channel(
    client: httpx.AsyncClient, workspace_id: str
) -> None:
    await _set_schema(client, workspace_id, "out", SIMPLE_SCHEMA)
    resp = await client.post(
        f"/v1/workspaces/{workspace_id}/channels/out/send",
        json={"payload": {"topic": "ai", "sources": ["s1"]}},
    )
    assert resp.status_code == 201, resp.text
    assert resp.json()["channel"] == "out"

    msgs = (
        await client.get(
            f"/v1/workspaces/{workspace_id}/channels/out/receive"
        )
    ).json()["messages"]
    assert len(msgs) == 1
    assert msgs[0]["payload"]["topic"] == "ai"


async def test_invalid_payload_routes_to_dlq(
    client: httpx.AsyncClient, workspace_id: str
) -> None:
    """Validation failures return 422 + populate the DLQ; main stays empty."""
    await _set_schema(client, workspace_id, "out", SIMPLE_SCHEMA)
    resp = await client.post(
        f"/v1/workspaces/{workspace_id}/channels/out/send",
        json={"payload": {"topic": "ai"}},  # missing 'sources'
    )
    assert resp.status_code == 422, resp.text
    body = resp.json()
    assert body["error"]["code"] == "SCHEMA_VIOLATION"
    assert body["error"]["details"]["channel"] == "out"
    assert body["error"]["details"]["deadletter_msg_id"].startswith("msg_")
    assert isinstance(body["error"]["details"]["errors"], list)
    assert body["error"]["details"]["errors"][0]["message"]

    # Main channel didn't receive — but it also wasn't ever created since
    # the only send went to the DLQ. Verify with the DLQ list endpoint.
    dlq = (
        await client.get(
            f"/v1/workspaces/{workspace_id}/channels/out/deadletter"
        )
    ).json()["messages"]
    assert len(dlq) == 1
    assert dlq[0]["channel"] == "out.deadletter"
    assert dlq[0]["headers"]["x-original-channel"] == "out"
    assert "x-validation-errors" in dlq[0]["headers"]
    assert dlq[0]["headers"]["x-schema-version"] == "1"


async def test_unschemaed_channel_skips_validation(
    client: httpx.AsyncClient, workspace_id: str
) -> None:
    """A channel without a schema accepts anything (back-compat with v0.2)."""
    resp = await client.post(
        f"/v1/workspaces/{workspace_id}/channels/free-form/send",
        json={"payload": {"x": 1}},
    )
    assert resp.status_code == 201
    msgs = (
        await client.get(
            f"/v1/workspaces/{workspace_id}/channels/free-form/receive"
        )
    ).json()["messages"]
    assert len(msgs) == 1


async def test_deleting_schema_disables_validation(
    client: httpx.AsyncClient, workspace_id: str
) -> None:
    """After DELETE on the schema, previously-invalid payloads succeed."""
    await _set_schema(client, workspace_id, "out", SIMPLE_SCHEMA)
    resp = await client.post(
        f"/v1/workspaces/{workspace_id}/channels/out/send",
        json={"payload": {"topic": "x"}},
    )
    assert resp.status_code == 422

    await client.delete(
        f"/v1/workspaces/{workspace_id}/channels/out/schema"
    )
    resp = await client.post(
        f"/v1/workspaces/{workspace_id}/channels/out/send",
        json={"payload": {"topic": "x"}},
    )
    assert resp.status_code == 201


# ---------------------------------------------------------------------------
# DLQ list / replay / drop


async def test_list_deadletter_empty_when_clean(
    client: httpx.AsyncClient, workspace_id: str
) -> None:
    """No DLQ rows → empty list, not 404."""
    await _set_schema(client, workspace_id, "out", SIMPLE_SCHEMA)
    resp = await client.get(
        f"/v1/workspaces/{workspace_id}/channels/out/deadletter"
    )
    assert resp.status_code == 200
    assert resp.json()["messages"] == []


async def test_list_deadletter_returns_messages(
    client: httpx.AsyncClient, workspace_id: str
) -> None:
    """DLQ list returns failed messages in seq order."""
    await _set_schema(client, workspace_id, "out", SIMPLE_SCHEMA)
    for _ in range(3):
        await client.post(
            f"/v1/workspaces/{workspace_id}/channels/out/send",
            json={"payload": {"topic": "x"}},
        )
    msgs = (
        await client.get(
            f"/v1/workspaces/{workspace_id}/channels/out/deadletter"
        )
    ).json()["messages"]
    assert len(msgs) == 3
    assert [m["seq"] for m in msgs] == [1, 2, 3]


async def test_replay_after_schema_relaxed(
    client: httpx.AsyncClient, workspace_id: str
) -> None:
    """A DLQ message that newly passes validation moves to the main channel."""
    await _set_schema(client, workspace_id, "out", SIMPLE_SCHEMA)
    resp = await client.post(
        f"/v1/workspaces/{workspace_id}/channels/out/send",
        json={"payload": {"topic": "ai"}},
    )
    dlq_id = resp.json()["error"]["details"]["deadletter_msg_id"]

    # Relax schema: now ``sources`` is optional.
    relaxed = dict(SIMPLE_SCHEMA)
    relaxed["required"] = ["topic"]
    await _set_schema(client, workspace_id, "out", relaxed)

    resp = await client.post(
        f"/v1/workspaces/{workspace_id}/channels/out/deadletter/{dlq_id}/replay"
    )
    assert resp.status_code == 200, resp.text
    new_msg = resp.json()
    assert new_msg["channel"] == "out"
    assert new_msg["payload"] == {"topic": "ai"}

    # DLQ now empty.
    dlq = (
        await client.get(
            f"/v1/workspaces/{workspace_id}/channels/out/deadletter"
        )
    ).json()["messages"]
    assert dlq == []

    # Main channel has it.
    main = (
        await client.get(
            f"/v1/workspaces/{workspace_id}/channels/out/receive"
        )
    ).json()["messages"]
    assert len(main) == 1
    assert main[0]["payload"] == {"topic": "ai"}


async def test_replay_still_invalid_keeps_dlq(
    client: httpx.AsyncClient, workspace_id: str
) -> None:
    """A still-invalid message bounces back to the DLQ untouched."""
    await _set_schema(client, workspace_id, "out", SIMPLE_SCHEMA)
    resp = await client.post(
        f"/v1/workspaces/{workspace_id}/channels/out/send",
        json={"payload": {"topic": "x"}},
    )
    dlq_id = resp.json()["error"]["details"]["deadletter_msg_id"]

    resp = await client.post(
        f"/v1/workspaces/{workspace_id}/channels/out/deadletter/{dlq_id}/replay"
    )
    assert resp.status_code == 422
    body = resp.json()
    assert body["error"]["code"] == "SCHEMA_VIOLATION"
    assert body["error"]["details"]["deadletter_msg_id"] == dlq_id

    # Still in DLQ.
    dlq = (
        await client.get(
            f"/v1/workspaces/{workspace_id}/channels/out/deadletter"
        )
    ).json()["messages"]
    assert len(dlq) == 1
    assert dlq[0]["id"] == dlq_id


async def test_replay_unknown_message_404(
    client: httpx.AsyncClient, workspace_id: str
) -> None:
    """Replaying a non-existent DLQ message → 404."""
    resp = await client.post(
        f"/v1/workspaces/{workspace_id}/channels/out/deadletter/msg_does_not_exist/replay"
    )
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "MESSAGE_NOT_FOUND"


async def test_drop_deadletter_removes_message(
    client: httpx.AsyncClient, workspace_id: str
) -> None:
    """DELETE on a DLQ message removes it without sending to main."""
    await _set_schema(client, workspace_id, "out", SIMPLE_SCHEMA)
    resp = await client.post(
        f"/v1/workspaces/{workspace_id}/channels/out/send",
        json={"payload": {"topic": "x"}},
    )
    dlq_id = resp.json()["error"]["details"]["deadletter_msg_id"]

    resp = await client.delete(
        f"/v1/workspaces/{workspace_id}/channels/out/deadletter/{dlq_id}"
    )
    assert resp.status_code == 204

    dlq = (
        await client.get(
            f"/v1/workspaces/{workspace_id}/channels/out/deadletter"
        )
    ).json()["messages"]
    assert dlq == []


async def test_drop_unknown_deadletter_404(
    client: httpx.AsyncClient, workspace_id: str
) -> None:
    resp = await client.delete(
        f"/v1/workspaces/{workspace_id}/channels/out/deadletter/msg_unknown"
    )
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "MESSAGE_NOT_FOUND"


# ---------------------------------------------------------------------------
# DLQ hidden from regular listings


async def test_dlq_hidden_from_channel_listing(
    client: httpx.AsyncClient, workspace_id: str
) -> None:
    """``GET /channels`` filters out ``.deadletter`` sub-channels."""
    await _set_schema(client, workspace_id, "out", SIMPLE_SCHEMA)
    # Trigger DLQ creation.
    await client.post(
        f"/v1/workspaces/{workspace_id}/channels/out/send",
        json={"payload": {"topic": "x"}},
    )
    # Also send a valid one so the main channel exists.
    await client.post(
        f"/v1/workspaces/{workspace_id}/channels/out/send",
        json={"payload": {"topic": "ai", "sources": ["s"]}},
    )

    listing = (
        await client.get(f"/v1/workspaces/{workspace_id}/channels")
    ).json()["channels"]
    names = {c["name"] for c in listing}
    assert "out" in names
    assert "out.deadletter" not in names
    assert all(not n.endswith(".deadletter") for n in names)


# ---------------------------------------------------------------------------
# Tenant isolation


async def test_schema_is_tenant_scoped(
    client: httpx.AsyncClient, workspace_id: str
) -> None:
    """A schema set on one workspace doesn't leak to another in the same tenant.

    This is a workspace-isolation check (the schema_store keys on
    workspace_id). Tenant-vs-tenant separation derives transitively from
    the workspace's tenant_id.
    """
    other_ws = await _make_workspace(client, name="other")
    await _set_schema(client, workspace_id, "out", SIMPLE_SCHEMA)

    # The other workspace has no schema attached.
    resp = await client.get(
        f"/v1/workspaces/{other_ws}/channels/out/schema"
    )
    assert resp.status_code == 404
    # And invalid payloads on the other ws still go through.
    resp = await client.post(
        f"/v1/workspaces/{other_ws}/channels/out/send",
        json={"payload": {"topic": "no-schema"}},
    )
    assert resp.status_code == 201


async def test_dlq_messages_isolated_per_workspace(
    client: httpx.AsyncClient, workspace_id: str
) -> None:
    """DLQ rows from one workspace are invisible to another."""
    other_ws = await _make_workspace(client, name="other")
    await _set_schema(client, workspace_id, "out", SIMPLE_SCHEMA)
    await client.post(
        f"/v1/workspaces/{workspace_id}/channels/out/send",
        json={"payload": {"topic": "x"}},
    )
    other_dlq = (
        await client.get(
            f"/v1/workspaces/{other_ws}/channels/out/deadletter"
        )
    ).json()["messages"]
    assert other_dlq == []


# ---------------------------------------------------------------------------
# Workspace-not-found surfaces


async def test_set_schema_unknown_workspace_404(client: httpx.AsyncClient) -> None:
    resp = await client.post(
        "/v1/workspaces/ws_doesnotexist/channels/x/schema",
        json={"schema": {"type": "object"}},
    )
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "WORKSPACE_NOT_FOUND"


async def test_send_with_dlq_does_not_pollute_main_channel_seq(
    client: httpx.AsyncClient, workspace_id: str
) -> None:
    """Failed sends don't burn a sequence number on the main channel."""
    await _set_schema(client, workspace_id, "out", SIMPLE_SCHEMA)

    # 3 invalid sends — go to DLQ only.
    for _ in range(3):
        await client.post(
            f"/v1/workspaces/{workspace_id}/channels/out/send",
            json={"payload": {"topic": "x"}},
        )

    # Now a valid one — should be seq=1 on the main channel.
    resp = await client.post(
        f"/v1/workspaces/{workspace_id}/channels/out/send",
        json={"payload": {"topic": "ai", "sources": ["s"]}},
    )
    assert resp.status_code == 201
    assert resp.json()["seq"] == 1


async def test_existing_messages_unaffected_by_new_schema(
    client: httpx.AsyncClient, workspace_id: str
) -> None:
    """Schemas only apply at send time — already-on-channel messages stay."""
    # No schema yet — send permissive payload.
    resp = await client.post(
        f"/v1/workspaces/{workspace_id}/channels/out/send",
        json={"payload": {"topic": "ai"}},
    )
    assert resp.status_code == 201

    # Now attach a strict schema.
    await _set_schema(client, workspace_id, "out", SIMPLE_SCHEMA)

    # Existing message survives.
    msgs = (
        await client.get(
            f"/v1/workspaces/{workspace_id}/channels/out/receive"
        )
    ).json()["messages"]
    assert len(msgs) == 1
    assert msgs[0]["payload"] == {"topic": "ai"}

    # But a brand-new invalid send goes to DLQ.
    resp = await client.post(
        f"/v1/workspaces/{workspace_id}/channels/out/send",
        json={"payload": {"topic": "ai"}},
    )
    assert resp.status_code == 422


async def test_replay_strips_internal_dlq_headers(
    client: httpx.AsyncClient, workspace_id: str
) -> None:
    """Replayed messages don't carry the synthetic ``x-validation-*`` headers."""
    await _set_schema(client, workspace_id, "out", SIMPLE_SCHEMA)
    resp = await client.post(
        f"/v1/workspaces/{workspace_id}/channels/out/send",
        json={
            "payload": {"topic": "ai"},
            "headers": {"trace": "abc"},
        },
    )
    dlq_id = resp.json()["error"]["details"]["deadletter_msg_id"]

    relaxed = dict(SIMPLE_SCHEMA)
    relaxed["required"] = ["topic"]
    await _set_schema(client, workspace_id, "out", relaxed)

    resp = await client.post(
        f"/v1/workspaces/{workspace_id}/channels/out/deadletter/{dlq_id}/replay"
    )
    assert resp.status_code == 200
    headers = resp.json()["headers"]
    # Original user header preserved.
    assert headers.get("trace") == "abc"
    # Synthetic markers stripped.
    assert "x-original-channel" not in headers
    assert "x-validation-errors" not in headers
    assert "x-failed-at" not in headers
    assert "x-schema-version" not in headers


async def test_dlq_pagination_via_since(
    client: httpx.AsyncClient, workspace_id: str
) -> None:
    """``?since=`` skips DLQ rows with seq <= since."""
    await _set_schema(client, workspace_id, "out", SIMPLE_SCHEMA)
    for _ in range(5):
        await client.post(
            f"/v1/workspaces/{workspace_id}/channels/out/send",
            json={"payload": {"topic": "x"}},
        )

    resp = await client.get(
        f"/v1/workspaces/{workspace_id}/channels/out/deadletter?since=2"
    )
    assert resp.status_code == 200
    seqs = [m["seq"] for m in resp.json()["messages"]]
    assert seqs == [3, 4, 5]


async def test_dlq_pagination_via_limit(
    client: httpx.AsyncClient, workspace_id: str
) -> None:
    await _set_schema(client, workspace_id, "out", SIMPLE_SCHEMA)
    for _ in range(5):
        await client.post(
            f"/v1/workspaces/{workspace_id}/channels/out/send",
            json={"payload": {"topic": "x"}},
        )

    resp = await client.get(
        f"/v1/workspaces/{workspace_id}/channels/out/deadletter?limit=2"
    )
    assert resp.status_code == 200
    msgs = resp.json()["messages"]
    assert len(msgs) == 2


async def test_unknown_workspace_dlq_404(client: httpx.AsyncClient) -> None:
    resp = await client.get(
        "/v1/workspaces/ws_unknown/channels/out/deadletter"
    )
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "WORKSPACE_NOT_FOUND"


async def test_invalid_payload_with_full_envelope_preserves_metadata(
    client: httpx.AsyncClient, workspace_id: str
) -> None:
    """User-supplied sender/type/correlation_id flow through to the DLQ row."""
    await _set_schema(client, workspace_id, "out", SIMPLE_SCHEMA)
    resp = await client.post(
        f"/v1/workspaces/{workspace_id}/channels/out/send",
        json={
            "payload": {"topic": "x"},
            "sender": "alice",
            "type": "research-complete",
            "correlation_id": "corr-1",
        },
    )
    dlq_id = resp.json()["error"]["details"]["deadletter_msg_id"]

    msgs = (
        await client.get(
            f"/v1/workspaces/{workspace_id}/channels/out/deadletter"
        )
    ).json()["messages"]
    [msg] = msgs
    assert msg["id"] == dlq_id
    assert msg["sender"] == "alice"
    assert msg["type"] == "research-complete"
    assert msg["correlation_id"] == "corr-1"


async def test_send_to_dlq_directly_skips_validation(
    client: httpx.AsyncClient, workspace_id: str
) -> None:
    """Sending directly to ``<channel>.deadletter`` bypasses validation.

    It's a hidden channel for DLQ-only writes; we don't want validation to
    create an infinite loop (DLQ-of-DLQ-of-DLQ…).
    """
    await _set_schema(client, workspace_id, "out", SIMPLE_SCHEMA)
    resp = await client.post(
        f"/v1/workspaces/{workspace_id}/channels/out.deadletter/send",
        json={"payload": {"topic": "x"}},  # invalid against schema
    )
    # The DLQ channel itself never validates — accepts anything.
    assert resp.status_code == 201
