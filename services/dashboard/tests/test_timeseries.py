# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 The Plinth Authors
"""Unit + integration tests for the dashboard time-series builder.

Three layers:

* :func:`build_timeseries` — pure function over a list of audit events.
  Easiest to verify and the spec says aggregation correctness on sample
  audit data is required.
* ``GET /api/timeseries`` — the FastAPI route. Tested via respx-mocked
  upstream gateway audit calls.
* Edge cases — invalid metric/window, empty event lists, bucket clamping.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

import httpx
import pytest
import respx

from plinth_dashboard.timeseries import (
    InvalidMetricError,
    InvalidWindowError,
    build_timeseries,
)


# ---------------------------------------------------------------------------
# build_timeseries — pure function tests


def _evt(
    *,
    timestamp: datetime,
    cost: float = 0.0,
    duration_ms: int = 0,
    error: str | None = None,
    cached: bool = False,
    agent_id: str | None = None,
) -> dict[str, Any]:
    """Build a synthetic audit event matching the gateway's ``/v1/audit`` shape."""
    out: dict[str, Any] = {
        "timestamp": timestamp.isoformat().replace("+00:00", "Z"),
        "cost_estimate_usd": cost,
        "duration_ms": duration_ms,
        "cached": cached,
    }
    if error is not None:
        out["error"] = error
    if agent_id is not None:
        out["agent_id"] = agent_id
    return out


def test_cost_aggregation_sums_per_bucket() -> None:
    now = datetime(2026, 5, 9, 12, 0, 0, tzinfo=timezone.utc)
    events = [
        _evt(timestamp=now - timedelta(minutes=2), cost=0.10),
        _evt(timestamp=now - timedelta(minutes=2), cost=0.20),
        _evt(timestamp=now - timedelta(minutes=30), cost=0.50),
    ]
    out = build_timeseries(events, metric="cost", window="1h", now=now)
    # Sum of all costs is 0.80
    assert out["unit"] == "USD"
    assert out["metric"] == "cost"
    total = sum(p["value"] for p in out["points"])
    assert abs(total - 0.80) < 1e-6


def test_latency_p99_uses_percentile() -> None:
    now = datetime(2026, 5, 9, 12, 0, 0, tzinfo=timezone.utc)
    # 100 events all at the same minute — p99 should be the 99th element.
    events = [
        _evt(timestamp=now - timedelta(seconds=30), duration_ms=ms)
        for ms in range(100)  # 0..99 ms
    ]
    out = build_timeseries(events, metric="latency_p99", window="1h", now=now)
    last_min = next(
        p for p in reversed(out["points"]) if p["value"] > 0
    )
    # p99 of 0..99 → ~98.01 (linear interpolation)
    assert last_min["value"] >= 98.0
    assert last_min["value"] <= 99.0


def test_error_rate_percentages() -> None:
    now = datetime(2026, 5, 9, 12, 0, 0, tzinfo=timezone.utc)
    events = [
        _evt(timestamp=now - timedelta(seconds=30), error=None),
        _evt(timestamp=now - timedelta(seconds=30), error=None),
        _evt(timestamp=now - timedelta(seconds=30), error="boom"),
        _evt(timestamp=now - timedelta(seconds=30), error="boom"),
    ]
    out = build_timeseries(events, metric="error_rate", window="1h", now=now)
    # 2 of 4 are errors → 50%.
    last_min = next(p for p in reversed(out["points"]) if p["value"] > 0)
    assert abs(last_min["value"] - 50.0) < 1e-6
    assert out["unit"] == "percent"


def test_cache_hit_ratio_percentages() -> None:
    now = datetime(2026, 5, 9, 12, 0, 0, tzinfo=timezone.utc)
    events = [
        _evt(timestamp=now - timedelta(seconds=30), cached=True),
        _evt(timestamp=now - timedelta(seconds=30), cached=True),
        _evt(timestamp=now - timedelta(seconds=30), cached=False),
        _evt(timestamp=now - timedelta(seconds=30), cached=False),
    ]
    out = build_timeseries(events, metric="cache_hit_ratio", window="1h", now=now)
    last_min = next(p for p in reversed(out["points"]) if p["value"] > 0)
    assert abs(last_min["value"] - 50.0) < 1e-6


def test_active_workers_distinct_agents() -> None:
    now = datetime(2026, 5, 9, 12, 0, 0, tzinfo=timezone.utc)
    events = [
        _evt(timestamp=now - timedelta(seconds=30), agent_id="agent-a"),
        _evt(timestamp=now - timedelta(seconds=30), agent_id="agent-a"),
        _evt(timestamp=now - timedelta(seconds=30), agent_id="agent-b"),
    ]
    out = build_timeseries(events, metric="active_workers", window="1h", now=now)
    # In the latest minute we should see 2 distinct agents.
    last_min = next(p for p in reversed(out["points"]) if p["value"] > 0)
    assert last_min["value"] == 2.0


def test_tool_calls_per_minute_counts() -> None:
    now = datetime(2026, 5, 9, 12, 0, 0, tzinfo=timezone.utc)
    events = [_evt(timestamp=now - timedelta(seconds=30)) for _ in range(7)]
    out = build_timeseries(
        events, metric="tool_calls_per_minute", window="1h", now=now
    )
    total = sum(p["value"] for p in out["points"])
    assert total == 7.0


def test_empty_events_produces_zero_points() -> None:
    now = datetime(2026, 5, 9, 12, 0, 0, tzinfo=timezone.utc)
    out = build_timeseries([], metric="cost", window="1h", now=now)
    assert all(p["value"] == 0.0 for p in out["points"])
    assert out["summary"]["min"] == 0.0
    assert out["summary"]["max"] == 0.0
    assert out["summary"]["avg"] == 0.0


def test_invalid_metric_raises() -> None:
    with pytest.raises(InvalidMetricError):
        build_timeseries([], metric="not-a-metric", window="1h")


def test_invalid_window_raises() -> None:
    with pytest.raises(InvalidWindowError):
        build_timeseries([], metric="cost", window="42d")


def test_bucket_count_clamped_to_max() -> None:
    """Bucket count > 1000 is clamped to avoid massive payloads."""
    now = datetime(2026, 5, 9, 12, 0, 0, tzinfo=timezone.utc)
    out = build_timeseries(
        [], metric="cost", window="24h", buckets=10_000, now=now
    )
    assert out["buckets"] == 1000
    assert len(out["points"]) == 1000


def test_bucket_count_floor_one() -> None:
    """Bucket count <= 0 falls back to the window default."""
    now = datetime(2026, 5, 9, 12, 0, 0, tzinfo=timezone.utc)
    out = build_timeseries([], metric="cost", window="24h", buckets=0, now=now)
    assert out["buckets"] == 24


def test_window_24h_default_buckets() -> None:
    out = build_timeseries([], metric="cost", window="24h")
    assert out["buckets"] == 24


def test_window_7d_default_buckets() -> None:
    out = build_timeseries([], metric="cost", window="7d")
    assert out["buckets"] == 168


def test_events_outside_window_are_ignored() -> None:
    now = datetime(2026, 5, 9, 12, 0, 0, tzinfo=timezone.utc)
    events = [
        _evt(timestamp=now - timedelta(days=10), cost=99.99),  # outside
        _evt(timestamp=now - timedelta(minutes=30), cost=0.10),  # inside
    ]
    out = build_timeseries(events, metric="cost", window="1h", now=now)
    total = sum(p["value"] for p in out["points"])
    assert abs(total - 0.10) < 1e-6


def test_events_with_invalid_timestamps_are_dropped() -> None:
    now = datetime(2026, 5, 9, 12, 0, 0, tzinfo=timezone.utc)
    events = [
        {"timestamp": "garbage", "cost_estimate_usd": 99.0},
        {"timestamp": None, "cost_estimate_usd": 99.0},
        _evt(timestamp=now - timedelta(minutes=30), cost=0.10),
    ]
    out = build_timeseries(events, metric="cost", window="1h", now=now)
    total = sum(p["value"] for p in out["points"])
    assert abs(total - 0.10) < 1e-6


def test_summary_present_in_payload() -> None:
    now = datetime(2026, 5, 9, 12, 0, 0, tzinfo=timezone.utc)
    events = [
        _evt(timestamp=now - timedelta(minutes=2), cost=0.30),
        _evt(timestamp=now - timedelta(minutes=10), cost=0.10),
    ]
    out = build_timeseries(events, metric="cost", window="1h", now=now)
    assert "summary" in out
    assert {"min", "max", "avg"} <= set(out["summary"].keys())


# ---------------------------------------------------------------------------
# /api/timeseries integration tests
# (pytest-asyncio is in ``auto`` mode for this package, so ``async def``
# tests are picked up without an explicit marker.)


def _audit_payload(events: list[dict[str, Any]]) -> dict[str, Any]:
    return {"events": events}


async def test_api_timeseries_cost_returns_payload(
    client: httpx.AsyncClient,
    settings,
) -> None:
    now = datetime.now(timezone.utc)
    events = [
        _evt(timestamp=now - timedelta(minutes=2), cost=0.10),
        _evt(timestamp=now - timedelta(minutes=10), cost=0.20),
    ]
    with respx.mock(base_url=settings.gateway_url) as mock:
        mock.get("/v1/audit").mock(
            return_value=httpx.Response(200, json=_audit_payload(events))
        )
        resp = await client.get(
            "/api/timeseries", params={"metric": "cost", "window": "1h"}
        )
    assert resp.status_code == 200
    data = resp.json()
    assert data["metric"] == "cost"
    assert data["window"] == "1h"
    assert data["unit"] == "USD"
    assert isinstance(data["points"], list)
    assert len(data["points"]) == 60


async def test_api_timeseries_invalid_metric_returns_400(
    client: httpx.AsyncClient,
    settings,
) -> None:
    with respx.mock(base_url=settings.gateway_url) as mock:
        mock.get("/v1/audit").mock(
            return_value=httpx.Response(200, json=_audit_payload([]))
        )
        resp = await client.get("/api/timeseries", params={"metric": "bogus"})
    assert resp.status_code == 400
    body = resp.json()
    assert body["error"]["code"] == "INVALID_ARGUMENTS"


async def test_api_timeseries_invalid_window_returns_400(
    client: httpx.AsyncClient,
    settings,
) -> None:
    with respx.mock(base_url=settings.gateway_url) as mock:
        mock.get("/v1/audit").mock(
            return_value=httpx.Response(200, json=_audit_payload([]))
        )
        resp = await client.get(
            "/api/timeseries", params={"metric": "cost", "window": "99h"}
        )
    assert resp.status_code == 400


async def test_api_timeseries_upstream_error_502(
    client: httpx.AsyncClient,
    settings,
) -> None:
    with respx.mock(base_url=settings.gateway_url) as mock:
        mock.get("/v1/audit").mock(
            return_value=httpx.Response(503, json={})
        )
        resp = await client.get("/api/timeseries", params={"metric": "cost"})
    assert resp.status_code == 503  # propagated upstream status


async def test_api_timeseries_upstream_unreachable_502(
    client: httpx.AsyncClient,
    settings,
) -> None:
    with respx.mock(base_url=settings.gateway_url) as mock:
        mock.get("/v1/audit").mock(
            side_effect=httpx.ConnectError("connection refused")
        )
        resp = await client.get("/api/timeseries", params={"metric": "cost"})
    assert resp.status_code == 502
    body = resp.json()
    assert body["error"]["code"] == "UPSTREAM_UNREACHABLE"


async def test_api_timeseries_default_window_24h(
    client: httpx.AsyncClient,
    settings,
) -> None:
    with respx.mock(base_url=settings.gateway_url) as mock:
        mock.get("/v1/audit").mock(
            return_value=httpx.Response(200, json=_audit_payload([]))
        )
        resp = await client.get("/api/timeseries", params={"metric": "cost"})
    assert resp.status_code == 200
    assert resp.json()["window"] == "24h"
    # Default 24h ⇒ 24 buckets.
    assert resp.json()["buckets"] == 24


async def test_api_timeseries_explicit_buckets(
    client: httpx.AsyncClient,
    settings,
) -> None:
    with respx.mock(base_url=settings.gateway_url) as mock:
        mock.get("/v1/audit").mock(
            return_value=httpx.Response(200, json=_audit_payload([]))
        )
        resp = await client.get(
            "/api/timeseries",
            params={"metric": "cost", "window": "1h", "buckets": "12"},
        )
    assert resp.status_code == 200
    assert resp.json()["buckets"] == 12


async def test_api_timeseries_garbage_buckets_falls_back_to_default(
    client: httpx.AsyncClient,
    settings,
) -> None:
    """A non-integer buckets value silently uses the window default."""
    with respx.mock(base_url=settings.gateway_url) as mock:
        mock.get("/v1/audit").mock(
            return_value=httpx.Response(200, json=_audit_payload([]))
        )
        resp = await client.get(
            "/api/timeseries",
            params={"metric": "cost", "window": "1h", "buckets": "not-an-int"},
        )
    assert resp.status_code == 200
    assert resp.json()["buckets"] == 60
