# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 The Plinth Authors
"""Tests for the rolling-window cost tracker (``plinth_gateway.cost_caps``).

Layered:

1. ``cost_used_in_window`` — sums correctly, ignores out-of-window events,
   ignores cached events.
2. ``CostCapTracker.check`` — returns the violated window or ``None``.
3. ``/v1/invoke`` integration — the spec's mandatory cases:
   - cost cap exceeded → 429
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
import pytest_asyncio
import respx
from httpx import ASGITransport, AsyncClient, Response

from plinth_gateway.api import create_app
from plinth_gateway.audit import AuditLog
from plinth_gateway.cost_caps import (
    CostCapTracker,
    calls_in_window_seconds,
    cost_used_in_window,
)
from plinth_gateway.db import Database
from plinth_gateway.settings import Settings

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def db(tmp_path: Path):
    database = Database(tmp_path / "cc.db")
    await database.connect()
    try:
        yield database
    finally:
        await database.close()


@pytest_asyncio.fixture
async def audit(db):
    return AuditLog(db)


# ---------------------------------------------------------------------------
# Helper: insert an audit row at a specific timestamp
# ---------------------------------------------------------------------------


# Module-level counter so duplicate-arg inserts inside a single test still get
# unique primary keys (the audit_events PK is the event id).
_EVENT_COUNTER = 0


async def _insert_event(
    db: Database,
    *,
    agent_id: str | None,
    cost: float,
    cached: bool = False,
    when: datetime | None = None,
    tool_id: str = "web.fetch",
) -> str:
    """Insert a synthetic audit row at ``when`` (defaults to now). Returns id."""
    global _EVENT_COUNTER
    _EVENT_COUNTER += 1
    when = when or datetime.now(timezone.utc)
    eid = f"evt_test_{_EVENT_COUNTER}"
    await db.execute(
        """
        INSERT INTO audit_events
        (id, timestamp, tool_id, workspace_id, agent_id,
         arguments_hash, arguments_preview, result_hash,
         cached, duration_ms, cost_estimate_usd, error)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            eid,
            when.isoformat(),
            tool_id,
            None,  # workspace_id
            agent_id,
            "h" * 64,
            "{}",
            None,
            1 if cached else 0,
            10,
            cost,
            None,
        ),
    )
    return eid


# ---------------------------------------------------------------------------
# cost_used_in_window
# ---------------------------------------------------------------------------


class TestCostUsedInWindow:
    @pytest.mark.asyncio
    async def test_returns_sum_in_window(self, db: Database) -> None:
        """Spec mandatory: returns sum, ignores out-of-window."""
        # Two recent events, one ancient.
        await _insert_event(db, agent_id="agt_a", cost=0.40)
        await _insert_event(db, agent_id="agt_a", cost=0.30)
        old = datetime.now(timezone.utc) - timedelta(hours=2)
        await _insert_event(db, agent_id="agt_a", cost=99.0, when=old)

        # 1h window — only the two recent events counted.
        used = await cost_used_in_window(db, "agt_a", 1)
        assert pytest.approx(used) == 0.70

        # 24h window — all three counted.
        used = await cost_used_in_window(db, "agt_a", 24)
        assert pytest.approx(used) == 99.0 + 0.70

    @pytest.mark.asyncio
    async def test_returns_zero_for_unknown_agent(self, db: Database) -> None:
        await _insert_event(db, agent_id="agt_a", cost=1.0)
        assert await cost_used_in_window(db, "agt_other", 1) == 0.0

    @pytest.mark.asyncio
    async def test_excludes_cached_events_by_default(self, db: Database) -> None:
        """Cached calls cost $0 — make sure they don't affect the sum even if
        someone retroactively assigns them a non-zero cost."""
        await _insert_event(db, agent_id="agt_a", cost=0.50, cached=False)
        await _insert_event(db, agent_id="agt_a", cost=0.99, cached=True)
        used = await cost_used_in_window(db, "agt_a", 1)
        assert pytest.approx(used) == 0.50

    @pytest.mark.asyncio
    async def test_include_cached_optional(self, db: Database) -> None:
        await _insert_event(db, agent_id="agt_a", cost=0.50, cached=False)
        await _insert_event(db, agent_id="agt_a", cost=0.99, cached=True)
        used = await cost_used_in_window(db, "agt_a", 1, include_cached=True)
        assert pytest.approx(used) == 1.49

    @pytest.mark.asyncio
    async def test_per_agent_isolation(self, db: Database) -> None:
        """Spec: agent A spend doesn't show up in agent B's window."""
        for _ in range(3):
            await _insert_event(db, agent_id="agt_a", cost=0.5)
        assert await cost_used_in_window(db, "agt_b", 1) == 0.0
        assert pytest.approx(await cost_used_in_window(db, "agt_a", 1)) == 1.5

    @pytest.mark.asyncio
    async def test_window_boundary(self, db: Database) -> None:
        """An event right on the boundary is included; one millisecond past is not."""
        # Far enough past the window edge to survive clock-skew at the millisecond
        # level (insert + read travel through SQLite which truncates fractional
        # microseconds on some platforms).
        just_outside = datetime.now(timezone.utc) - timedelta(hours=1, seconds=2)
        just_inside = datetime.now(timezone.utc) - timedelta(minutes=59)
        await _insert_event(db, agent_id="agt_a", cost=1.0, when=just_outside)
        await _insert_event(db, agent_id="agt_a", cost=2.0, when=just_inside)
        used = await cost_used_in_window(db, "agt_a", 1)
        assert pytest.approx(used) == 2.0

    @pytest.mark.asyncio
    async def test_zero_window_returns_zero(self, db: Database) -> None:
        await _insert_event(db, agent_id="agt_a", cost=1.0)
        assert await cost_used_in_window(db, "agt_a", 0) == 0.0
        assert await cost_used_in_window(db, "agt_a", -1) == 0.0

    @pytest.mark.asyncio
    async def test_empty_table_returns_zero(self, db: Database) -> None:
        assert await cost_used_in_window(db, "agt_a", 1) == 0.0


# ---------------------------------------------------------------------------
# calls_in_window_seconds
# ---------------------------------------------------------------------------


class TestCallsInWindow:
    @pytest.mark.asyncio
    async def test_counts_recent_calls(self, db: Database) -> None:
        for _ in range(5):
            await _insert_event(db, agent_id="agt_a", cost=0.0)
        assert await calls_in_window_seconds(db, "agt_a", 60) == 5

    @pytest.mark.asyncio
    async def test_ignores_old_calls(self, db: Database) -> None:
        # Three recent + two old.
        for _ in range(3):
            await _insert_event(db, agent_id="agt_a", cost=0.0)
        old = datetime.now(timezone.utc) - timedelta(seconds=120)
        for _ in range(2):
            await _insert_event(db, agent_id="agt_a", cost=0.0, when=old)
        assert await calls_in_window_seconds(db, "agt_a", 60) == 3

    @pytest.mark.asyncio
    async def test_zero_or_negative_window(self, db: Database) -> None:
        await _insert_event(db, agent_id="agt_a", cost=0.0)
        assert await calls_in_window_seconds(db, "agt_a", 0) == 0
        assert await calls_in_window_seconds(db, "agt_a", -10) == 0


# ---------------------------------------------------------------------------
# CostCapTracker
# ---------------------------------------------------------------------------


class TestCostCapTracker:
    @pytest.mark.asyncio
    async def test_within_caps_returns_none(self, db: Database) -> None:
        await _insert_event(db, agent_id="agt_a", cost=0.10)
        t = CostCapTracker(db, hour_cap_usd=1.0, day_cap_usd=10.0)
        violated, used, cap = await t.check("agt_a")
        assert violated is None
        assert used == 0.0
        assert cap == 0.0

    @pytest.mark.asyncio
    async def test_hour_cap_exceeded(self, db: Database) -> None:
        await _insert_event(db, agent_id="agt_a", cost=1.5)
        t = CostCapTracker(db, hour_cap_usd=1.0, day_cap_usd=10.0)
        violated, used, cap = await t.check("agt_a")
        assert violated == "cost_hour"
        assert pytest.approx(used) == 1.5
        assert cap == 1.0

    @pytest.mark.asyncio
    async def test_day_cap_exceeded(self, db: Database) -> None:
        # Hour cap is generous, day cap is tight.
        await _insert_event(db, agent_id="agt_a", cost=0.50)
        t = CostCapTracker(db, hour_cap_usd=100.0, day_cap_usd=0.4)
        violated, used, cap = await t.check("agt_a")
        assert violated == "cost_day"
        assert pytest.approx(used) == 0.5
        assert cap == 0.4

    @pytest.mark.asyncio
    async def test_zero_caps_disabled(self, db: Database) -> None:
        """Cap == 0 → disabled. Even huge spend doesn't trip it."""
        await _insert_event(db, agent_id="agt_a", cost=1_000_000.0)
        t = CostCapTracker(db, hour_cap_usd=0.0, day_cap_usd=0.0)
        violated, used, cap = await t.check("agt_a")
        assert violated is None

    @pytest.mark.asyncio
    async def test_used_hour_and_day_helpers(self, db: Database) -> None:
        # Two recent ($0.4 each) + one 2h ago ($0.6).
        await _insert_event(db, agent_id="agt_a", cost=0.40)
        await _insert_event(db, agent_id="agt_a", cost=0.40)
        old = datetime.now(timezone.utc) - timedelta(hours=2)
        await _insert_event(db, agent_id="agt_a", cost=0.60, when=old)

        t = CostCapTracker(db, hour_cap_usd=10.0, day_cap_usd=10.0)
        assert pytest.approx(await t.used_hour("agt_a")) == 0.80
        assert pytest.approx(await t.used_day("agt_a")) == 1.40


# ---------------------------------------------------------------------------
# /v1/invoke integration — cost cap enforcement
# ---------------------------------------------------------------------------


def _sample_tool() -> dict:
    return {
        "tool_id": "web.fetch",
        "name": "Web Fetch",
        "description": "Fetch a URL",
        "transport": "http",
        "endpoint": "http://mcp.test/invoke/fetch",
        "input_schema": {"type": "object"},
        "output_schema": {"type": "object"},
        "idempotent": True,
        "side_effects": "read",
        "cache_ttl_seconds": 300,
        "auth_method": "none",
        "auth_config": {},
    }


async def _client_for_settings(settings: Settings):
    app = create_app(settings)
    transport = ASGITransport(app=app)
    return app, AsyncClient(
        transport=transport,
        base_url="http://testserver",
        headers={"Authorization": "Bearer test-token"},
    )


@pytest.mark.asyncio
async def test_invoke_cost_cap_hour_returns_429(tmp_path: Path) -> None:
    """Spec mandatory: /v1/invoke with cost cap exceeded → 429."""
    settings = Settings(
        data_dir=tmp_path / "cc1",
        log_level="WARNING",
        log_format="console",
        backend_timeout_seconds=5.0,
    )
    app, client = await _client_for_settings(settings)
    async with client, app.router.lifespan_context(app):
        await client.post("/v1/tools/register", json=_sample_tool())
        # web.fetch is $0.0005/call. Cap at $0.0006 → 2nd uncached call trips.
        await client.post(
            "/v1/limits/agt_cc",
            json={
                "rpm": 10000,
                "burst": 10000,
                "cost_cap_usd_hour": 0.0006,
                "cost_cap_usd_day": 1.0,
            },
        )

        with respx.mock(assert_all_called=False) as mock:
            mock.post("http://mcp.test/invoke/fetch").mock(
                return_value=Response(200, json={"content": "x"})
            )
            # First two calls: success ($0.0005 + $0.0005 = $0.001).
            for i in range(2):
                r = await client.post(
                    "/v1/invoke",
                    json={
                        "tool_id": "web.fetch",
                        "arguments": {"url": f"u{i}"},
                        "agent_id": "agt_cc",
                        "cache": False,
                    },
                )
                assert r.status_code == 200

            # Third call — accumulated cost ($0.001) ≥ cap ($0.0006).
            r = await client.post(
                "/v1/invoke",
                json={
                    "tool_id": "web.fetch",
                    "arguments": {"url": "u_third"},
                    "agent_id": "agt_cc",
                    "cache": False,
                },
            )
            assert r.status_code == 429
            body = r.json()
            assert body["error"]["code"] == "COST_CAP_EXCEEDED"
            assert body["error"]["details"]["limit_type"] == "cost_hour"
            assert "Retry-After" in r.headers


@pytest.mark.asyncio
async def test_invoke_cost_cap_day_returns_429(tmp_path: Path) -> None:
    """Cost cap (day window) triggers when only the day cap is tight."""
    settings = Settings(
        data_dir=tmp_path / "cc2",
        log_level="WARNING",
        log_format="console",
        backend_timeout_seconds=5.0,
    )
    app, client = await _client_for_settings(settings)
    async with client, app.router.lifespan_context(app):
        await client.post("/v1/tools/register", json=_sample_tool())
        await client.post(
            "/v1/limits/agt_dc",
            json={
                "rpm": 10000,
                "burst": 10000,
                "cost_cap_usd_hour": 100.0,
                "cost_cap_usd_day": 0.0006,
            },
        )

        with respx.mock(assert_all_called=False) as mock:
            mock.post("http://mcp.test/invoke/fetch").mock(
                return_value=Response(200, json={"content": "x"})
            )
            for i in range(2):
                r = await client.post(
                    "/v1/invoke",
                    json={
                        "tool_id": "web.fetch",
                        "arguments": {"url": f"u{i}"},
                        "agent_id": "agt_dc",
                        "cache": False,
                    },
                )
                assert r.status_code == 200

            r = await client.post(
                "/v1/invoke",
                json={
                    "tool_id": "web.fetch",
                    "arguments": {"url": "u_third"},
                    "agent_id": "agt_dc",
                    "cache": False,
                },
            )
            assert r.status_code == 429
            body = r.json()
            assert body["error"]["code"] == "COST_CAP_EXCEEDED"
            assert body["error"]["details"]["limit_type"] == "cost_day"


@pytest.mark.asyncio
async def test_cached_calls_dont_count_toward_cost(tmp_path: Path) -> None:
    """Cached invocations record $0 → must not push the agent over the cap."""
    settings = Settings(
        data_dir=tmp_path / "cc3",
        log_level="WARNING",
        log_format="console",
        backend_timeout_seconds=5.0,
    )
    app, client = await _client_for_settings(settings)
    async with client, app.router.lifespan_context(app):
        await client.post("/v1/tools/register", json=_sample_tool())
        # Cap at $0.0007. One real call = $0.0005. 100 cached → $0 still under.
        await client.post(
            "/v1/limits/agt_cached",
            json={
                "rpm": 10000,
                "burst": 10000,
                "cost_cap_usd_hour": 0.0007,
                "cost_cap_usd_day": 1.0,
            },
        )

        with respx.mock(assert_all_called=False) as mock:
            route = mock.post("http://mcp.test/invoke/fetch").mock(
                return_value=Response(200, json={"content": "x"})
            )
            # 1 cache miss.
            r = await client.post(
                "/v1/invoke",
                json={
                    "tool_id": "web.fetch",
                    "arguments": {"url": "u"},
                    "agent_id": "agt_cached",
                },
            )
            assert r.status_code == 200
            # 20 cache hits — all cost $0.
            for _ in range(20):
                r = await client.post(
                    "/v1/invoke",
                    json={
                        "tool_id": "web.fetch",
                        "arguments": {"url": "u"},
                        "agent_id": "agt_cached",
                    },
                )
                assert r.status_code == 200, r.text
                assert r.json()["cached"] is True
            # Backend was hit exactly once.
            assert route.call_count == 1
