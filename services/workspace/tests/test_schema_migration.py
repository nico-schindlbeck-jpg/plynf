# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 The Plinth Authors
"""End-to-end tests for v0.6 channel schema migration helpers.

Covers:
- ``POST .../schema/check`` (preview compatibility against a candidate schema)
- ``POST .../deadletter/replay-all`` (bulk DLQ replay, with dry-run)
- ``DELETE .../deadletter`` (purge old / all DLQ rows)

Tests run against the real FastAPI app via ``httpx.AsyncClient`` (see
``conftest.py`` for the fixtures); SQLite is tmp-dir backed.
"""

from __future__ import annotations

from typing import Any

import httpx


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

# A schema with NO ``required`` constraints — makes any object payload valid.
RELAXED_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "topic": {"type": "string"},
        "sources": {"type": "array", "items": {"type": "string"}},
    },
}

# A schema *stricter* than SIMPLE_SCHEMA: also requires ``priority``.
STRICTER_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["topic", "sources", "priority"],
    "properties": {
        "topic": {"type": "string"},
        "sources": {"type": "array", "items": {"type": "string"}},
        "priority": {"type": "integer"},
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


async def _send(
    client: httpx.AsyncClient,
    ws: str,
    channel: str,
    payload: Any,
) -> httpx.Response:
    return await client.post(
        f"/v1/workspaces/{ws}/channels/{channel}/send",
        json={"payload": payload},
    )


async def _seed_dlq(
    client: httpx.AsyncClient,
    ws: str,
    channel: str,
    count: int,
) -> list[str]:
    """Send ``count`` invalid payloads against a SIMPLE_SCHEMA-typed channel.

    Returns the DLQ message IDs in seq order.
    """

    ids: list[str] = []
    for _ in range(count):
        resp = await _send(client, ws, channel, {"topic": "x"})  # missing sources
        assert resp.status_code == 422
        ids.append(resp.json()["error"]["details"]["deadletter_msg_id"])
    return ids


# ---------------------------------------------------------------------------
# /schema/check


async def test_check_against_current_schema_reports_all_valid_for_main(
    client: httpx.AsyncClient, workspace_id: str
) -> None:
    """Main-channel rows that already passed schema validation stay valid."""

    await _set_schema(client, workspace_id, "out", SIMPLE_SCHEMA)
    for i in range(3):
        resp = await _send(
            client,
            workspace_id,
            "out",
            {"topic": f"t{i}", "sources": ["s"]},
        )
        assert resp.status_code == 201

    resp = await client.post(
        f"/v1/workspaces/{workspace_id}/channels/out/schema/check",
        json={"schema": SIMPLE_SCHEMA, "scope": "main"},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["channel"] == "out"
    assert body["scope"] == "main"
    assert body["checked"] == 3
    assert body["valid"] == 3
    assert body["invalid"] == 0
    assert body["sample_failures"] == []


async def test_check_with_stricter_schema_reports_invalid_count_and_samples(
    client: httpx.AsyncClient, workspace_id: str
) -> None:
    """A stricter candidate flags previously-valid messages."""

    await _set_schema(client, workspace_id, "out", SIMPLE_SCHEMA)
    for i in range(5):
        await _send(
            client,
            workspace_id,
            "out",
            {"topic": f"t{i}", "sources": ["s"]},  # passes SIMPLE_SCHEMA
        )

    resp = await client.post(
        f"/v1/workspaces/{workspace_id}/channels/out/schema/check",
        json={"schema": STRICTER_SCHEMA, "scope": "main"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["checked"] == 5
    assert body["valid"] == 0
    assert body["invalid"] == 5
    # Bounded — 10 samples max — but here we only have 5 invalids.
    assert len(body["sample_failures"]) == 5
    sample = body["sample_failures"][0]
    assert sample["msg_id"].startswith("msg_")
    assert isinstance(sample["errors"], list)
    assert sample["errors"][0]["message"]


async def test_check_sample_failures_capped_at_ten(
    client: httpx.AsyncClient, workspace_id: str
) -> None:
    """Even with 25 failures, the response carries at most 10 samples."""

    await _set_schema(client, workspace_id, "out", SIMPLE_SCHEMA)
    for i in range(25):
        await _send(
            client,
            workspace_id,
            "out",
            {"topic": f"t{i}", "sources": ["s"]},
        )

    resp = await client.post(
        f"/v1/workspaces/{workspace_id}/channels/out/schema/check",
        json={"schema": STRICTER_SCHEMA, "scope": "main"},
    )
    body = resp.json()
    assert body["checked"] == 25
    assert body["invalid"] == 25
    assert len(body["sample_failures"]) == 10  # bounded


async def test_check_limit_caps_iteration(
    client: httpx.AsyncClient, workspace_id: str
) -> None:
    """``limit=10`` doesn't iterate past 10 messages even with more available."""

    await _set_schema(client, workspace_id, "out", SIMPLE_SCHEMA)
    for i in range(50):
        await _send(
            client,
            workspace_id,
            "out",
            {"topic": f"t{i}", "sources": ["s"]},
        )

    resp = await client.post(
        f"/v1/workspaces/{workspace_id}/channels/out/schema/check",
        json={"schema": SIMPLE_SCHEMA, "scope": "main", "limit": 10},
    )
    body = resp.json()
    assert body["checked"] == 10
    assert body["valid"] == 10


async def test_check_scope_deadletter_only(
    client: httpx.AsyncClient, workspace_id: str
) -> None:
    """``scope=deadletter`` checks only DLQ messages, not the main channel."""

    await _set_schema(client, workspace_id, "out", SIMPLE_SCHEMA)
    # 4 main-channel valid sends.
    for i in range(4):
        await _send(
            client,
            workspace_id,
            "out",
            {"topic": f"t{i}", "sources": ["s"]},
        )
    # 3 DLQ rows (invalid against SIMPLE_SCHEMA).
    await _seed_dlq(client, workspace_id, "out", 3)

    resp = await client.post(
        f"/v1/workspaces/{workspace_id}/channels/out/schema/check",
        json={"schema": SIMPLE_SCHEMA, "scope": "deadletter"},
    )
    body = resp.json()
    assert body["scope"] == "deadletter"
    # Only DLQ rows are scanned; against SIMPLE_SCHEMA they're still invalid.
    assert body["checked"] == 3
    assert body["invalid"] == 3
    assert body["valid"] == 0


async def test_check_scope_both_combines_main_and_deadletter(
    client: httpx.AsyncClient, workspace_id: str
) -> None:
    """``scope=both`` covers both the main channel AND its DLQ."""

    await _set_schema(client, workspace_id, "out", SIMPLE_SCHEMA)
    for i in range(2):
        await _send(
            client,
            workspace_id,
            "out",
            {"topic": f"t{i}", "sources": ["s"]},  # main, valid against SIMPLE
        )
    await _seed_dlq(client, workspace_id, "out", 3)

    resp = await client.post(
        f"/v1/workspaces/{workspace_id}/channels/out/schema/check",
        json={"schema": SIMPLE_SCHEMA, "scope": "both"},
    )
    body = resp.json()
    assert body["checked"] == 5
    assert body["valid"] == 2
    assert body["invalid"] == 3


async def test_check_does_not_mutate(
    client: httpx.AsyncClient, workspace_id: str
) -> None:
    """``check`` is purely read-only — DLQ + main channel rows untouched."""

    await _set_schema(client, workspace_id, "out", SIMPLE_SCHEMA)
    await _seed_dlq(client, workspace_id, "out", 3)
    for i in range(2):
        await _send(
            client,
            workspace_id,
            "out",
            {"topic": f"t{i}", "sources": ["s"]},
        )

    # Run check.
    await client.post(
        f"/v1/workspaces/{workspace_id}/channels/out/schema/check",
        json={"schema": RELAXED_SCHEMA, "scope": "both"},
    )

    # DLQ count unchanged.
    dlq = (
        await client.get(
            f"/v1/workspaces/{workspace_id}/channels/out/deadletter"
        )
    ).json()["messages"]
    assert len(dlq) == 3
    # Main count unchanged.
    main = (
        await client.get(
            f"/v1/workspaces/{workspace_id}/channels/out/receive"
        )
    ).json()["messages"]
    assert len(main) == 2


async def test_check_invalid_schema_rejected(
    client: httpx.AsyncClient, workspace_id: str
) -> None:
    """A malformed candidate schema returns 400 INVALID_ARGUMENTS."""

    resp = await client.post(
        f"/v1/workspaces/{workspace_id}/channels/out/schema/check",
        json={"schema": {"type": "not-a-real-type"}},
    )
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "INVALID_ARGUMENTS"


# ---------------------------------------------------------------------------
# /deadletter/replay-all


async def test_replay_all_dry_run_reports_what_would_succeed(
    client: httpx.AsyncClient, workspace_id: str
) -> None:
    """Dry-run after schema relaxation reports 5 would-succeed."""

    await _set_schema(client, workspace_id, "out", SIMPLE_SCHEMA)
    await _seed_dlq(client, workspace_id, "out", 5)

    # Relax — every payload now passes.
    await _set_schema(client, workspace_id, "out", RELAXED_SCHEMA)

    resp = await client.post(
        f"/v1/workspaces/{workspace_id}/channels/out/deadletter/replay-all"
        "?dry_run=true"
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["dry_run"] is True
    assert body["attempted"] == 5
    assert body["succeeded"] == 5
    assert body["failed"] == 0
    assert body["failures"] == []

    # Nothing actually moved — DLQ still has 5.
    dlq = (
        await client.get(
            f"/v1/workspaces/{workspace_id}/channels/out/deadletter"
        )
    ).json()["messages"]
    assert len(dlq) == 5


async def test_replay_all_actual_run_moves_all_to_main(
    client: httpx.AsyncClient, workspace_id: str
) -> None:
    """Actual replay-all moves the previously-invalid messages to main."""

    await _set_schema(client, workspace_id, "out", SIMPLE_SCHEMA)
    await _seed_dlq(client, workspace_id, "out", 5)
    await _set_schema(client, workspace_id, "out", RELAXED_SCHEMA)

    resp = await client.post(
        f"/v1/workspaces/{workspace_id}/channels/out/deadletter/replay-all"
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["attempted"] == 5
    assert body["succeeded"] == 5
    assert body["failed"] == 0
    assert body["dry_run"] is False

    # DLQ now empty.
    dlq = (
        await client.get(
            f"/v1/workspaces/{workspace_id}/channels/out/deadletter"
        )
    ).json()["messages"]
    assert dlq == []

    # Main has 5.
    main = (
        await client.get(
            f"/v1/workspaces/{workspace_id}/channels/out/receive"
        )
    ).json()["messages"]
    assert len(main) == 5


async def test_replay_all_partial_success(
    client: httpx.AsyncClient, workspace_id: str
) -> None:
    """3 succeed, 2 still invalid → kept in DLQ with reasons."""

    # Strict schema rejects payloads missing 'topic'.
    strict = {
        "type": "object",
        "required": ["topic"],
        "properties": {"topic": {"type": "string"}},
    }
    await _set_schema(client, workspace_id, "out", strict)

    # Send 3 valid (will go to main).
    for i in range(3):
        resp = await _send(client, workspace_id, "out", {"topic": f"t{i}"})
        assert resp.status_code == 201

    # Now make a NEW schema requiring 'sources' — and send via the OLD schema's
    # acceptable shape so messages land in DLQ. We do this by setting the
    # strict 'sources' schema and sending payloads without sources.
    await _set_schema(client, workspace_id, "out", SIMPLE_SCHEMA)
    # 3 messages with topic+sources → land on main fine.
    for i in range(3):
        await _send(
            client, workspace_id, "out", {"topic": f"v{i}", "sources": ["s"]}
        )
    # 2 messages without sources → DLQ.
    await _seed_dlq(client, workspace_id, "out", 2)
    # 3 messages with sources but topic omitted → DLQ (no 'topic').
    for _ in range(3):
        resp = await _send(
            client,
            workspace_id,
            "out",
            {"sources": ["a"]},  # missing topic
        )
        assert resp.status_code == 422

    # Now we have 5 DLQ rows. Relax the schema only for 'sources' — keep
    # 'topic' required. Replay should succeed for the 2 with topic and fail
    # for the 3 without.
    topic_only = {
        "type": "object",
        "required": ["topic"],
        "properties": {
            "topic": {"type": "string"},
            "sources": {"type": "array"},
        },
    }
    await _set_schema(client, workspace_id, "out", topic_only)

    resp = await client.post(
        f"/v1/workspaces/{workspace_id}/channels/out/deadletter/replay-all"
    )
    body = resp.json()
    assert body["attempted"] == 5
    assert body["succeeded"] == 2
    assert body["failed"] == 3
    # Each failure carries a msg_id + a reason string.
    assert len(body["failures"]) == 3
    for f in body["failures"]:
        assert f["msg_id"].startswith("msg_")
        assert isinstance(f["reason"], str)


async def test_replay_all_max_caps_iteration(
    client: httpx.AsyncClient, workspace_id: str
) -> None:
    """``?max=2`` only attempts the first two DLQ rows."""

    await _set_schema(client, workspace_id, "out", SIMPLE_SCHEMA)
    await _seed_dlq(client, workspace_id, "out", 5)
    await _set_schema(client, workspace_id, "out", RELAXED_SCHEMA)

    resp = await client.post(
        f"/v1/workspaces/{workspace_id}/channels/out/deadletter/replay-all"
        "?max=2"
    )
    body = resp.json()
    assert body["attempted"] == 2
    assert body["succeeded"] == 2

    # 3 rows still in DLQ.
    dlq = (
        await client.get(
            f"/v1/workspaces/{workspace_id}/channels/out/deadletter"
        )
    ).json()["messages"]
    assert len(dlq) == 3


async def test_replay_all_empty_dlq_returns_zeros(
    client: httpx.AsyncClient, workspace_id: str
) -> None:
    """Replay against a never-used DLQ returns a zero envelope, not 404."""

    resp = await client.post(
        f"/v1/workspaces/{workspace_id}/channels/empty/deadletter/replay-all"
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body == {
        "channel": "empty",
        "attempted": 0,
        "succeeded": 0,
        "failed": 0,
        "failures": [],
        "dry_run": False,
    }


# ---------------------------------------------------------------------------
# DELETE /deadletter (purge)


async def test_purge_with_zero_seconds_deletes_all(
    client: httpx.AsyncClient, workspace_id: str
) -> None:
    """``older_than_seconds=0`` clears the DLQ entirely."""

    await _set_schema(client, workspace_id, "out", SIMPLE_SCHEMA)
    await _seed_dlq(client, workspace_id, "out", 4)

    resp = await client.delete(
        f"/v1/workspaces/{workspace_id}/channels/out/deadletter"
        "?older_than_seconds=0"
    )
    assert resp.status_code == 200
    assert resp.json() == {"purged": 4}

    dlq = (
        await client.get(
            f"/v1/workspaces/{workspace_id}/channels/out/deadletter"
        )
    ).json()["messages"]
    assert dlq == []


async def test_purge_keeps_recent_messages(
    client: httpx.AsyncClient, workspace_id: str
) -> None:
    """``older_than_seconds=86400`` keeps freshly-sent rows."""

    await _set_schema(client, workspace_id, "out", SIMPLE_SCHEMA)
    await _seed_dlq(client, workspace_id, "out", 3)

    # 1-day cutoff — none of the just-sent messages are old enough.
    resp = await client.delete(
        f"/v1/workspaces/{workspace_id}/channels/out/deadletter"
        "?older_than_seconds=86400"
    )
    assert resp.status_code == 200
    assert resp.json() == {"purged": 0}

    dlq = (
        await client.get(
            f"/v1/workspaces/{workspace_id}/channels/out/deadletter"
        )
    ).json()["messages"]
    assert len(dlq) == 3


async def test_purge_mixed_recent_and_old(
    client: httpx.AsyncClient, workspace_id: str
) -> None:
    """Backdate some DLQ rows to verify only old ones are purged."""

    from plinth_workspace.db import connect, iso, now_utc
    from datetime import timedelta

    await _set_schema(client, workspace_id, "out", SIMPLE_SCHEMA)
    ids = await _seed_dlq(client, workspace_id, "out", 4)

    # Backdate 2 of the 4 messages by 2 days.
    old_ts = iso(now_utc() - timedelta(days=2))
    settings = client._transport.app.state.settings  # type: ignore[attr-defined]
    async with connect(settings.db_path) as conn:
        for msg_id in ids[:2]:
            await conn.execute(
                "UPDATE channel_messages SET sent_at=? WHERE id=?",
                (old_ts, msg_id),
            )
        await conn.commit()

    resp = await client.delete(
        f"/v1/workspaces/{workspace_id}/channels/out/deadletter"
        "?older_than_seconds=86400"
    )
    assert resp.status_code == 200
    assert resp.json() == {"purged": 2}

    dlq = (
        await client.get(
            f"/v1/workspaces/{workspace_id}/channels/out/deadletter"
        )
    ).json()["messages"]
    assert len(dlq) == 2


async def test_purge_unknown_channel_returns_zero(
    client: httpx.AsyncClient, workspace_id: str
) -> None:
    """Purging a channel that never had a DLQ is a 200 with ``purged=0``."""

    resp = await client.delete(
        f"/v1/workspaces/{workspace_id}/channels/never-typed/deadletter"
        "?older_than_seconds=0"
    )
    assert resp.status_code == 200
    assert resp.json() == {"purged": 0}


# ---------------------------------------------------------------------------
# Tenant / workspace isolation


async def test_check_isolated_per_workspace(
    client: httpx.AsyncClient, workspace_id: str
) -> None:
    """A workspace's check doesn't see another workspace's messages."""

    other_ws = await _make_workspace(client, name="other")
    await _set_schema(client, workspace_id, "out", SIMPLE_SCHEMA)
    for i in range(3):
        await _send(
            client,
            workspace_id,
            "out",
            {"topic": f"t{i}", "sources": ["s"]},
        )

    # The other workspace has no messages.
    resp = await client.post(
        f"/v1/workspaces/{other_ws}/channels/out/schema/check",
        json={"schema": SIMPLE_SCHEMA, "scope": "both"},
    )
    body = resp.json()
    assert body["checked"] == 0
    assert body["valid"] == 0
    assert body["invalid"] == 0


async def test_replay_all_isolated_per_workspace(
    client: httpx.AsyncClient, workspace_id: str
) -> None:
    """Replay on one workspace doesn't drain another workspace's DLQ."""

    other_ws = await _make_workspace(client, name="other")
    await _set_schema(client, workspace_id, "out", SIMPLE_SCHEMA)
    await _seed_dlq(client, workspace_id, "out", 3)

    # Other workspace replay — should be a no-op.
    resp = await client.post(
        f"/v1/workspaces/{other_ws}/channels/out/deadletter/replay-all"
    )
    assert resp.status_code == 200
    assert resp.json()["attempted"] == 0

    # The original workspace's DLQ is unchanged.
    dlq = (
        await client.get(
            f"/v1/workspaces/{workspace_id}/channels/out/deadletter"
        )
    ).json()["messages"]
    assert len(dlq) == 3


async def test_unknown_workspace_returns_404(
    client: httpx.AsyncClient,
) -> None:
    """All three v0.6 endpoints surface WORKSPACE_NOT_FOUND for unknown ws."""

    resp = await client.post(
        "/v1/workspaces/ws_nope/channels/out/schema/check",
        json={"schema": SIMPLE_SCHEMA},
    )
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "WORKSPACE_NOT_FOUND"

    resp = await client.post(
        "/v1/workspaces/ws_nope/channels/out/deadletter/replay-all"
    )
    assert resp.status_code == 404

    resp = await client.delete(
        "/v1/workspaces/ws_nope/channels/out/deadletter?older_than_seconds=0"
    )
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# Smoke test — the end-to-end happy path described in the deliverable.


async def test_set_schema_post_5_invalid_replay_all_smoke(
    client: httpx.AsyncClient, workspace_id: str
) -> None:
    """Set schema, post 5 invalid messages, relax, replay-all → main has 5."""

    await _set_schema(client, workspace_id, "out", SIMPLE_SCHEMA)

    for _ in range(5):
        resp = await _send(client, workspace_id, "out", {"topic": "smoke"})
        assert resp.status_code == 422

    # DLQ has 5.
    dlq = (
        await client.get(
            f"/v1/workspaces/{workspace_id}/channels/out/deadletter"
        )
    ).json()["messages"]
    assert len(dlq) == 5

    # Relax schema.
    await _set_schema(client, workspace_id, "out", RELAXED_SCHEMA)

    # Replay all.
    resp = await client.post(
        f"/v1/workspaces/{workspace_id}/channels/out/deadletter/replay-all"
    )
    body = resp.json()
    assert body["succeeded"] == 5
    assert body["failed"] == 0

    # Main has 5.
    main = (
        await client.get(
            f"/v1/workspaces/{workspace_id}/channels/out/receive"
        )
    ).json()["messages"]
    assert len(main) == 5
