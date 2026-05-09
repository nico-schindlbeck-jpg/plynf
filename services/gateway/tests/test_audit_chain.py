# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 The Plinth Authors
"""Tests for the v1.0 tamper-evident audit hash chain.

Covers:
  * Fresh chain — every newly recorded event extends the chain.
  * verify_chain on a clean chain returns ``verified=True``.
  * In-place tamper of ``arguments_hash`` is detected as
    ``hash_mismatch``.
  * In-place tamper of ``prev_hash`` is detected as
    ``prev_hash_mismatch``.
  * Pre-v1.0 rows (NULL hash columns) don't break verification — the
    chain skips them and verifies the hashed rows separately.
  * The ``GET /v1/audit/verify`` endpoint surfaces the same outcomes.
"""

from __future__ import annotations

import pytest

from plinth_gateway.audit import (
    AuditLog,
    AuditRecord,
    compute_event_hash,
)
from plinth_gateway.cache import canonical_json


def _record(**overrides) -> AuditRecord:
    base = {
        "tool_id": "web.fetch",
        "arguments": {"url": "u"},
        "workspace_id": "ws_a",
        "agent_id": "ag_a",
        "arguments_hash": "h" * 64,
        "arguments_preview": '{"url":"u"}',
        "cached": False,
        "duration_ms": 50,
        "cost_estimate_usd": 0.0005,
        "result_hash": "r" * 64,
        "error": None,
    }
    base.update(overrides)
    return AuditRecord(**base)


@pytest.mark.asyncio
async def test_fresh_event_has_null_prev_first(db) -> None:
    """The very first event has prev_hash=NULL and a populated event_hash."""

    audit = AuditLog(db)
    event = await audit.record(_record())
    row = await db.fetchone(
        "SELECT prev_hash, event_hash FROM audit_events WHERE id = ?",
        (event.id,),
    )
    assert row["prev_hash"] is None
    assert row["event_hash"] is not None
    assert len(row["event_hash"]) == 64


@pytest.mark.asyncio
async def test_subsequent_event_chains_to_previous(db) -> None:
    """Event N's prev_hash equals event N-1's event_hash."""

    audit = AuditLog(db)
    e1 = await audit.record(_record(tool_id="a"))
    e2 = await audit.record(_record(tool_id="b"))
    rows = await db.fetchall(
        "SELECT id, prev_hash, event_hash FROM audit_events ORDER BY id"
    )
    assert len(rows) == 2
    assert rows[0]["id"] == e1.id
    assert rows[1]["id"] == e2.id
    assert rows[1]["prev_hash"] == rows[0]["event_hash"]


@pytest.mark.asyncio
async def test_verify_chain_passes_on_clean_chain(db) -> None:
    audit = AuditLog(db)
    for _ in range(5):
        await audit.record(_record())
    result = await audit.verify_chain()
    assert result.verified is True
    assert result.checked == 5
    assert result.broken_at is None
    assert result.broken_reason is None


@pytest.mark.asyncio
async def test_verify_chain_detects_args_hash_tamper(db) -> None:
    """Mutating ``arguments_hash`` after the fact must fail verification."""

    audit = AuditLog(db)
    e1 = await audit.record(_record())
    await audit.record(_record(tool_id="b"))

    # Tamper: change arguments_hash on the first row.
    await db.execute(
        "UPDATE audit_events SET arguments_hash = ? WHERE id = ?",
        ("0" * 64, e1.id),
    )

    result = await audit.verify_chain()
    assert result.verified is False
    assert result.broken_at == e1.id
    assert result.broken_reason == "hash_mismatch"


@pytest.mark.asyncio
async def test_verify_chain_detects_prev_hash_tamper(db) -> None:
    """Mutating ``prev_hash`` (linkage tamper) breaks verification."""

    audit = AuditLog(db)
    await audit.record(_record())
    e2 = await audit.record(_record(tool_id="b"))

    await db.execute(
        "UPDATE audit_events SET prev_hash = ? WHERE id = ?",
        ("f" * 64, e2.id),
    )

    result = await audit.verify_chain()
    assert result.verified is False
    assert result.broken_at == e2.id
    # Mutating prev_hash also breaks the recomputed event_hash → hash_mismatch
    # (we hash prev_hash || canonical_json). prev_hash_mismatch only fires
    # when prev_hash *would* have been correct but doesn't match the actual
    # previous chained event's event_hash.
    assert result.broken_reason in {"hash_mismatch", "prev_hash_mismatch"}


@pytest.mark.asyncio
async def test_verify_chain_detects_event_hash_tamper(db) -> None:
    audit = AuditLog(db)
    e1 = await audit.record(_record())
    await db.execute(
        "UPDATE audit_events SET event_hash = ? WHERE id = ?",
        ("0" * 64, e1.id),
    )
    result = await audit.verify_chain()
    assert result.verified is False
    assert result.broken_at == e1.id
    assert result.broken_reason == "hash_mismatch"


@pytest.mark.asyncio
async def test_verify_chain_skips_legacy_null_rows(db) -> None:
    """Legacy rows with NULL hash columns don't fail the chain."""

    # Insert a legacy row directly with NULL hash columns.
    await db.execute(
        """
        INSERT INTO audit_events (
            id, timestamp, tool_id, workspace_id, agent_id, tenant_id,
            arguments_hash, arguments_preview, result_hash,
            cached, duration_ms, cost_estimate_usd, error,
            prev_hash, event_hash
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, NULL)
        """,
        (
            "evt_legacy_a",
            "2025-01-01T00:00:00+00:00",
            "legacy.tool",
            None,
            None,
            "default",
            "h" * 64,
            None,
            None,
            0,
            10,
            0.0,
            None,
        ),
    )

    audit = AuditLog(db)
    # Now record some hashed events.
    await audit.record(_record())
    await audit.record(_record(tool_id="b"))

    result = await audit.verify_chain()
    assert result.verified is True
    # Two hashed rows checked; the legacy row was skipped.
    assert result.checked == 2


@pytest.mark.asyncio
async def test_compute_event_hash_is_deterministic() -> None:
    payload = {"id": "x", "tool_id": "t", "tenant_id": "default"}
    a = compute_event_hash("abc", payload)
    b = compute_event_hash("abc", payload)
    assert a == b
    # Different prev_hash → different event_hash.
    c = compute_event_hash("def", payload)
    assert a != c
    # canonical_json sorts keys, so order doesn't matter.
    payload_reordered = {"tenant_id": "default", "tool_id": "t", "id": "x"}
    d = compute_event_hash("abc", payload_reordered)
    assert a == d
    # Sanity: changes in payload propagate.
    e = compute_event_hash("abc", {**payload, "tool_id": "u"})
    assert a != e
    _ = canonical_json  # type: ignore[unused]


# ---------------------------------------------------------------------------
# HTTP endpoint coverage


@pytest.mark.asyncio
async def test_verify_endpoint_returns_verified_on_clean_chain(client) -> None:
    # Seed a tool + a couple of invokes via the audit table directly.
    # Easier than wiring a mock-mcp here.
    resp = await client.get("/v1/audit/verify")
    assert resp.status_code == 200
    body = resp.json()
    assert body["verified"] is True
    assert body["checked"] == 0


@pytest.mark.asyncio
async def test_verify_endpoint_surfaces_tamper(app_and_client) -> None:
    app, client = app_and_client
    db = app.state.db
    audit = AuditLog(db)
    e1 = await audit.record(_record())
    await audit.record(_record(tool_id="b"))

    # Tamper.
    await db.execute(
        "UPDATE audit_events SET arguments_hash = ? WHERE id = ?",
        ("0" * 64, e1.id),
    )

    resp = await client.get("/v1/audit/verify")
    assert resp.status_code == 200
    body = resp.json()
    assert body["verified"] is False
    assert body["broken_at"] == e1.id
    assert body["broken_reason"] == "hash_mismatch"


@pytest.mark.asyncio
async def test_verify_endpoint_accepts_since(app_and_client) -> None:
    """``since`` filter narrows the verification window."""

    app, client = app_and_client
    db = app.state.db
    audit = AuditLog(db)
    await audit.record(_record())
    await audit.record(_record(tool_id="b"))

    # since=0 includes everything.
    resp = await client.get("/v1/audit/verify", params={"since": 0})
    assert resp.status_code == 200
    body = resp.json()
    assert body["verified"] is True

    # Far-future since matches nothing.
    resp = await client.get("/v1/audit/verify", params={"since": 9999999999})
    assert resp.status_code == 200
    body = resp.json()
    assert body["verified"] is True
    assert body["checked"] == 0
