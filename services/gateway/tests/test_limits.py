# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 The Plinth Authors
"""Tests for rate limiting + cost caps.

Covers three layers:

* ``TokenBucket`` math (fake clock — predictable refills + retry_after).
* ``LimitsRegistry`` — DB persistence, defaults, cost-window queries.
* The HTTP surface — ``GET/POST/DELETE /v1/limits/...``, ``/status`` and the
  enforcement leg in ``/v1/invoke``.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
import respx
from httpx import Response

from plinth_gateway.audit import AuditLog, AuditRecord
from plinth_gateway.exceptions import CostCapExceeded, RateLimited
from plinth_gateway.limits import LimitsRegistry, TokenBucket
from plinth_gateway.models import AgentLimitsBody


# ---------------------------------------------------------------------------
# TokenBucket — pure unit tests with a fake clock
# ---------------------------------------------------------------------------


class FakeClock:
    """Mutable monotonic clock for deterministic bucket math."""

    def __init__(self, start: float = 1000.0) -> None:
        self.now = float(start)

    def __call__(self) -> float:
        return self.now

    def advance(self, dt: float) -> None:
        self.now += dt


def test_bucket_starts_full() -> None:
    clock = FakeClock()
    b = TokenBucket(rate_per_second=1.0, capacity=5, time_fn=clock)
    assert b.tokens == 5.0
    assert b.snapshot_tokens() == 5.0


def test_bucket_consumes_token() -> None:
    clock = FakeClock()
    b = TokenBucket(rate_per_second=1.0, capacity=5, time_fn=clock)

    ok, retry = b.try_acquire(1)
    assert ok is True
    assert retry == 0.0
    assert b.tokens == 4.0


def test_bucket_drains_then_rejects() -> None:
    clock = FakeClock()
    b = TokenBucket(rate_per_second=1.0, capacity=3, time_fn=clock)

    for _ in range(3):
        ok, retry = b.try_acquire(1)
        assert ok is True
        assert retry == 0.0

    ok, retry = b.try_acquire(1)
    assert ok is False
    # Need 1 token, refill rate = 1/s → retry after ~1.0s.
    assert pytest.approx(retry, abs=1e-6) == 1.0


def test_bucket_refills_over_time() -> None:
    clock = FakeClock()
    b = TokenBucket(rate_per_second=2.0, capacity=2, time_fn=clock)

    # Drain.
    assert b.try_acquire(1)[0] is True
    assert b.try_acquire(1)[0] is True
    ok, retry = b.try_acquire(1)
    assert ok is False
    assert pytest.approx(retry, abs=1e-6) == 0.5

    # Advance 0.5s → 1 token back.
    clock.advance(0.5)
    ok, retry = b.try_acquire(1)
    assert ok is True
    assert retry == 0.0
    # Bucket should be empty again.
    ok, _ = b.try_acquire(1)
    assert ok is False


def test_bucket_refill_caps_at_capacity() -> None:
    clock = FakeClock()
    b = TokenBucket(rate_per_second=10.0, capacity=5, time_fn=clock)
    b.tokens = 0.0  # drained
    # 100 seconds of refill would be 1000 tokens — must clamp at capacity.
    clock.advance(100.0)
    assert b.snapshot_tokens() == 5.0


def test_bucket_partial_refill_math() -> None:
    """Asymmetric numbers — make sure the math isn't off-by-one."""
    clock = FakeClock()
    b = TokenBucket(rate_per_second=3.0, capacity=10, time_fn=clock)
    # Use 7 tokens.
    for _ in range(7):
        assert b.try_acquire(1)[0] is True
    assert b.tokens == 3.0

    # Wait 0.5s → +1.5 tokens.
    clock.advance(0.5)
    ok, _ = b.try_acquire(4)
    # Have 4.5 tokens, asked for 4 → ok; 0.5 left.
    assert ok is True
    assert pytest.approx(b.tokens, abs=1e-6) == 0.5

    # Asking for 1 now needs 0.5 more tokens → 0.5 / 3 = ~0.1667s.
    ok, retry = b.try_acquire(1)
    assert ok is False
    assert pytest.approx(retry, abs=1e-6) == 0.5 / 3.0


def test_bucket_acquire_more_than_capacity() -> None:
    """Asking for more than capacity — never satisfiable, always reject."""
    clock = FakeClock()
    b = TokenBucket(rate_per_second=1.0, capacity=3, time_fn=clock)
    ok, retry = b.try_acquire(5)
    assert ok is False
    # retry_after = n/rate when n > capacity.
    assert pytest.approx(retry, abs=1e-6) == 5.0


def test_bucket_acquire_zero_or_negative_is_noop() -> None:
    clock = FakeClock()
    b = TokenBucket(rate_per_second=1.0, capacity=3, time_fn=clock)
    ok, retry = b.try_acquire(0)
    assert ok is True
    assert retry == 0.0
    assert b.tokens == 3.0
    ok, _ = b.try_acquire(-1)
    assert ok is True
    assert b.tokens == 3.0


def test_bucket_zero_rate_means_no_refill() -> None:
    clock = FakeClock()
    b = TokenBucket(rate_per_second=0.0, capacity=2, time_fn=clock)
    assert b.try_acquire(1)[0] is True
    assert b.try_acquire(1)[0] is True
    ok, retry = b.try_acquire(1)
    assert ok is False
    assert retry == float("inf")


def test_bucket_validates_inputs() -> None:
    with pytest.raises(ValueError):
        TokenBucket(rate_per_second=-1.0, capacity=1)
    with pytest.raises(ValueError):
        TokenBucket(rate_per_second=1.0, capacity=-1)


# ---------------------------------------------------------------------------
# LimitsRegistry — DB-backed persistence + cost windows
# ---------------------------------------------------------------------------


async def test_registry_returns_defaults_when_no_override(db, settings) -> None:
    reg = LimitsRegistry(db, settings)
    limits = await reg.get_limits("agt_new")
    assert limits.agent_id == "agt_new"
    assert limits.rpm == settings.rate_limit_default_rpm
    assert limits.burst == settings.rate_limit_default_burst
    assert limits.cost_cap_usd_hour == settings.cost_cap_default_usd_hour
    assert limits.cost_cap_usd_day == settings.cost_cap_default_usd_day


async def test_registry_set_and_get_overrides(db, settings) -> None:
    reg = LimitsRegistry(db, settings)
    body = AgentLimitsBody(
        rpm=120, burst=40, cost_cap_usd_hour=2.5, cost_cap_usd_day=20.0
    )
    saved = await reg.set_limits("agt_x", body)
    assert saved.rpm == 120
    assert saved.burst == 40
    assert saved.cost_cap_usd_hour == 2.5
    assert saved.cost_cap_usd_day == 20.0

    fetched = await reg.get_limits("agt_x")
    assert fetched.rpm == 120
    assert fetched.cost_cap_usd_day == 20.0


async def test_registry_partial_update_merges_with_existing(db, settings) -> None:
    reg = LimitsRegistry(db, settings)
    await reg.set_limits("agt_x", AgentLimitsBody(rpm=100, burst=50))
    # Update only one field — others should keep saved values.
    new = await reg.set_limits("agt_x", AgentLimitsBody(cost_cap_usd_hour=5.0))
    assert new.rpm == 100
    assert new.burst == 50
    assert new.cost_cap_usd_hour == 5.0


async def test_registry_delete_reverts_to_defaults(db, settings) -> None:
    reg = LimitsRegistry(db, settings)
    await reg.set_limits("agt_x", AgentLimitsBody(rpm=999))
    assert (await reg.get_limits("agt_x")).rpm == 999

    removed = await reg.delete_limits("agt_x")
    assert removed is True

    again = await reg.get_limits("agt_x")
    assert again.rpm == settings.rate_limit_default_rpm


async def test_registry_delete_missing_returns_false(db, settings) -> None:
    reg = LimitsRegistry(db, settings)
    assert await reg.delete_limits("agt_never_set") is False


async def test_registry_cost_used_sums_audit_costs(db, settings) -> None:
    reg = LimitsRegistry(db, settings)
    audit = AuditLog(db)

    # Two non-cached events for agt_a, one cached event (must be excluded).
    await audit.record(
        AuditRecord(
            tool_id="web.fetch",
            arguments={"u": 1},
            workspace_id=None,
            agent_id="agt_a",
            arguments_hash="h",
            arguments_preview="p",
            cached=False,
            duration_ms=10,
            cost_estimate_usd=0.40,
        )
    )
    await audit.record(
        AuditRecord(
            tool_id="web.fetch",
            arguments={"u": 2},
            workspace_id=None,
            agent_id="agt_a",
            arguments_hash="h",
            arguments_preview="p",
            cached=False,
            duration_ms=10,
            cost_estimate_usd=0.30,
        )
    )
    await audit.record(
        AuditRecord(
            tool_id="web.fetch",
            arguments={"u": 3},
            workspace_id=None,
            agent_id="agt_a",
            arguments_hash="h",
            arguments_preview="p",
            cached=True,
            duration_ms=0,
            cost_estimate_usd=0.0,
        )
    )

    used = await reg.cost_used("agt_a", 1)
    assert pytest.approx(used) == 0.70
    used24 = await reg.cost_used("agt_a", 24)
    assert pytest.approx(used24) == 0.70


async def test_registry_cost_used_other_agent_isolated(db, settings) -> None:
    reg = LimitsRegistry(db, settings)
    audit = AuditLog(db)
    for _ in range(3):
        await audit.record(
            AuditRecord(
                tool_id="t",
                arguments={},
                workspace_id=None,
                agent_id="agt_a",
                arguments_hash="h",
                arguments_preview="p",
                cached=False,
                duration_ms=0,
                cost_estimate_usd=0.5,
            )
        )

    assert await reg.cost_used("agt_b", 1) == 0.0
    assert pytest.approx(await reg.cost_used("agt_a", 1)) == 1.5


async def test_registry_cost_window_excludes_old_events(db, settings) -> None:
    """Events older than the window must not count."""
    reg = LimitsRegistry(db, settings)

    # Record an event but rewrite its timestamp to 2 hours ago.
    old_ts = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat()
    await db.execute(
        """
        INSERT INTO audit_events
        (id, timestamp, tool_id, workspace_id, agent_id,
         arguments_hash, arguments_preview, result_hash,
         cached, duration_ms, cost_estimate_usd, error)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            "evt_old",
            old_ts,
            "web.fetch",
            None,
            "agt_a",
            "h" * 64,
            "{}",
            None,
            0,
            10,
            0.99,
            None,
        ),
    )

    # 1h window excludes it.
    assert await reg.cost_used("agt_a", 1) == 0.0
    # 24h window includes it.
    assert pytest.approx(await reg.cost_used("agt_a", 24)) == 0.99


async def test_registry_check_rate_uses_overrides(db, settings) -> None:
    """A new override should take effect on the next bucket creation."""
    clock = FakeClock()
    reg = LimitsRegistry(db, settings, time_fn=clock)
    # Tight bucket: 60 rpm, burst 2.
    await reg.set_limits("agt_a", AgentLimitsBody(rpm=60, burst=2))

    ok1, _ = await reg.check_rate("agt_a")
    ok2, _ = await reg.check_rate("agt_a")
    assert ok1 and ok2
    ok3, retry = await reg.check_rate("agt_a")
    assert ok3 is False
    # 60 rpm = 1/s; need 1 → retry ~1.0s.
    assert pytest.approx(retry, abs=1e-6) == 1.0


async def test_registry_set_resets_existing_bucket(db, settings) -> None:
    """When the override is changed, the in-memory bucket must be rebuilt."""
    clock = FakeClock()
    reg = LimitsRegistry(db, settings, time_fn=clock)
    await reg.set_limits("agt_a", AgentLimitsBody(rpm=60, burst=1))
    ok, _ = await reg.check_rate("agt_a")
    assert ok is True
    ok, _ = await reg.check_rate("agt_a")
    assert ok is False  # drained

    # Bump capacity → fresh bucket should hold burst tokens.
    await reg.set_limits("agt_a", AgentLimitsBody(burst=10))
    ok, _ = await reg.check_rate("agt_a")
    assert ok is True
    # 9 more should still work in burst.
    for _ in range(9):
        assert (await reg.check_rate("agt_a"))[0] is True


async def test_registry_independent_buckets_per_agent(db, settings) -> None:
    clock = FakeClock()
    reg = LimitsRegistry(db, settings, time_fn=clock)
    await reg.set_limits("agt_a", AgentLimitsBody(rpm=60, burst=1))
    await reg.set_limits("agt_b", AgentLimitsBody(rpm=60, burst=1))

    # Drain agt_a.
    assert (await reg.check_rate("agt_a"))[0] is True
    assert (await reg.check_rate("agt_a"))[0] is False
    # agt_b is untouched.
    assert (await reg.check_rate("agt_b"))[0] is True


async def test_registry_assert_within_rate_raises_on_empty_bucket(db, settings) -> None:
    clock = FakeClock()
    reg = LimitsRegistry(db, settings, time_fn=clock)
    await reg.set_limits("agt_a", AgentLimitsBody(rpm=60, burst=1))
    await reg.assert_within_rate("agt_a")  # consumes the only token
    with pytest.raises(RateLimited) as excinfo:
        await reg.assert_within_rate("agt_a")
    assert excinfo.value.reason == "rpm"
    assert excinfo.value.retry_after > 0
    assert excinfo.value.code == "RATE_LIMITED"
    assert excinfo.value.http_status == 429


async def test_registry_assert_within_cost_caps_hour(db, settings) -> None:
    reg = LimitsRegistry(db, settings)
    audit = AuditLog(db)
    await reg.set_limits(
        "agt_a", AgentLimitsBody(cost_cap_usd_hour=0.5, cost_cap_usd_day=10.0)
    )
    # Spend $0.6 in last hour → cap exceeded.
    await audit.record(
        AuditRecord(
            tool_id="t",
            arguments={},
            workspace_id=None,
            agent_id="agt_a",
            arguments_hash="h",
            arguments_preview="p",
            cached=False,
            duration_ms=0,
            cost_estimate_usd=0.6,
        )
    )
    with pytest.raises(CostCapExceeded) as excinfo:
        await reg.assert_within_cost_caps("agt_a")
    assert excinfo.value.reason == "cost_hour"
    assert pytest.approx(excinfo.value.used) == 0.6
    assert excinfo.value.cap == 0.5


async def test_registry_assert_within_cost_caps_day(db, settings) -> None:
    """When the hour cap is high but day cap is hit, raise cost_day."""
    reg = LimitsRegistry(db, settings)
    audit = AuditLog(db)
    await reg.set_limits(
        "agt_a", AgentLimitsBody(cost_cap_usd_hour=100.0, cost_cap_usd_day=1.0)
    )
    await audit.record(
        AuditRecord(
            tool_id="t",
            arguments={},
            workspace_id=None,
            agent_id="agt_a",
            arguments_hash="h",
            arguments_preview="p",
            cached=False,
            duration_ms=0,
            cost_estimate_usd=1.5,
        )
    )
    with pytest.raises(CostCapExceeded) as excinfo:
        await reg.assert_within_cost_caps("agt_a")
    assert excinfo.value.reason == "cost_day"


async def test_registry_cost_cap_zero_disables(db, settings) -> None:
    """``cost_cap_usd_hour = 0`` (and day = 0) disables the cap entirely."""
    reg = LimitsRegistry(db, settings)
    audit = AuditLog(db)
    await reg.set_limits(
        "agt_a", AgentLimitsBody(cost_cap_usd_hour=0.0, cost_cap_usd_day=0.0)
    )
    # Spend a million dollars — should still be fine (caps off).
    await audit.record(
        AuditRecord(
            tool_id="t",
            arguments={},
            workspace_id=None,
            agent_id="agt_a",
            arguments_hash="h",
            arguments_preview="p",
            cached=False,
            duration_ms=0,
            cost_estimate_usd=1_000_000.0,
        )
    )
    # Should not raise.
    await reg.assert_within_cost_caps("agt_a")


async def test_registry_rpm_used_counts_recent_events(db, settings) -> None:
    reg = LimitsRegistry(db, settings)
    audit = AuditLog(db)
    for i in range(3):
        await audit.record(
            AuditRecord(
                tool_id="t",
                arguments={"i": i},
                workspace_id=None,
                agent_id="agt_a",
                arguments_hash="h",
                arguments_preview="p",
                cached=False,
                duration_ms=0,
                cost_estimate_usd=0.0,
            )
        )
    assert await reg.rpm_used("agt_a") == 3


# ---------------------------------------------------------------------------
# HTTP surface — limits CRUD + status
# ---------------------------------------------------------------------------


async def test_get_limits_returns_defaults_for_unknown_agent(client) -> None:
    r = await client.get("/v1/limits/agt_unknown")
    assert r.status_code == 200
    body = r.json()
    assert body["agent_id"] == "agt_unknown"
    # These match the Settings defaults loaded by the test fixture.
    assert body["rpm"] == 60
    assert body["burst"] == 20
    assert body["cost_cap_usd_hour"] == 1.0
    assert body["cost_cap_usd_day"] == 10.0


async def test_post_then_get_limits(client) -> None:
    r = await client.post(
        "/v1/limits/agt_x",
        json={
            "rpm": 99,
            "burst": 10,
            "cost_cap_usd_hour": 0.25,
            "cost_cap_usd_day": 5.0,
        },
    )
    assert r.status_code == 200
    saved = r.json()
    assert saved["agent_id"] == "agt_x"
    assert saved["rpm"] == 99

    r = await client.get("/v1/limits/agt_x")
    assert r.status_code == 200
    body = r.json()
    assert body["rpm"] == 99
    assert body["cost_cap_usd_hour"] == 0.25


async def test_post_limits_partial_body(client) -> None:
    """Only some fields specified — others should fall back to defaults."""
    r = await client.post("/v1/limits/agt_x", json={"rpm": 5})
    assert r.status_code == 200
    body = r.json()
    assert body["rpm"] == 5
    assert body["burst"] == 20  # default


async def test_delete_limits(client) -> None:
    await client.post("/v1/limits/agt_x", json={"rpm": 1})
    r = await client.delete("/v1/limits/agt_x")
    assert r.status_code == 204
    # After delete, GET returns defaults.
    r = await client.get("/v1/limits/agt_x")
    assert r.json()["rpm"] == 60


async def test_delete_limits_missing_is_204(client) -> None:
    """DELETE is idempotent — 204 even when no row existed."""
    r = await client.delete("/v1/limits/agt_never_set")
    assert r.status_code == 204


async def test_post_limits_rejects_extra_fields(client) -> None:
    r = await client.post("/v1/limits/agt_x", json={"foo": "bar"})
    assert r.status_code == 422


async def test_post_limits_rejects_negative_values(client) -> None:
    r = await client.post("/v1/limits/agt_x", json={"rpm": -1})
    assert r.status_code == 422


async def test_status_endpoint(client, make_tool) -> None:
    await client.post("/v1/tools/register", json=make_tool())

    # Bump the cost cap so a couple invokes don't trip it; we just want usage
    # numbers to come back non-zero.
    await client.post(
        "/v1/limits/agt_demo",
        json={"rpm": 100, "burst": 100, "cost_cap_usd_hour": 100.0, "cost_cap_usd_day": 100.0},
    )

    with respx.mock(assert_all_called=False) as mock:
        mock.post("http://mcp.test/invoke/fetch").mock(
            return_value=Response(200, json={"content": "x"})
        )
        for i in range(2):
            await client.post(
                "/v1/invoke",
                json={
                    "tool_id": "web.fetch",
                    "arguments": {"url": f"u{i}"},
                    "agent_id": "agt_demo",
                },
            )

    r = await client.get("/v1/limits/agt_demo/status")
    assert r.status_code == 200
    body = r.json()
    assert body["agent_id"] == "agt_demo"
    assert body["rpm_limit"] == 100
    assert body["rpm_used_in_window"] == 2
    assert body["cost_used_usd_hour"] > 0
    assert body["cost_used_usd_day"] >= body["cost_used_usd_hour"]


# ---------------------------------------------------------------------------
# Enforcement in /v1/invoke
# ---------------------------------------------------------------------------


async def test_invoke_rate_limited_returns_429(app_and_client, make_tool) -> None:
    app, client = app_and_client
    await client.post("/v1/tools/register", json=make_tool())

    # rpm=60 → rate=1/s; burst=2 → 2 tokens. Third call must 429.
    await client.post(
        "/v1/limits/agt_rl",
        json={"rpm": 60, "burst": 2, "cost_cap_usd_hour": 100.0, "cost_cap_usd_day": 100.0},
    )

    with respx.mock(assert_all_called=False) as mock:
        mock.post("http://mcp.test/invoke/fetch").mock(
            return_value=Response(200, json={"content": "x"})
        )
        # First two — succeed.
        for i in range(2):
            r = await client.post(
                "/v1/invoke",
                json={
                    "tool_id": "web.fetch",
                    "arguments": {"url": f"u{i}"},
                    "agent_id": "agt_rl",
                },
            )
            assert r.status_code == 200, r.text

        # Third — 429.
        r = await client.post(
            "/v1/invoke",
            json={
                "tool_id": "web.fetch",
                "arguments": {"url": "u_third"},
                "agent_id": "agt_rl",
            },
        )

    assert r.status_code == 429
    body = r.json()
    assert body["error"]["code"] == "RATE_LIMITED"
    assert body["error"]["details"]["limit_type"] == "rpm"
    assert "Retry-After" in r.headers
    assert int(r.headers["Retry-After"]) >= 1


async def test_invoke_cost_capped_returns_429(app_and_client, make_tool) -> None:
    app, client = app_and_client
    await client.post("/v1/tools/register", json=make_tool())

    # Tight cost cap, generous rate so we don't hit rpm first.
    await client.post(
        "/v1/limits/agt_cc",
        json={
            "rpm": 1000,
            "burst": 1000,
            "cost_cap_usd_hour": 0.0006,
            "cost_cap_usd_day": 1.0,
        },
    )
    # web.fetch costs $0.0005 per call. Two non-cached calls = $0.001 > $0.0006.

    with respx.mock(assert_all_called=False) as mock:
        mock.post("http://mcp.test/invoke/fetch").mock(
            return_value=Response(200, json={"content": "x"})
        )
        # Disable cache to make sure the second call also costs.
        for i in range(2):
            await client.post(
                "/v1/invoke",
                json={
                    "tool_id": "web.fetch",
                    "arguments": {"url": f"u{i}"},
                    "agent_id": "agt_cc",
                    "cache": False,
                },
            )

        # Third call — accumulated cost ($0.001) >= cap ($0.0006).
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


async def test_invoke_cost_capped_day(app_and_client, make_tool) -> None:
    app, client = app_and_client
    await client.post("/v1/tools/register", json=make_tool())

    # Hour cap relaxed, day cap tight.
    await client.post(
        "/v1/limits/agt_dc",
        json={
            "rpm": 1000,
            "burst": 1000,
            "cost_cap_usd_hour": 100.0,
            "cost_cap_usd_day": 0.0006,
        },
    )

    with respx.mock(assert_all_called=False) as mock:
        mock.post("http://mcp.test/invoke/fetch").mock(
            return_value=Response(200, json={"content": "x"})
        )
        for i in range(2):
            await client.post(
                "/v1/invoke",
                json={
                    "tool_id": "web.fetch",
                    "arguments": {"url": f"u{i}"},
                    "agent_id": "agt_dc",
                    "cache": False,
                },
            )
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


async def test_invoke_cached_calls_dont_count_toward_cost(
    app_and_client, make_tool
) -> None:
    """Cached invocations cost $0 → must not push the agent over the cap."""
    app, client = app_and_client
    await client.post("/v1/tools/register", json=make_tool())

    # Cap at $0.0007. One real call = $0.0005 (web.fetch). 100 cached calls
    # = $0 → still under cap.
    await client.post(
        "/v1/limits/agt_cached",
        json={
            "rpm": 1000,
            "burst": 1000,
            "cost_cap_usd_hour": 0.0007,
            "cost_cap_usd_day": 1.0,
        },
    )

    with respx.mock(assert_all_called=False) as mock:
        route = mock.post("http://mcp.test/invoke/fetch").mock(
            return_value=Response(200, json={"content": "x"})
        )
        # 1 miss (real) → $0.0005.
        r = await client.post(
            "/v1/invoke",
            json={
                "tool_id": "web.fetch",
                "arguments": {"url": "u"},
                "agent_id": "agt_cached",
            },
        )
        assert r.status_code == 200
        # Cached hits — many of them, all $0.
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
        assert route.call_count == 1


async def test_invoke_independent_agent_buckets(app_and_client, make_tool) -> None:
    """Two agents share a tool but each has its own bucket."""
    app, client = app_and_client
    await client.post("/v1/tools/register", json=make_tool())

    for aid in ("agt_p", "agt_q"):
        await client.post(
            f"/v1/limits/{aid}",
            json={
                "rpm": 60,
                "burst": 1,
                "cost_cap_usd_hour": 100.0,
                "cost_cap_usd_day": 100.0,
            },
        )

    with respx.mock(assert_all_called=False) as mock:
        mock.post("http://mcp.test/invoke/fetch").mock(
            return_value=Response(200, json={"content": "x"})
        )
        # agt_p drains its single token.
        r = await client.post(
            "/v1/invoke",
            json={"tool_id": "web.fetch", "arguments": {"url": "u1"}, "agent_id": "agt_p"},
        )
        assert r.status_code == 200
        r = await client.post(
            "/v1/invoke",
            json={"tool_id": "web.fetch", "arguments": {"url": "u2"}, "agent_id": "agt_p"},
        )
        assert r.status_code == 429

        # agt_q untouched.
        r = await client.post(
            "/v1/invoke",
            json={"tool_id": "web.fetch", "arguments": {"url": "u3"}, "agent_id": "agt_q"},
        )
        assert r.status_code == 200


async def test_invoke_without_agent_id_skips_limits(
    app_and_client, make_tool
) -> None:
    """Per CONTRACTS.md v0.2: anonymous calls (no agent_id) skip limit checks.

    A workspace_id without an agent_id no longer triggers enforcement — the
    workspace fallback was dropped in favour of the explicit "identified
    traffic only" semantics. When OAuth-issued tokens land we'll re-enforce.
    """
    _, client = app_and_client
    await client.post("/v1/tools/register", json=make_tool())

    # Override on workspace_id should be ignored — no agent_id on the call.
    await client.post(
        "/v1/limits/ws_demo",
        json={
            "rpm": 60,
            "burst": 1,
            "cost_cap_usd_hour": 100.0,
            "cost_cap_usd_day": 100.0,
        },
    )

    with respx.mock(assert_all_called=False) as mock:
        mock.post("http://mcp.test/invoke/fetch").mock(
            return_value=Response(200, json={"content": "x"})
        )
        # 5 calls, no agent_id — never rate-limited even though burst is 1.
        for i in range(5):
            r = await client.post(
                "/v1/invoke",
                json={
                    "tool_id": "web.fetch",
                    "arguments": {"url": f"u{i}"},
                    "workspace_id": "ws_demo",
                    "cache": False,
                },
            )
            assert r.status_code == 200, f"call {i}: {r.text}"


async def test_invoke_default_limits_apply_when_no_override(
    app_and_client, make_tool
) -> None:
    """An agent with no override row uses the global defaults."""
    app, client = app_and_client
    await client.post("/v1/tools/register", json=make_tool())

    # Default rpm=60, burst=20 — 20 immediate calls fit in burst.
    with respx.mock(assert_all_called=False) as mock:
        mock.post("http://mcp.test/invoke/fetch").mock(
            return_value=Response(200, json={"content": "x"})
        )
        for i in range(20):
            r = await client.post(
                "/v1/invoke",
                json={
                    "tool_id": "web.fetch",
                    "arguments": {"url": f"u{i}"},
                    "agent_id": "agt_default",
                    "cache": False,
                },
            )
            assert r.status_code == 200, f"call {i}: {r.text}"

        # Call 21 — bucket drained, default rpm=60 needs ~1s to refill 1 token.
        r = await client.post(
            "/v1/invoke",
            json={
                "tool_id": "web.fetch",
                "arguments": {"url": "u_21"},
                "agent_id": "agt_default",
                "cache": False,
            },
        )
        assert r.status_code == 429
        assert r.json()["error"]["code"] == "RATE_LIMITED"


async def test_429_retry_after_header_is_integer_seconds(
    app_and_client, make_tool
) -> None:
    """The Retry-After header must be a positive integer (HTTP/1.1 spec)."""
    app, client = app_and_client
    await client.post("/v1/tools/register", json=make_tool())

    await client.post(
        "/v1/limits/agt_h",
        json={
            "rpm": 60,
            "burst": 1,
            "cost_cap_usd_hour": 100.0,
            "cost_cap_usd_day": 100.0,
        },
    )

    with respx.mock(assert_all_called=False) as mock:
        mock.post("http://mcp.test/invoke/fetch").mock(
            return_value=Response(200, json={"content": "x"})
        )
        await client.post(
            "/v1/invoke",
            json={"tool_id": "web.fetch", "arguments": {"url": "u"}, "agent_id": "agt_h"},
        )
        r = await client.post(
            "/v1/invoke",
            json={"tool_id": "web.fetch", "arguments": {"url": "u2"}, "agent_id": "agt_h"},
        )
    assert r.status_code == 429
    ra = r.headers.get("Retry-After")
    assert ra is not None
    assert int(ra) >= 1


async def test_limits_endpoints_require_auth(app_and_client) -> None:
    """All /v1/limits routes are auth-gated."""
    _, client = app_and_client
    for method, path in [
        ("get", "/v1/limits/agt_x"),
        ("post", "/v1/limits/agt_x"),
        ("delete", "/v1/limits/agt_x"),
        ("get", "/v1/limits/agt_x/status"),
    ]:
        kwargs = {"headers": {"Authorization": ""}}
        if method == "post":
            kwargs["json"] = {}
        r = await getattr(client, method)(path, **kwargs)
        assert r.status_code == 401, f"{method} {path} → {r.status_code}"
