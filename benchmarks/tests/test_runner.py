# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 The Plinth Authors
"""Unit tests for the bench runner — percentile math, ramp, end-to-end shape.

These tests do NOT require any Plinth services to be running. We use a
local FastAPI app + ASGITransport for the integration test, and
synthetic samples for the math.
"""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI
from httpx import ASGITransport

from plinth_bench.compare import compare_runs
from plinth_bench.reporter import render_markdown_table, to_dict, write_json
from plinth_bench.runner import (
    RequestSample,
    RunnerConfig,
    WorkloadResult,
    percentile,
    run_workload,
    target_rps_at,
)


# ---------------------------------------------------------------------------
# Percentiles
# ---------------------------------------------------------------------------


def test_percentile_empty_returns_zero() -> None:
    assert percentile([], 0.5) == 0.0


def test_percentile_known_values() -> None:
    """100-element ramp 1..100 has p50≈50.5, p95≈95.05, p99≈99.01."""

    samples = list(range(1, 101))  # 1..100
    p50 = percentile(samples, 0.50)
    p95 = percentile(samples, 0.95)
    p99 = percentile(samples, 0.99)
    assert 50.0 <= p50 <= 51.0
    # Linear interpolation: rank = 0.95 * 99 = 94.05 → 95.05
    assert 95.0 <= p95 <= 96.0
    assert 99.0 <= p99 <= 100.0


def test_percentile_extremes_match_min_max() -> None:
    samples = [3.0, 1.0, 4.0, 1.0, 5.0, 9.0, 2.0, 6.0]
    assert percentile(samples, 0.0) == 1.0
    assert percentile(samples, 1.0) == 9.0


# ---------------------------------------------------------------------------
# Ramp curve
# ---------------------------------------------------------------------------


def test_ramp_curve_phases() -> None:
    """Ramp linear up, hold flat, ramp linear down, then zero."""

    rps = lambda t: target_rps_at(  # noqa: E731
        t,
        ramp_seconds=10,
        hold_seconds=10,
        cooldown_seconds=10,
        target_rps=110,
        initial_rps=10,
    )
    # t=0 → initial.
    assert rps(0) == pytest.approx(10.0)
    # t=10 → at target after ramp.
    assert rps(10) == pytest.approx(110.0)
    # Mid hold.
    assert rps(15) == pytest.approx(110.0)
    # End of hold.
    assert rps(19.9999) == pytest.approx(110.0, abs=0.01)
    # Mid cooldown — should be (110 - 50) = 60? Actually after 5s of cooldown
    # the rps is target - (target - initial) * (5/10) = 110 - 50 = 60.
    assert rps(25) == pytest.approx(60.0)
    # After cooldown: 0.
    assert rps(30) == pytest.approx(0.0)
    assert rps(40) == pytest.approx(0.0)


def test_ramp_zero_phase_no_div_zero() -> None:
    """Zero ramp + zero cooldown shouldn't divide by zero."""

    assert target_rps_at(
        0, ramp_seconds=0, hold_seconds=5, cooldown_seconds=0,
        target_rps=100,
    ) == pytest.approx(100.0)


# ---------------------------------------------------------------------------
# Runner end-to-end against a tiny FastAPI app
# ---------------------------------------------------------------------------


def _build_test_app() -> FastAPI:
    """Tiny in-process FastAPI app used as a target.

    Two endpoints:
      * ``/echo`` — fast 200
      * ``/error`` — always 500
    """

    app = FastAPI()

    @app.get("/echo")
    async def echo() -> dict:
        return {"ok": True}

    @app.get("/error")
    async def err():
        from fastapi import HTTPException

        raise HTTPException(status_code=500, detail="boom")

    return app


@pytest.mark.asyncio
async def test_runner_short_run_produces_well_formed_result() -> None:
    """A short ramp+hold against an in-process app produces a sane result."""

    app = _build_test_app()
    transport = ASGITransport(app=app)
    client = httpx.AsyncClient(transport=transport, base_url="http://t")

    async def workload(c: httpx.AsyncClient) -> RequestSample:
        import time

        t0 = time.perf_counter()
        r = await c.get("/echo")
        dur = (time.perf_counter() - t0) * 1000.0
        return RequestSample(
            duration_ms=dur,
            status_code=r.status_code,
            error=None if r.status_code < 400 else f"http_{r.status_code}",
        )

    config = RunnerConfig(
        target_rps=10,
        ramp_seconds=1,
        hold_seconds=1,
        cooldown_seconds=0,
        initial_rps=5,
        inflight_cap=50,
        request_timeout_seconds=5.0,
        http2=False,  # ASGITransport doesn't speak HTTP/2.
    )

    try:
        result = await run_workload(
            workload_name="test_echo",
            target_url="http://t",
            workload=workload,
            config=config,
            client=client,
        )
    finally:
        await client.aclose()

    assert isinstance(result, WorkloadResult)
    assert result.workload == "test_echo"
    assert result.target_rps == 10
    assert result.total_requests > 0
    assert result.successful == result.total_requests
    assert result.failed == 0
    assert result.error_rate == 0.0
    # Percentiles should be present and ordered.
    lat = result.latency_ms
    assert lat["p50"] >= 0
    assert lat["p95"] >= lat["p50"]
    assert lat["p99"] >= lat["p95"]
    assert lat["max"] >= lat["p99"]
    # Buckets are 1 per second of (ramp + hold + cooldown) = 2.
    assert len(result.buckets) == 2
    assert all(b.t == i for i, b in enumerate(result.buckets))


@pytest.mark.asyncio
async def test_runner_classifies_errors() -> None:
    """Hitting the always-500 endpoint shows up in errors_by_type."""

    app = _build_test_app()
    transport = ASGITransport(app=app)
    client = httpx.AsyncClient(transport=transport, base_url="http://t")

    async def workload(c: httpx.AsyncClient) -> RequestSample:
        r = await c.get("/error")
        return RequestSample(
            duration_ms=1.0,
            status_code=r.status_code,
            error=None if r.status_code < 400 else f"http_{r.status_code}",
        )

    config = RunnerConfig(
        target_rps=5,
        ramp_seconds=1,
        hold_seconds=1,
        cooldown_seconds=0,
        initial_rps=3,
        http2=False,
    )

    try:
        result = await run_workload(
            workload_name="test_err",
            target_url="http://t",
            workload=workload,
            config=config,
            client=client,
        )
    finally:
        await client.aclose()

    assert result.successful == 0
    assert result.failed == result.total_requests
    assert result.error_rate == 1.0
    assert "http_500" in result.errors_by_type
    assert result.errors_by_type["http_500"] == result.total_requests


# ---------------------------------------------------------------------------
# Reporter + compare
# ---------------------------------------------------------------------------


def _synthetic_result(name: str, p50: float, p95: float) -> WorkloadResult:
    return WorkloadResult(
        workload=name,
        target_url="http://t",
        target_rps=100,
        ramp_seconds=1,
        hold_seconds=1,
        cooldown_seconds=0,
        started_at="2026-05-07T00:00:00+00:00",
        finished_at="2026-05-07T00:00:02+00:00",
        total_requests=200,
        successful=199,
        failed=1,
        error_rate=0.005,
        latency_ms={"p50": p50, "p95": p95, "p99": p95 + 5, "max": p95 + 30, "mean": p50 + 1},
        buckets=[],
        errors_by_type={"timeout": 1},
    )


def test_write_json_roundtrip(tmp_path: Path) -> None:
    res = _synthetic_result("workspace_kv", 4.2, 18.7)
    out = write_json(res, tmp_path / "x.json")
    assert out.exists()
    data = json.loads(out.read_text())
    assert data["workload"] == "workspace_kv"
    assert data["latency_ms"]["p50"] == 4.2
    assert data["error_rate"] == 0.005


def test_render_markdown_table_columns() -> None:
    md = render_markdown_table([
        _synthetic_result("workspace_kv", 4.2, 18.7),
        _synthetic_result("gateway_invoke_cached", 2.1, 9.4),
    ])
    assert "| Workload | RPS | p50 | p95 | p99 | error_rate |" in md
    assert "workspace_kv" in md
    assert "gateway_invoke_cached" in md
    # Two data rows + 2 header rows = 4 lines + trailing newline.
    assert md.count("\n") == 4


def test_compare_runs_produces_markdown(tmp_path: Path) -> None:
    a = tmp_path / "A.json"
    b = tmp_path / "B.json"
    write_json(_synthetic_result("workspace_kv", 4.2, 18.7), a)
    write_json(_synthetic_result("workspace_kv", 3.8, 19.2), b)

    md = compare_runs(a, b)
    # Headers.
    assert "| Workload" in md
    # Includes rows for p50, p95, p99, err_pct.
    assert "p50 ms" in md
    assert "p95 ms" in md
    assert "p99 ms" in md
    assert "err_pct" in md
    # Δ shows -9.5% for p50 (3.8 vs 4.2).
    assert "-9.5%" in md or "-9.6%" in md  # rounding tolerance


def test_compare_handles_suite_lists(tmp_path: Path) -> None:
    """Compare can ingest a JSON list of runs (a suite)."""

    a = tmp_path / "suite_a.json"
    b = tmp_path / "suite_b.json"
    runs_a = [
        to_dict(_synthetic_result("workspace_kv", 4.2, 18.7)),
        to_dict(_synthetic_result("gateway_invoke_cached", 2.1, 9.4)),
    ]
    runs_b = [
        to_dict(_synthetic_result("workspace_kv", 3.5, 17.0)),
        to_dict(_synthetic_result("gateway_invoke_cached", 2.0, 9.0)),
    ]
    a.write_text(json.dumps(runs_a))
    b.write_text(json.dumps(runs_b))

    md = compare_runs(a, b)
    assert "workspace_kv" in md
    assert "gateway_invoke_cached" in md
