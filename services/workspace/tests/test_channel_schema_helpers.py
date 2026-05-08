# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 The Plinth Authors
"""End-to-end tests for v0.6 channel schema migration helpers.

Exercises three additive endpoints layered on top of the v0.5 typed-channel
machinery:

* ``POST .../channels/{name}/schema/check`` — dry-run validate existing
  rows against a candidate schema.
* ``POST .../channels/{name}/deadletter/replay-all`` — bulk replay DLQ
  through the *currently attached* schema, with optional ``dry_run``.
* ``DELETE .../channels/{name}/deadletter`` — purge DLQ rows older than
  ``older_than_seconds`` (``0`` clears everything).

The fixtures below build a small chain of "send some valid + some invalid
messages" before each test to keep individual cases focused on the helper
behaviour rather than the setup cost.
"""

from __future__ import annotations

import asyncio
from typing import Any

import httpx


# ---------------------------------------------------------------------------
# Helpers / fixtures (local — keep this file self-contained)
# ---------------------------------------------------------------------------


# A strict "report" schema we can both relax and tighten across tests.
STRICT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["topic", "sources"],
    "properties": {
        "topic": {"type": "string"},
        "sources": {"type": "array", "items": {"type": "string"}},
    },
    "additionalProperties": False,
}


# Loose variant that accepts everything that's an object — used for "schema
# was relaxed, replay should now succeed" cases.
LOOSE_SCHEMA: dict[str, Any] = {"type": "object"}


async def _set_schema(
    client: httpx.AsyncClient, ws: str, ch: str, schema: dict[str, Any]
) -> None:
    resp = await client.post(
        f"/v1/workspaces/{ws}/channels/{ch}/schema",
        json={"schema": schema},
    )
    assert resp.status_code == 200, resp.text


async def _send_valid(
    client: httpx.AsyncClient, ws: str, ch: str, n: int = 1
) -> None:
    """Send ``n`` ``STRICT_SCHEMA``-valid payloads; assert each is 201."""
    for i in range(n):
        resp = await client.post(
            f"/v1/workspaces/{ws}/channels/{ch}/send",
            json={"payload": {"topic": f"t-{i}", "sources": [f"s{i}"]}},
        )
        assert resp.status_code == 201, resp.text


async def _send_invalid(
    client: httpx.AsyncClient, ws: str, ch: str, n: int = 1
) -> None:
    """Send ``n`` payloads that fail ``STRICT_SCHEMA`` (missing ``sources``)."""
    for i in range(n):
        resp = await client.post(
            f"/v1/workspaces/{ws}/channels/{ch}/send",
            json={"payload": {"topic": f"bad-{i}"}},
        )
        assert resp.status_code == 422, resp.text


# ---------------------------------------------------------------------------
# /schema/check
# ---------------------------------------------------------------------------


async def test_check_main_all_valid(
    client: httpx.AsyncClient, workspace_id: str
) -> None:
    """check(scope=main) with 5 valid messages reports valid=5, invalid=0."""
    await _set_schema(client, workspace_id, "out", STRICT_SCHEMA)
    await _send_valid(client, workspace_id, "out", 5)

    resp = await client.post(
        f"/v1/workspaces/{workspace_id}/channels/out/schema/check",
        json={"schema": STRICT_SCHEMA, "scope": "main"},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["channel"] == "out"
    assert body["scope"] == "main"
    assert body["checked"] == 5
    assert body["valid"] == 5
    assert body["invalid"] == 0
    assert body["sample_failures"] == []


async def test_check_dlq_only_collects_failures(
    client: httpx.AsyncClient, workspace_id: str
) -> None:
    """scope=deadletter sees only the rows that failed the live schema."""
    await _set_schema(client, workspace_id, "out", STRICT_SCHEMA)
    await _send_invalid(client, workspace_id, "out", 3)

    resp = await client.post(
        f"/v1/workspaces/{workspace_id}/channels/out/schema/check",
        json={"schema": STRICT_SCHEMA, "scope": "deadletter"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["scope"] == "deadletter"
    assert body["checked"] == 3
    assert body["valid"] == 0
    assert body["invalid"] == 3
    # First-N sampling: every failure here fits inside the 10-cap.
    assert len(body["sample_failures"]) == 3
    sample = body["sample_failures"][0]
    assert sample["msg_id"].startswith("msg_")
    assert isinstance(sample["errors"], list)
    assert sample["errors"][0]["message"]


async def test_check_both_scope_combines(
    client: httpx.AsyncClient, workspace_id: str
) -> None:
    """scope=both interleaves main + DLQ counts under one envelope."""
    await _set_schema(client, workspace_id, "out", STRICT_SCHEMA)
    await _send_valid(client, workspace_id, "out", 2)
    await _send_invalid(client, workspace_id, "out", 2)

    resp = await client.post(
        f"/v1/workspaces/{workspace_id}/channels/out/schema/check",
        json={"schema": STRICT_SCHEMA, "scope": "both"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["checked"] == 4
    assert body["valid"] == 2
    assert body["invalid"] == 2


async def test_check_relaxed_candidate_makes_invalid_valid(
    client: httpx.AsyncClient, workspace_id: str
) -> None:
    """A loosened candidate should turn DLQ failures into valid rows.

    This is the real migration use-case: "if I relax the schema like this,
    will my DLQ drain cleanly?"
    """
    await _set_schema(client, workspace_id, "out", STRICT_SCHEMA)
    await _send_invalid(client, workspace_id, "out", 4)

    resp = await client.post(
        f"/v1/workspaces/{workspace_id}/channels/out/schema/check",
        json={"schema": LOOSE_SCHEMA, "scope": "deadletter"},
    )
    body = resp.json()
    assert body["valid"] == 4
    assert body["invalid"] == 0


async def test_check_does_not_mutate(
    client: httpx.AsyncClient, workspace_id: str
) -> None:
    """check() is read-only — main + DLQ counts stay put."""
    await _set_schema(client, workspace_id, "out", STRICT_SCHEMA)
    await _send_valid(client, workspace_id, "out", 1)
    await _send_invalid(client, workspace_id, "out", 1)

    # Run a check that would "succeed" everywhere.
    resp = await client.post(
        f"/v1/workspaces/{workspace_id}/channels/out/schema/check",
        json={"schema": LOOSE_SCHEMA, "scope": "both"},
    )
    assert resp.status_code == 200

    # The DLQ row stays put.
    dlq = (
        await client.get(
            f"/v1/workspaces/{workspace_id}/channels/out/deadletter"
        )
    ).json()["messages"]
    assert len(dlq) == 1


async def test_check_limit_caps_iteration(
    client: httpx.AsyncClient, workspace_id: str
) -> None:
    """``limit`` bounds how many rows are scanned across the chosen scope."""
    await _set_schema(client, workspace_id, "out", STRICT_SCHEMA)
    await _send_valid(client, workspace_id, "out", 10)

    resp = await client.post(
        f"/v1/workspaces/{workspace_id}/channels/out/schema/check",
        json={"schema": STRICT_SCHEMA, "scope": "main", "limit": 4},
    )
    body = resp.json()
    assert body["checked"] == 4


async def test_check_channel_without_schema_treats_all_as_valid(
    client: httpx.AsyncClient, workspace_id: str
) -> None:
    """Untyped channels: validate against the *candidate* alone.

    The endpoint isn't gated on having a current schema — it's the very
    helper you'd reach for *before* attaching one.
    """
    # No set_schema call. Send raw messages.
    for payload in [{"a": 1}, {"a": 2}]:
        resp = await client.post(
            f"/v1/workspaces/{workspace_id}/channels/raw/send",
            json={"payload": payload},
        )
        assert resp.status_code == 201

    resp = await client.post(
        f"/v1/workspaces/{workspace_id}/channels/raw/schema/check",
        json={"schema": {"type": "object", "required": ["a"]}, "scope": "main"},
    )
    body = resp.json()
    assert body["checked"] == 2
    assert body["valid"] == 2


async def test_check_invalid_candidate_schema_returns_400(
    client: httpx.AsyncClient, workspace_id: str
) -> None:
    """A malformed candidate is rejected before any rows are scanned."""
    resp = await client.post(
        f"/v1/workspaces/{workspace_id}/channels/x/schema/check",
        json={"schema": {"type": "not-a-real-type"}, "scope": "main"},
    )
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "INVALID_ARGUMENTS"


async def test_check_workspace_404(client: httpx.AsyncClient) -> None:
    resp = await client.post(
        "/v1/workspaces/ws_nope/channels/x/schema/check",
        json={"schema": LOOSE_SCHEMA},
    )
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "WORKSPACE_NOT_FOUND"


# ---------------------------------------------------------------------------
# /deadletter/replay-all
# ---------------------------------------------------------------------------


async def test_replay_all_dry_run_does_not_mutate(
    client: httpx.AsyncClient, workspace_id: str
) -> None:
    """dry_run reports what *would* happen but leaves the DLQ untouched."""
    await _set_schema(client, workspace_id, "out", STRICT_SCHEMA)
    await _send_invalid(client, workspace_id, "out", 3)
    # Now relax the schema — every DLQ row would now pass.
    await _set_schema(client, workspace_id, "out", LOOSE_SCHEMA)

    resp = await client.post(
        f"/v1/workspaces/{workspace_id}/channels/out/deadletter/replay-all",
        json={"dry_run": True},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["channel"] == "out"
    assert body["dry_run"] is True
    assert body["attempted"] == 3
    assert body["succeeded"] == 3
    assert body["failed"] == 0

    # DLQ untouched.
    dlq = (
        await client.get(
            f"/v1/workspaces/{workspace_id}/channels/out/deadletter"
        )
    ).json()["messages"]
    assert len(dlq) == 3


async def test_replay_all_moves_valid_rows_to_main(
    client: httpx.AsyncClient, workspace_id: str
) -> None:
    """When the schema relaxes, replay-all drains the DLQ to main."""
    await _set_schema(client, workspace_id, "out", STRICT_SCHEMA)
    await _send_invalid(client, workspace_id, "out", 4)
    await _set_schema(client, workspace_id, "out", LOOSE_SCHEMA)

    resp = await client.post(
        f"/v1/workspaces/{workspace_id}/channels/out/deadletter/replay-all",
        json={"dry_run": False},
    )
    body = resp.json()
    assert body["attempted"] == 4
    assert body["succeeded"] == 4
    assert body["failed"] == 0

    # DLQ now empty …
    dlq = (
        await client.get(
            f"/v1/workspaces/{workspace_id}/channels/out/deadletter"
        )
    ).json()["messages"]
    assert dlq == []

    # … and the main channel now holds the replayed rows.
    main = (
        await client.get(
            f"/v1/workspaces/{workspace_id}/channels/out/receive?limit=100"
        )
    ).json()["messages"]
    assert len(main) == 4


async def test_replay_all_keeps_invalid_in_dlq(
    client: httpx.AsyncClient, workspace_id: str
) -> None:
    """Messages that *still* fail the current schema stay in the DLQ.

    We exercise the half-and-half path: write 3 invalid rows under
    ``STRICT_SCHEMA``; relax to a partially-tighter schema where only some
    of the DLQ entries pass.
    """
    await _set_schema(client, workspace_id, "out", STRICT_SCHEMA)

    # Send rows that vary in shape so the new schema passes some / fails some.
    payloads = [
        {"topic": "ok"},  # missing sources -> fails STRICT, passes "object"
        {"topic": "ok2"},
        {"topic": 7},  # int topic -> fails ANY schema with type=string
    ]
    for p in payloads:
        resp = await client.post(
            f"/v1/workspaces/{workspace_id}/channels/out/send",
            json={"payload": p},
        )
        assert resp.status_code == 422, resp.text

    # Relax to "topic must be string"; rows 0,1 pass, row 2 still fails.
    half_schema = {"type": "object", "properties": {"topic": {"type": "string"}}, "required": ["topic"]}
    await _set_schema(client, workspace_id, "out", half_schema)

    resp = await client.post(
        f"/v1/workspaces/{workspace_id}/channels/out/deadletter/replay-all",
        json={},
    )
    body = resp.json()
    assert body["attempted"] == 3
    assert body["succeeded"] == 2
    assert body["failed"] == 1
    assert len(body["failures"]) == 1
    failure = body["failures"][0]
    assert failure["msg_id"].startswith("msg_")
    assert "reason" in failure

    # The remaining message is still in the DLQ.
    dlq = (
        await client.get(
            f"/v1/workspaces/{workspace_id}/channels/out/deadletter"
        )
    ).json()["messages"]
    assert len(dlq) == 1


async def test_replay_all_max_caps_attempts(
    client: httpx.AsyncClient, workspace_id: str
) -> None:
    """``max=N`` stops after N candidates even with more DLQ rows pending."""
    await _set_schema(client, workspace_id, "out", STRICT_SCHEMA)
    await _send_invalid(client, workspace_id, "out", 5)
    await _set_schema(client, workspace_id, "out", LOOSE_SCHEMA)

    # Use the query-string form (exercises the FastAPI ``Query(alias="max")``).
    resp = await client.post(
        f"/v1/workspaces/{workspace_id}/channels/out/deadletter/replay-all?max=2",
    )
    body = resp.json()
    assert body["attempted"] == 2
    assert body["succeeded"] == 2

    # The remaining 3 DLQ rows are still around.
    dlq = (
        await client.get(
            f"/v1/workspaces/{workspace_id}/channels/out/deadletter"
        )
    ).json()["messages"]
    assert len(dlq) == 3


async def test_replay_all_empty_dlq_returns_zeroed_envelope(
    client: httpx.AsyncClient, workspace_id: str
) -> None:
    """Calling replay-all on a clean DLQ is fine — counts are all zero."""
    await _set_schema(client, workspace_id, "out", STRICT_SCHEMA)

    resp = await client.post(
        f"/v1/workspaces/{workspace_id}/channels/out/deadletter/replay-all",
        json={},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["attempted"] == 0
    assert body["succeeded"] == 0
    assert body["failed"] == 0
    assert body["failures"] == []


async def test_replay_all_strips_synthetic_headers(
    client: httpx.AsyncClient, workspace_id: str
) -> None:
    """Replayed messages on the main channel must not carry the DLQ headers."""
    await _set_schema(client, workspace_id, "out", STRICT_SCHEMA)
    # Original send carries a custom header that should survive replay.
    resp = await client.post(
        f"/v1/workspaces/{workspace_id}/channels/out/send",
        json={"payload": {"topic": "x"}, "headers": {"trace": "preserved"}},
    )
    assert resp.status_code == 422

    await _set_schema(client, workspace_id, "out", LOOSE_SCHEMA)
    resp = await client.post(
        f"/v1/workspaces/{workspace_id}/channels/out/deadletter/replay-all",
        json={},
    )
    body = resp.json()
    assert body["succeeded"] == 1

    main = (
        await client.get(
            f"/v1/workspaces/{workspace_id}/channels/out/receive?limit=100"
        )
    ).json()["messages"]
    assert len(main) == 1
    headers = main[0]["headers"] or {}
    # Custom user header preserved …
    assert headers.get("trace") == "preserved"
    # … synthetic DLQ headers stripped.
    assert "x-original-channel" not in headers
    assert "x-validation-errors" not in headers
    assert "x-failed-at" not in headers
    assert "x-schema-version" not in headers


# ---------------------------------------------------------------------------
# DELETE /deadletter (purge)
# ---------------------------------------------------------------------------


async def test_purge_with_zero_clears_everything(
    client: httpx.AsyncClient, workspace_id: str
) -> None:
    """``older_than_seconds=0`` is the operator-reset path: nuke the DLQ.

    Use with caution — there is no recovery once a DLQ row is dropped.
    """
    await _set_schema(client, workspace_id, "out", STRICT_SCHEMA)
    await _send_invalid(client, workspace_id, "out", 3)

    resp = await client.delete(
        f"/v1/workspaces/{workspace_id}/channels/out/deadletter?older_than_seconds=0"
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["purged"] == 3

    dlq = (
        await client.get(
            f"/v1/workspaces/{workspace_id}/channels/out/deadletter"
        )
    ).json()["messages"]
    assert dlq == []


async def test_purge_with_long_age_keeps_recent_rows(
    client: httpx.AsyncClient, workspace_id: str
) -> None:
    """A 24h cutoff leaves just-now DLQ rows alone."""
    await _set_schema(client, workspace_id, "out", STRICT_SCHEMA)
    await _send_invalid(client, workspace_id, "out", 2)

    resp = await client.delete(
        f"/v1/workspaces/{workspace_id}/channels/out/deadletter?older_than_seconds=86400"
    )
    assert resp.status_code == 200
    assert resp.json()["purged"] == 0

    dlq = (
        await client.get(
            f"/v1/workspaces/{workspace_id}/channels/out/deadletter"
        )
    ).json()["messages"]
    assert len(dlq) == 2


async def test_purge_empty_dlq_is_zero(
    client: httpx.AsyncClient, workspace_id: str
) -> None:
    """Channels that never had a DLQ row return purged=0 (idempotent)."""
    resp = await client.delete(
        f"/v1/workspaces/{workspace_id}/channels/never-typed/deadletter?older_than_seconds=0"
    )
    assert resp.status_code == 200
    assert resp.json()["purged"] == 0


async def test_purge_negative_age_is_400(
    client: httpx.AsyncClient, workspace_id: str
) -> None:
    """A negative cutoff is a 400, not a "purge nothing" silent success."""
    resp = await client.delete(
        f"/v1/workspaces/{workspace_id}/channels/x/deadletter?older_than_seconds=-1"
    )
    # FastAPI's ``Query(ge=0)`` returns its own 422 envelope; either is fine
    # because the client cannot have meant a "valid" negative.
    assert resp.status_code in {400, 422}


async def test_purge_then_replay_all_is_noop(
    client: httpx.AsyncClient, workspace_id: str
) -> None:
    """Purge -> replay-all on a now-empty DLQ returns zeroed counts.

    Documents the operator-reset workflow: clear the DLQ, then replay
    once more (idempotent), then attach the new schema.
    """
    await _set_schema(client, workspace_id, "out", STRICT_SCHEMA)
    await _send_invalid(client, workspace_id, "out", 2)

    purge = await client.delete(
        f"/v1/workspaces/{workspace_id}/channels/out/deadletter?older_than_seconds=0"
    )
    assert purge.json()["purged"] == 2

    replay = await client.post(
        f"/v1/workspaces/{workspace_id}/channels/out/deadletter/replay-all",
        json={},
    )
    assert replay.json()["attempted"] == 0


# ---------------------------------------------------------------------------
# Larger DLQ, exercises pagination cap
# ---------------------------------------------------------------------------


async def test_check_handles_large_main_channel(
    client: httpx.AsyncClient, workspace_id: str
) -> None:
    """Stress: send 50 messages and verify the default 1000 limit covers."""
    await _set_schema(client, workspace_id, "out", STRICT_SCHEMA)
    await _send_valid(client, workspace_id, "out", 50)

    resp = await client.post(
        f"/v1/workspaces/{workspace_id}/channels/out/schema/check",
        json={"schema": STRICT_SCHEMA, "scope": "main"},
    )
    body = resp.json()
    assert body["checked"] == 50
    assert body["valid"] == 50


async def test_replay_all_dry_run_then_actual_match_counts(
    client: httpx.AsyncClient, workspace_id: str
) -> None:
    """A dry-run preview should agree with the subsequent live run.

    Useful safety property — operators rely on the dry-run counts to
    decide whether to commit.
    """
    await _set_schema(client, workspace_id, "out", STRICT_SCHEMA)
    await _send_invalid(client, workspace_id, "out", 4)
    await _set_schema(client, workspace_id, "out", LOOSE_SCHEMA)

    dry = (
        await client.post(
            f"/v1/workspaces/{workspace_id}/channels/out/deadletter/replay-all",
            json={"dry_run": True},
        )
    ).json()
    actual = (
        await client.post(
            f"/v1/workspaces/{workspace_id}/channels/out/deadletter/replay-all",
            json={"dry_run": False},
        )
    ).json()

    assert dry["succeeded"] == actual["succeeded"] == 4
    assert dry["failed"] == actual["failed"] == 0


async def test_purge_older_than_threshold_with_sleep(
    client: httpx.AsyncClient, workspace_id: str
) -> None:
    """A 1-second cutoff after a brief sleep removes only the earlier batch.

    Intentionally tiny ``asyncio.sleep`` to cross the threshold without
    relying on real time. The second send must land *after* the threshold
    so its row stays put.
    """
    await _set_schema(client, workspace_id, "out", STRICT_SCHEMA)
    await _send_invalid(client, workspace_id, "out", 1)
    # Sleep more than the cutoff so the first row is "older than 1 second".
    await asyncio.sleep(1.1)
    # Tighten the cutoff: only the 1.1s-old row should match.
    resp = await client.delete(
        f"/v1/workspaces/{workspace_id}/channels/out/deadletter?older_than_seconds=1"
    )
    assert resp.status_code == 200
    assert resp.json()["purged"] == 1

    # Second send to leave one fresh row …
    await _send_invalid(client, workspace_id, "out", 1)
    # … which a 1s cutoff a moment later still won't match.
    resp = await client.delete(
        f"/v1/workspaces/{workspace_id}/channels/out/deadletter?older_than_seconds=1"
    )
    assert resp.status_code == 200
    assert resp.json()["purged"] == 0
