# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 The Plinth Authors
"""Direct tests for ``audit.AuditLog``."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from plinth_gateway.audit import AuditLog, AuditRecord, new_audit_id


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


def test_new_audit_id_format() -> None:
    aid = new_audit_id()
    assert aid.startswith("evt_")
    assert len(aid) == len("evt_") + 26  # ULID = 26 chars


@pytest.mark.asyncio
async def test_record_roundtrip(db) -> None:
    audit = AuditLog(db)
    event = await audit.record(_record())
    assert event.id.startswith("evt_")
    fetched = await audit.query(workspace_id="ws_a")
    assert len(fetched) == 1
    assert fetched[0].id == event.id
    assert fetched[0].tool_id == "web.fetch"


@pytest.mark.asyncio
async def test_query_filters_workspace_and_tool(db) -> None:
    audit = AuditLog(db)
    await audit.record(_record(tool_id="web.fetch", workspace_id="ws_a"))
    await audit.record(_record(tool_id="web.search", workspace_id="ws_a"))
    await audit.record(_record(tool_id="web.fetch", workspace_id="ws_b"))

    e_ws_a = await audit.query(workspace_id="ws_a")
    assert {e.tool_id for e in e_ws_a} == {"web.fetch", "web.search"}

    e_fetch = await audit.query(tool_id="web.fetch")
    assert all(e.tool_id == "web.fetch" for e in e_fetch)

    e_combined = await audit.query(workspace_id="ws_a", tool_id="web.fetch")
    assert len(e_combined) == 1


@pytest.mark.asyncio
async def test_query_limit(db) -> None:
    audit = AuditLog(db)
    for _ in range(5):
        await audit.record(_record())
    rows = await audit.query(limit=2)
    assert len(rows) == 2


@pytest.mark.asyncio
async def test_query_since_filter(db) -> None:
    audit = AuditLog(db)
    await audit.record(_record())
    future = datetime.now(timezone.utc) + timedelta(hours=1)
    rows = await audit.query(since=future)
    assert rows == []
    past = datetime.now(timezone.utc) - timedelta(hours=1)
    rows = await audit.query(since=past)
    assert len(rows) == 1


@pytest.mark.asyncio
async def test_stats_aggregates_correctly(db) -> None:
    audit = AuditLog(db)
    await audit.record(_record(tool_id="web.fetch", cost_estimate_usd=0.001))
    await audit.record(_record(tool_id="web.fetch", cached=True, cost_estimate_usd=0.0))
    await audit.record(
        _record(tool_id="web.search", error="boom", cost_estimate_usd=0.001)
    )

    s = await audit.stats()
    assert s.total_invocations == 3
    assert s.cached_count == 1
    assert s.error_count == 1
    assert pytest.approx(s.total_cost_usd, abs=1e-9) == 0.002
    by_tool = {row.tool_id: row for row in s.by_tool}
    assert by_tool["web.fetch"].count == 2
    assert by_tool["web.search"].count == 1


@pytest.mark.asyncio
async def test_stats_filtered_by_workspace(db) -> None:
    audit = AuditLog(db)
    await audit.record(_record(workspace_id="ws_a"))
    await audit.record(_record(workspace_id="ws_b"))
    s = await audit.stats(workspace_id="ws_a")
    assert s.total_invocations == 1


def test_make_preview_truncates_long_args() -> None:
    long_args = {"x": "y" * 1000}
    preview = AuditLog.make_preview(long_args)
    assert len(preview) == 500


@pytest.mark.asyncio
async def test_query_filter_by_agent(db) -> None:
    audit = AuditLog(db)
    await audit.record(_record(agent_id="ag_1"))
    await audit.record(_record(agent_id="ag_2"))
    rows = await audit.query(agent_id="ag_1")
    assert len(rows) == 1
    assert rows[0].agent_id == "ag_1"


def test_parse_ts_branches() -> None:
    from plinth_gateway import audit as audit_mod
    from datetime import datetime, timezone

    aware = datetime(2026, 1, 1, tzinfo=timezone.utc)
    assert audit_mod._parse_ts(aware) is aware
    naive = datetime(2026, 1, 1)
    assert audit_mod._parse_ts(naive).tzinfo == timezone.utc
