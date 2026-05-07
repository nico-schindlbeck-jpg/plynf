# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 The Plinth Authors
"""Tests for the token-bucket rate limiter (``plinth_gateway.rate_limit``).

Layered:

1. :class:`TokenBucket` — pure math; uses a fake clock for determinism.
2. :class:`RateLimiter` — per-agent bucket map; isolation + rebuild semantics.
3. Snapshot persistence — ``snapshot_to_db`` / ``restore_from_db`` round-trip.
4. ``/v1/invoke`` integration — covers the spec's mandatory cases:
   - rate exceeded → 429 + Retry-After
   - missing agent_id → no limits applied
   - ``rate_limits_enabled = False`` → no limits applied
"""

from __future__ import annotations

import time
from pathlib import Path

import pytest
import pytest_asyncio
import respx
from httpx import ASGITransport, AsyncClient, Response

from plinth_gateway.api import create_app
from plinth_gateway.db import Database
from plinth_gateway.rate_limit import (
    RateLimiter,
    TokenBucket,
    restore_from_db,
    snapshot_to_db,
)
from plinth_gateway.settings import Settings

# ---------------------------------------------------------------------------
# Fake clock
# ---------------------------------------------------------------------------


class FakeClock:
    """Mutable monotonic-style clock for deterministic refill math."""

    def __init__(self, start: float = 1000.0) -> None:
        self.now = float(start)

    def __call__(self) -> float:
        return self.now

    def advance(self, dt: float) -> None:
        self.now += dt


# ---------------------------------------------------------------------------
# TokenBucket
# ---------------------------------------------------------------------------


class TestTokenBucket:
    """Pure unit tests for the bucket primitive."""

    def test_starts_full(self) -> None:
        clock = FakeClock()
        b = TokenBucket(rate_per_second=1.0, capacity=5, time_fn=clock)
        assert b.tokens == 5.0
        assert b.snapshot_tokens() == 5.0

    def test_burst_5_allows_5_calls(self) -> None:
        """Spec: 5 calls within burst all succeed; 6th in tight window blocks."""
        clock = FakeClock()
        b = TokenBucket(rate_per_second=1.0, capacity=5, time_fn=clock)
        # Five immediate calls — every one succeeds.
        for i in range(5):
            ok, retry = b.try_acquire(1)
            assert ok is True, f"call {i+1} should succeed"
            assert retry == 0.0

        # Sixth call without time advancing — bucket is empty.
        ok, retry = b.try_acquire(1)
        assert ok is False
        # rate=1/s, deficit=1 → wait ~1.0s
        assert pytest.approx(retry, abs=1e-6) == 1.0

    def test_refill_after_sleep_succeeds(self) -> None:
        """Spec: refill works (sleep + try_consume succeeds)."""
        clock = FakeClock()
        b = TokenBucket(rate_per_second=2.0, capacity=2, time_fn=clock)
        # Drain.
        assert b.try_acquire(1)[0] is True
        assert b.try_acquire(1)[0] is True
        ok, retry = b.try_acquire(1)
        assert ok is False
        assert pytest.approx(retry, abs=1e-6) == 0.5

        # Advance past the deficit — next call should succeed.
        clock.advance(0.5)
        ok, retry = b.try_acquire(1)
        assert ok is True
        assert retry == 0.0

    def test_refill_caps_at_capacity(self) -> None:
        """A long sleep cannot let the bucket exceed capacity."""
        clock = FakeClock()
        b = TokenBucket(rate_per_second=10.0, capacity=5, time_fn=clock)
        b.tokens = 0.0  # drain manually
        clock.advance(100.0)  # 1000 tokens worth of refill
        assert b.snapshot_tokens() == 5.0  # clamped

    def test_partial_refill_math(self) -> None:
        """Asymmetric numbers — guard against off-by-one in the math."""
        clock = FakeClock()
        b = TokenBucket(rate_per_second=3.0, capacity=10, time_fn=clock)
        # 7 tokens used.
        for _ in range(7):
            assert b.try_acquire(1)[0] is True
        assert b.tokens == 3.0

        # +0.5s → +1.5 tokens (now 4.5).
        clock.advance(0.5)
        ok, _ = b.try_acquire(4)
        assert ok is True
        assert pytest.approx(b.tokens, abs=1e-6) == 0.5

        # Need 1 more → require 0.5 tokens → 0.5 / 3 ≈ 0.1667s.
        ok, retry = b.try_acquire(1)
        assert ok is False
        assert pytest.approx(retry, abs=1e-6) == 0.5 / 3.0

    def test_acquire_more_than_capacity(self) -> None:
        """Asking for more than capacity — never satisfiable, surface estimate."""
        clock = FakeClock()
        b = TokenBucket(rate_per_second=1.0, capacity=3, time_fn=clock)
        ok, retry = b.try_acquire(5)
        assert ok is False
        assert pytest.approx(retry, abs=1e-6) == 5.0  # n / rate

    def test_acquire_zero_or_negative_is_noop(self) -> None:
        clock = FakeClock()
        b = TokenBucket(rate_per_second=1.0, capacity=3, time_fn=clock)
        ok, retry = b.try_acquire(0)
        assert ok is True
        assert retry == 0.0
        assert b.tokens == 3.0
        ok, _ = b.try_acquire(-1)
        assert ok is True
        assert b.tokens == 3.0  # untouched

    def test_zero_rate_no_refill(self) -> None:
        clock = FakeClock()
        b = TokenBucket(rate_per_second=0.0, capacity=2, time_fn=clock)
        assert b.try_acquire(1)[0] is True
        assert b.try_acquire(1)[0] is True
        ok, retry = b.try_acquire(1)
        assert ok is False
        assert retry == float("inf")

    def test_validates_inputs(self) -> None:
        with pytest.raises(ValueError):
            TokenBucket(rate_per_second=-1.0, capacity=1)
        with pytest.raises(ValueError):
            TokenBucket(rate_per_second=1.0, capacity=-1)

    def test_default_clock_is_monotonic(self) -> None:
        """Without ``time_fn``, the bucket uses ``time.monotonic`` — sanity check."""
        b = TokenBucket(rate_per_second=1000.0, capacity=1000)
        # Just make sure it runs and the snapshot returns a sensible number.
        n = b.snapshot_tokens()
        assert 0 <= n <= 1000


# ---------------------------------------------------------------------------
# RateLimiter
# ---------------------------------------------------------------------------


class TestRateLimiter:
    """Per-agent bucket store."""

    @pytest.mark.asyncio
    async def test_per_agent_isolation(self) -> None:
        """Spec: agent A consumed doesn't affect agent B."""
        clock = FakeClock()
        rl = RateLimiter(time_fn=clock)
        # Drain agent A (rpm=60, burst=1 → 1 token, refill 1/s).
        ok, _ = await rl.check("agt_a", rpm=60, burst=1)
        assert ok is True
        ok, _ = await rl.check("agt_a", rpm=60, burst=1)
        assert ok is False  # drained

        # Agent B is untouched.
        ok, _ = await rl.check("agt_b", rpm=60, burst=1)
        assert ok is True

    @pytest.mark.asyncio
    async def test_lazy_creation(self) -> None:
        rl = RateLimiter()
        assert (await rl.get_bucket("agt_x")) is None
        await rl.check("agt_x", rpm=60, burst=5)
        entry = await rl.get_bucket("agt_x")
        assert entry is not None
        assert entry.rpm == 60
        assert entry.burst == 5

    @pytest.mark.asyncio
    async def test_change_rpm_rebuilds_bucket(self) -> None:
        clock = FakeClock()
        rl = RateLimiter(time_fn=clock)
        # Drain at burst=1.
        await rl.check("agt_x", rpm=60, burst=1)
        ok, _ = await rl.check("agt_x", rpm=60, burst=1)
        assert ok is False

        # Bump burst — bucket rebuilt full.
        ok, _ = await rl.check("agt_x", rpm=60, burst=10)
        assert ok is True
        # 9 more should still fit.
        for _ in range(9):
            assert (await rl.check("agt_x", rpm=60, burst=10))[0] is True

    @pytest.mark.asyncio
    async def test_change_rpm_only_rebuilds_bucket(self) -> None:
        """A pure rpm change (same burst) also rebuilds — refill rate matters."""
        clock = FakeClock()
        rl = RateLimiter(time_fn=clock)
        # Burst=2, rpm=60 → 1/s refill.
        await rl.check("agt_x", rpm=60, burst=2)
        await rl.check("agt_x", rpm=60, burst=2)
        ok, _ = await rl.check("agt_x", rpm=60, burst=2)
        assert ok is False

        # Bump rpm to 600 (10/s) — bucket rebuilt full at burst=2.
        ok, _ = await rl.check("agt_x", rpm=600, burst=2)
        assert ok is True

    @pytest.mark.asyncio
    async def test_reset_drops_bucket(self) -> None:
        rl = RateLimiter()
        await rl.check("agt_x", rpm=60, burst=1)
        assert (await rl.get_bucket("agt_x")) is not None
        await rl.reset("agt_x")
        assert (await rl.get_bucket("agt_x")) is None

    @pytest.mark.asyncio
    async def test_reset_all(self) -> None:
        rl = RateLimiter()
        await rl.check("a", rpm=60, burst=1)
        await rl.check("b", rpm=60, burst=1)
        await rl.reset_all()
        assert (await rl.get_bucket("a")) is None
        assert (await rl.get_bucket("b")) is None

    @pytest.mark.asyncio
    async def test_concurrent_check_creates_one_bucket(self) -> None:
        """Many coroutines hitting a fresh agent_id must not race-create N buckets.

        Uses a frozen clock so 20 ``check`` calls draining a 20-token bucket
        leave exactly 0 tokens (no refill in between).
        """
        import asyncio

        clock = FakeClock()
        rl = RateLimiter(time_fn=clock)
        results = await asyncio.gather(
            *[rl.check("agt_x", rpm=60, burst=20) for _ in range(20)]
        )
        # All 20 calls land on the same bucket (capacity 20) → all succeed.
        assert all(ok for ok, _ in results)
        entry = await rl.get_bucket("agt_x")
        assert entry is not None
        # Capacity wasn't multiplied by 20 buckets — one bucket, drained.
        assert entry.bucket.tokens == pytest.approx(0.0, abs=1e-6)
        # And one more must fail (bucket really is empty, not 20× full).
        ok, _ = await rl.check("agt_x", rpm=60, burst=20)
        assert ok is False


# ---------------------------------------------------------------------------
# Snapshot persistence
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def snapshot_db(tmp_path: Path):
    db = Database(tmp_path / "snap.db")
    await db.connect()
    try:
        yield db
    finally:
        await db.close()


class TestSnapshotPersistence:
    @pytest.mark.asyncio
    async def test_round_trip(self, snapshot_db) -> None:
        """Snapshot writes one row per agent; restore returns the same map."""
        clock = FakeClock()
        rl = RateLimiter(time_fn=clock)
        # Two agents, drain each by different amounts.
        for _ in range(3):
            await rl.check("agt_a", rpm=60, burst=10)
        for _ in range(7):
            await rl.check("agt_b", rpm=60, burst=10)

        written = await snapshot_to_db(snapshot_db, rl)
        assert written == 2

        snapshot = await restore_from_db(snapshot_db)
        # 7 tokens left for a, 3 for b.
        assert pytest.approx(snapshot["agt_a"], abs=1e-6) == 7.0
        assert pytest.approx(snapshot["agt_b"], abs=1e-6) == 3.0

    @pytest.mark.asyncio
    async def test_snapshot_overwrites_existing(self, snapshot_db) -> None:
        """A second snapshot for the same agent updates the row."""
        clock = FakeClock()
        rl = RateLimiter(time_fn=clock)
        await rl.check("agt_a", rpm=60, burst=10)  # 9 left
        await snapshot_to_db(snapshot_db, rl)

        for _ in range(4):
            await rl.check("agt_a", rpm=60, burst=10)  # now 5 left
        await snapshot_to_db(snapshot_db, rl)

        snap = await restore_from_db(snapshot_db)
        assert pytest.approx(snap["agt_a"], abs=1e-6) == 5.0

    @pytest.mark.asyncio
    async def test_restore_empty_db(self, snapshot_db) -> None:
        """Restore returns ``{}`` for an empty snapshot table."""
        snap = await restore_from_db(snapshot_db)
        assert snap == {}


# ---------------------------------------------------------------------------
# /v1/invoke integration — rate-limit enforcement
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


@pytest_asyncio.fixture
async def app_with_settings(tmp_path: Path):
    """Yield (settings, client) — fresh app per test."""

    def _make(**overrides):
        s = Settings(
            data_dir=tmp_path / f"d_{overrides.get('data_subdir','x')}",
            gateway_host="127.0.0.1",
            gateway_port=7422,
            log_level="WARNING",
            log_format="console",
            backend_timeout_seconds=5.0,
            **{k: v for k, v in overrides.items() if k != "data_subdir"},
        )
        return s

    yield _make


async def _client_for_settings(settings: Settings):
    app = create_app(settings)
    transport = ASGITransport(app=app)
    return app, AsyncClient(
        transport=transport,
        base_url="http://testserver",
        headers={"Authorization": "Bearer test-token"},
    )


@pytest.mark.asyncio
async def test_invoke_rate_exceeded_returns_429_with_retry_after(tmp_path: Path) -> None:
    """Spec mandatory: /v1/invoke with rate exceeded → 429 + Retry-After header."""
    settings = Settings(
        data_dir=tmp_path / "rl1",
        log_level="WARNING",
        log_format="console",
        backend_timeout_seconds=5.0,
    )
    app, client = await _client_for_settings(settings)
    async with client, app.router.lifespan_context(app):
        await client.post("/v1/tools/register", json=_sample_tool())
        # Tight bucket — 1 rpm, 1 burst.
        await client.post(
            "/v1/limits/agt_rl",
            json={"rpm": 60, "burst": 1, "cost_cap_usd_hour": 0, "cost_cap_usd_day": 0},
        )

        with respx.mock(assert_all_called=False) as mock:
            mock.post("http://mcp.test/invoke/fetch").mock(
                return_value=Response(200, json={"content": "x"})
            )
            # First call OK.
            r = await client.post(
                "/v1/invoke",
                json={
                    "tool_id": "web.fetch",
                    "arguments": {"url": "u1"},
                    "agent_id": "agt_rl",
                    "cache": False,
                },
            )
            assert r.status_code == 200, r.text

            # Second call — bucket empty → 429.
            r = await client.post(
                "/v1/invoke",
                json={
                    "tool_id": "web.fetch",
                    "arguments": {"url": "u2"},
                    "agent_id": "agt_rl",
                    "cache": False,
                },
            )
            assert r.status_code == 429
            body = r.json()
            assert body["error"]["code"] == "RATE_LIMITED"
            assert body["error"]["details"]["limit_type"] == "rpm"
            # Retry-After must be a positive integer per HTTP/1.1.
            ra = r.headers.get("Retry-After")
            assert ra is not None
            assert int(ra) >= 1
            # retry_after_seconds in the body should be sane (≤ 60).
            assert 0 <= body["error"]["details"]["retry_after_seconds"] <= 60


@pytest.mark.asyncio
async def test_invoke_without_agent_id_skips_limits(tmp_path: Path) -> None:
    """Spec mandatory: /v1/invoke without agent_id → no limits applied."""
    settings = Settings(
        data_dir=tmp_path / "rl2",
        log_level="WARNING",
        log_format="console",
        backend_timeout_seconds=5.0,
    )
    app, client = await _client_for_settings(settings)
    async with client, app.router.lifespan_context(app):
        await client.post("/v1/tools/register", json=_sample_tool())

        with respx.mock(assert_all_called=False) as mock:
            mock.post("http://mcp.test/invoke/fetch").mock(
                return_value=Response(200, json={"content": "x"})
            )
            # Many more calls than the default burst (20). With no
            # agent_id, no bucket should ever be consulted.
            for i in range(50):
                r = await client.post(
                    "/v1/invoke",
                    json={
                        "tool_id": "web.fetch",
                        "arguments": {"url": f"u{i}"},
                        "cache": False,
                    },
                )
                assert r.status_code == 200, f"call {i}: {r.text}"


@pytest.mark.asyncio
async def test_invoke_with_limits_disabled_skips_enforcement(tmp_path: Path) -> None:
    """Spec mandatory: /v1/invoke with limits disabled (settings) → no limits."""
    settings = Settings(
        data_dir=tmp_path / "rl3",
        log_level="WARNING",
        log_format="console",
        backend_timeout_seconds=5.0,
        rate_limits_enabled=False,  # global kill switch
    )
    app, client = await _client_for_settings(settings)
    async with client, app.router.lifespan_context(app):
        await client.post("/v1/tools/register", json=_sample_tool())
        # Even with a tight per-agent override, enforcement is off.
        await client.post(
            "/v1/limits/agt_x",
            json={"rpm": 60, "burst": 1, "cost_cap_usd_hour": 0.0001, "cost_cap_usd_day": 0.0001},
        )

        with respx.mock(assert_all_called=False) as mock:
            mock.post("http://mcp.test/invoke/fetch").mock(
                return_value=Response(200, json={"content": "x"})
            )
            # 30 calls — would 429 with limits on, but they're off.
            for i in range(30):
                r = await client.post(
                    "/v1/invoke",
                    json={
                        "tool_id": "web.fetch",
                        "arguments": {"url": f"u{i}"},
                        "agent_id": "agt_x",
                        "cache": False,
                    },
                )
                assert r.status_code == 200, f"call {i}: {r.text}"


@pytest.mark.asyncio
async def test_real_clock_refills_after_sleep() -> None:
    """One end-to-end test against the real clock — guards against fake-clock-only bugs.

    Kept tiny (sleep 0.6s) so the suite stays fast.
    """
    # rate=10/s, capacity=2 — drain, sleep 0.6s, retry
    b = TokenBucket(rate_per_second=10.0, capacity=2)
    assert b.try_acquire(1)[0] is True
    assert b.try_acquire(1)[0] is True
    ok, retry = b.try_acquire(1)
    assert ok is False
    assert retry > 0

    time.sleep(0.6)  # 6 tokens worth of refill, capped at 2
    ok, _ = b.try_acquire(1)
    assert ok is True
