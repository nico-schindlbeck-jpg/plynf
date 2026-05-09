# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 The Plinth Authors
"""Tests for the dashboard metrics module + /metrics endpoint + timeseries."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
import respx
from httpx import AsyncClient, Response

from plinth_dashboard.metrics import MetricsRegistry
from plinth_dashboard.timeseries import (
    InvalidMetricError,
    InvalidWindowError,
    build_timeseries,
)


def test_dashboard_registry_has_canonical_series():
    r = MetricsRegistry("dashboard", "0.1.0")
    r.declare_counter("plinth_dashboard_polls_total", "test")
    r.declare_counter("plinth_dashboard_upstream_failures_total", "test")
    text = r.render()
    assert "# TYPE plinth_dashboard_polls_total" in text
    assert "# TYPE plinth_dashboard_upstream_failures_total" in text


@pytest.mark.asyncio
async def test_dashboard_metrics_endpoint(client: AsyncClient):
    resp = await client.get("/metrics")
    assert resp.status_code == 200
    body = resp.text
    assert "plinth_build_info" in body
    assert 'service="dashboard"' in body


@pytest.mark.asyncio
@respx.mock
async def test_timeseries_endpoint_returns_canonical_shape(client: AsyncClient):
    respx.get("http://gateway.test/v1/audit").mock(
        return_value=Response(
            200,
            json={
                "events": [
                    {
                        "id": "evt_1",
                        "tool_id": "weather.lookup",
                        "timestamp": _iso(_now() - timedelta(hours=1)),
                        "duration_ms": 42,
                        "cached": False,
                        "cost_estimate_usd": 0.001,
                    },
                    {
                        "id": "evt_2",
                        "tool_id": "weather.lookup",
                        "timestamp": _iso(_now() - timedelta(hours=2)),
                        "duration_ms": 70,
                        "cached": True,
                        "cost_estimate_usd": 0.0,
                    },
                ]
            },
        )
    )
    resp = await client.get("/api/timeseries?metric=cost&window=24h")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["metric"] == "cost"
    assert body["window"] == "24h"
    assert body["unit"] == "USD"
    assert len(body["points"]) == 24
    # Each point has the documented shape.
    for p in body["points"]:
        assert "t" in p and "value" in p
    assert {"min", "max", "avg"} == set(body["summary"].keys())


@pytest.mark.asyncio
@respx.mock
async def test_timeseries_invalid_metric_returns_400(client: AsyncClient):
    respx.get("http://gateway.test/v1/audit").mock(
        return_value=Response(200, json={"events": []})
    )
    resp = await client.get("/api/timeseries?metric=nonsense&window=24h")
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "INVALID_ARGUMENTS"


def test_build_timeseries_buckets_24h_default():
    out = build_timeseries([], metric="cost", window="24h")
    assert out["buckets"] == 24
    assert len(out["points"]) == 24


def test_build_timeseries_invalid_metric():
    with pytest.raises(InvalidMetricError):
        build_timeseries([], metric="bogus", window="24h")


def test_build_timeseries_invalid_window():
    with pytest.raises(InvalidWindowError):
        build_timeseries([], metric="cost", window="99d")


def test_build_timeseries_cost_aggregates_correctly():
    now = datetime(2026, 5, 8, 12, 0, 0, tzinfo=timezone.utc)
    events = [
        {
            "timestamp": (now - timedelta(minutes=30)).isoformat().replace("+00:00", "Z"),
            "cost_estimate_usd": 0.10,
        },
        {
            "timestamp": (now - timedelta(minutes=20)).isoformat().replace("+00:00", "Z"),
            "cost_estimate_usd": 0.05,
        },
    ]
    out = build_timeseries(events, metric="cost", window="1h", buckets=4, now=now)
    # Sum of all buckets equals sum of inputs.
    total = sum(p["value"] for p in out["points"])
    assert pytest.approx(total) == 0.15


def test_build_timeseries_error_rate_is_percent():
    now = datetime(2026, 5, 8, 12, 0, 0, tzinfo=timezone.utc)
    ts = (now - timedelta(minutes=10)).isoformat().replace("+00:00", "Z")
    events = [
        {"timestamp": ts, "error": "boom"},
        {"timestamp": ts, "error": None},
        {"timestamp": ts, "error": None},
        {"timestamp": ts, "error": None},
    ]
    out = build_timeseries(events, metric="error_rate", window="1h", buckets=1, now=now)
    # 25% errors, in percent.
    assert pytest.approx(out["points"][0]["value"]) == 25.0
    assert out["unit"] == "percent"


def test_build_timeseries_cache_hit_ratio_is_percent():
    now = datetime(2026, 5, 8, 12, 0, 0, tzinfo=timezone.utc)
    ts = (now - timedelta(minutes=5)).isoformat().replace("+00:00", "Z")
    events = [
        {"timestamp": ts, "cached": True},
        {"timestamp": ts, "cached": True},
        {"timestamp": ts, "cached": False},
    ]
    out = build_timeseries(
        events, metric="cache_hit_ratio", window="1h", buckets=1, now=now
    )
    # 2/3 = 66.67%
    assert pytest.approx(out["points"][0]["value"], rel=1e-3) == 66.667


def test_build_timeseries_summary_min_max_avg():
    now = datetime(2026, 5, 8, 12, 0, 0, tzinfo=timezone.utc)
    # Spread observations across two of four buckets so min < max.
    events = [
        {
            "timestamp": (now - timedelta(minutes=50)).isoformat().replace("+00:00", "Z"),
            "cost_estimate_usd": 0.01,
        },
        {
            "timestamp": (now - timedelta(minutes=10)).isoformat().replace("+00:00", "Z"),
            "cost_estimate_usd": 1.0,
        },
    ]
    out = build_timeseries(events, metric="cost", window="1h", buckets=4, now=now)
    summary = out["summary"]
    assert summary["min"] == 0.0
    assert summary["max"] == 1.0
    assert summary["avg"] >= 0.0


# ---------------------------------------------------------------------------
# Helpers


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.isoformat().replace("+00:00", "Z")
