# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 The Plinth Authors
"""Tests for the v1.4 ``GET /v1/audit/cost-by-agent`` endpoint."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from plinth_gateway.audit import AuditLog, AuditRecord


def _record(**overrides) -> AuditRecord:
    base = {
        "tool_id": "web.fetch",
        "arguments": {"url": "u"},
        "workspace_id": "ws_a",
        "agent_id": "ag_a",
        "tenant_id": "default",
        "arguments_hash": "h" * 64,
        "arguments_preview": '{"url":"u"}',
        "cached": False,
        "duration_ms": 50,
        "cost_estimate_usd": 0.001,
        "result_hash": "r" * 64,
        "error": None,
    }
    base.update(overrides)
    return AuditRecord(**base)


# ---------------------------------------------------------------------------
# Direct AuditLog.cost_by_agent unit tests


@pytest.mark.asyncio
async def test_cost_by_agent_empty_returns_empty(db) -> None:
    audit = AuditLog(db)
    since = datetime.now(timezone.utc) - timedelta(hours=1)
    agents, total_agents, total_cost = await audit.cost_by_agent(since=since)
    assert agents == []
    assert total_agents == 0
    assert total_cost == 0.0


@pytest.mark.asyncio
async def test_cost_by_agent_sorts_desc_by_cost(db) -> None:
    audit = AuditLog(db)
    # ag_b spends more than ag_a
    await audit.record(_record(agent_id="ag_a", cost_estimate_usd=0.01))
    await audit.record(_record(agent_id="ag_b", cost_estimate_usd=0.05))
    await audit.record(_record(agent_id="ag_b", cost_estimate_usd=0.02))
    since = datetime.now(timezone.utc) - timedelta(hours=1)
    agents, total_agents, total_cost = await audit.cost_by_agent(since=since)
    assert [a.agent_id for a in agents] == ["ag_b", "ag_a"]
    assert agents[0].total_cost_usd == pytest.approx(0.07, rel=1e-9)
    assert total_agents == 2
    assert total_cost == pytest.approx(0.08, rel=1e-9)


@pytest.mark.asyncio
async def test_cost_by_agent_tenant_filter(db) -> None:
    audit = AuditLog(db)
    await audit.record(_record(agent_id="ag_a", tenant_id="t1", cost_estimate_usd=0.01))
    await audit.record(_record(agent_id="ag_b", tenant_id="t2", cost_estimate_usd=0.05))
    since = datetime.now(timezone.utc) - timedelta(hours=1)
    agents, _, _ = await audit.cost_by_agent(since=since, tenant_id="t1")
    assert len(agents) == 1
    assert agents[0].agent_id == "ag_a"
    assert agents[0].tenant_id == "t1"


@pytest.mark.asyncio
async def test_cost_by_agent_top_n_respected(db) -> None:
    audit = AuditLog(db)
    for i in range(5):
        await audit.record(
            _record(agent_id=f"ag_{i}", cost_estimate_usd=0.001 * (i + 1))
        )
    since = datetime.now(timezone.utc) - timedelta(hours=1)
    agents, total_agents, _ = await audit.cost_by_agent(since=since, top=2)
    assert len(agents) == 2
    assert agents[0].agent_id == "ag_4"
    assert agents[1].agent_id == "ag_3"
    assert total_agents == 5  # unfiltered count, regardless of top=N


@pytest.mark.asyncio
async def test_cost_by_agent_unknown_bucket(db) -> None:
    audit = AuditLog(db)
    await audit.record(_record(agent_id=None, cost_estimate_usd=0.02))
    await audit.record(_record(agent_id=None, cost_estimate_usd=0.01))
    await audit.record(_record(agent_id="ag_a", cost_estimate_usd=0.005))
    since = datetime.now(timezone.utc) - timedelta(hours=1)
    agents, _, _ = await audit.cost_by_agent(since=since)
    by_id = {a.agent_id: a for a in agents}
    assert "(unknown)" in by_id
    assert by_id["(unknown)"].invocations == 2
    assert by_id["(unknown)"].total_cost_usd == pytest.approx(0.03, rel=1e-9)


@pytest.mark.asyncio
async def test_cost_by_agent_cached_counted_separately(db) -> None:
    audit = AuditLog(db)
    await audit.record(_record(agent_id="ag_a", cached=False))
    await audit.record(_record(agent_id="ag_a", cached=True))
    await audit.record(_record(agent_id="ag_a", cached=True))
    since = datetime.now(timezone.utc) - timedelta(hours=1)
    agents, _, _ = await audit.cost_by_agent(since=since)
    assert len(agents) == 1
    assert agents[0].invocations == 3
    assert agents[0].cached_invocations == 2


@pytest.mark.asyncio
async def test_cost_by_agent_top_tools_populated(db) -> None:
    audit = AuditLog(db)
    await audit.record(_record(agent_id="ag_a", tool_id="web.fetch", cost_estimate_usd=0.05))
    await audit.record(_record(agent_id="ag_a", tool_id="web.fetch", cost_estimate_usd=0.05))
    await audit.record(_record(agent_id="ag_a", tool_id="web.search", cost_estimate_usd=0.02))
    await audit.record(_record(agent_id="ag_a", tool_id="notes.add", cost_estimate_usd=0.001))
    since = datetime.now(timezone.utc) - timedelta(hours=1)
    agents, _, _ = await audit.cost_by_agent(since=since)
    assert len(agents) == 1
    tools = agents[0].top_tools
    assert tools[0].tool_id == "web.fetch"
    assert tools[0].invocations == 2
    assert tools[0].cost_usd == pytest.approx(0.10, rel=1e-9)
    assert tools[1].tool_id == "web.search"
    assert tools[2].tool_id == "notes.add"


@pytest.mark.asyncio
async def test_cost_by_agent_avg_duration(db) -> None:
    audit = AuditLog(db)
    await audit.record(_record(agent_id="ag_a", duration_ms=100))
    await audit.record(_record(agent_id="ag_a", duration_ms=200))
    await audit.record(_record(agent_id="ag_a", duration_ms=300))
    since = datetime.now(timezone.utc) - timedelta(hours=1)
    agents, _, _ = await audit.cost_by_agent(since=since)
    assert len(agents) == 1
    assert agents[0].avg_duration_ms == pytest.approx(200.0, rel=1e-9)


@pytest.mark.asyncio
async def test_cost_by_agent_window_excludes_old_rows(db) -> None:
    audit = AuditLog(db)
    await audit.record(_record(agent_id="ag_a", cost_estimate_usd=0.01))
    # The since cutoff is in the future — nothing is in-window.
    since = datetime.now(timezone.utc) + timedelta(hours=1)
    agents, total_agents, total_cost = await audit.cost_by_agent(since=since)
    assert agents == []
    assert total_agents == 0
    assert total_cost == 0.0


# ---------------------------------------------------------------------------
# HTTP endpoint tests — exercise window parsing + auth-mode interactions


@pytest.mark.asyncio
async def test_endpoint_window_parsing_24h(client) -> None:
    r = await client.get("/v1/audit/cost-by-agent?window=24h&top=5")
    assert r.status_code == 200
    body = r.json()
    assert body["window"] == "24h"
    assert body["agents"] == []
    assert body["total_agents"] == 0
    assert body["total_cost_usd"] == 0.0


@pytest.mark.asyncio
async def test_endpoint_window_parsing_invalid(client) -> None:
    r = await client.get("/v1/audit/cost-by-agent?window=banana")
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "INVALID_ARGUMENTS"


@pytest.mark.asyncio
@pytest.mark.parametrize("window", ["1h", "24h", "7d", "30d", "30m"])
async def test_endpoint_accepts_each_supported_window(client, window) -> None:
    r = await client.get(f"/v1/audit/cost-by-agent?window={window}")
    assert r.status_code == 200, r.text
    assert r.json()["window"] == window


@pytest.mark.asyncio
async def test_endpoint_returns_aggregated_rows(app_and_client) -> None:
    """End-to-end: insert via AuditLog, fetch via API, verify rollup."""
    app, client = app_and_client
    audit_log: AuditLog = app.state.audit
    await audit_log.record(_record(agent_id="ag_x", cost_estimate_usd=0.04))
    await audit_log.record(_record(agent_id="ag_y", cost_estimate_usd=0.02))
    r = await client.get("/v1/audit/cost-by-agent?window=1h&top=10")
    assert r.status_code == 200
    body = r.json()
    assert body["total_agents"] == 2
    assert body["total_cost_usd"] == pytest.approx(0.06, rel=1e-9)
    ids = [a["agent_id"] for a in body["agents"]]
    assert ids == ["ag_x", "ag_y"]
