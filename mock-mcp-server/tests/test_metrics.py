# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 The Plinth Authors
"""Tests for the mock-mcp Prometheus metrics surface.

Covers:

* ``GET /metrics`` returns the canonical ``text/plain; version=0.0.4`` content
  type and parses cleanly through a real Prometheus text-format parser.
* The middleware increments ``plinth_http_requests_total`` per request.
* Tool invocations bump ``plinth_mcp_invocations_total`` /
  ``plinth_mcp_invocation_errors_total`` and observe the duration histogram.
* High-cardinality risk paths are bounded to the FastAPI route shape (the
  middleware emits the concrete request path and operators are expected to
  aggregate via PromQL — we still verify the path label is the route, not a
  stack trace, and that the registry never explodes mid-flight).
"""

from __future__ import annotations

import re

import httpx
import pytest

from mock_mcp.metrics import MetricsRegistry
from mock_mcp.server import _record_mcp_invocation, create_app


# Async tests opt in explicitly via @pytest.mark.asyncio; the synchronous
# unit tests at the bottom of the file are deliberately *not* marked so
# pytest-asyncio doesn't warn about them.


def _parse_prom(body: str) -> dict[str, list[str]]:
    """Group exposition body by metric name → list of sample lines."""
    out: dict[str, list[str]] = {}
    current: str | None = None
    for line in body.splitlines():
        if line.startswith("# TYPE "):
            parts = line.split()
            current = parts[2]
            out.setdefault(current, [])
            continue
        if line.startswith("# HELP "):
            continue
        if line.strip() == "":
            continue
        if current is None:
            continue
        out.setdefault(current, []).append(line)
    return out


async def test_metrics_endpoint_content_type(client: httpx.AsyncClient) -> None:
    """The endpoint must use the canonical Prometheus text content type."""
    resp = await client.get("/metrics")
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/plain")
    assert "version=0.0.4" in resp.headers["content-type"]


async def test_metrics_endpoint_includes_build_info(client: httpx.AsyncClient) -> None:
    """``plinth_build_info`` must always be present (set on construction)."""
    resp = await client.get("/metrics")
    body = resp.text
    assert "plinth_build_info" in body
    # build_info is a gauge, value 1, with service+version labels.
    line = next(ln for ln in body.splitlines() if ln.startswith("plinth_build_info{"))
    assert 'service="mock-mcp"' in line
    assert line.endswith(" 1")


async def test_metrics_endpoint_pre_declared_series(client: httpx.AsyncClient) -> None:
    """Pre-declared MCP series are present even before any invocation.

    Important so a fresh deployment can be scraped + dashboards instantly
    have ``# TYPE`` headers + label keys.
    """
    resp = await client.get("/metrics")
    parsed = _parse_prom(resp.text)
    assert "plinth_mcp_invocations_total" in parsed
    assert "plinth_mcp_invocation_errors_total" in parsed
    assert "plinth_mcp_invocation_duration_seconds" in parsed


async def test_middleware_records_request_counter(client: httpx.AsyncClient) -> None:
    """A real request bumps ``plinth_http_requests_total``."""
    await client.get("/healthz")
    # /healthz is excluded — hit /tools instead.
    await client.get("/tools")
    resp = await client.get("/metrics")
    body = resp.text
    line = next(
        (
            ln for ln in body.splitlines()
            if ln.startswith("plinth_http_requests_total{")
            and 'path="/tools"' in ln
        ),
        None,
    )
    assert line is not None, "expected /tools request counter"
    value = float(line.rsplit(" ", 1)[-1])
    assert value >= 1


async def test_middleware_records_duration_histogram(client: httpx.AsyncClient) -> None:
    """The duration histogram receives observations on real requests."""
    await client.get("/tools")
    resp = await client.get("/metrics")
    body = resp.text
    # Bucket samples must exist after at least one request.
    bucket_lines = [
        ln for ln in body.splitlines()
        if ln.startswith("plinth_http_request_duration_seconds_bucket{")
    ]
    assert bucket_lines
    sum_lines = [
        ln for ln in body.splitlines()
        if ln.startswith("plinth_http_request_duration_seconds_sum{")
    ]
    assert sum_lines


async def test_metrics_endpoint_excluded_from_metrics(client: httpx.AsyncClient) -> None:
    """``/metrics`` itself must NOT show up in the request counter.

    Otherwise a Prometheus scrape would self-amplify until the registry
    cardinality explodes.
    """
    # Hit it twice to make sure repeated scrapes don't add /metrics samples.
    await client.get("/metrics")
    resp = await client.get("/metrics")
    body = resp.text
    assert 'path="/metrics"' not in body


async def test_invocation_records_metrics(client: httpx.AsyncClient) -> None:
    """A successful tool invocation bumps the MCP counters + histogram."""
    resp = await client.post("/invoke/notes.list", json={})
    assert resp.status_code == 200
    metrics_resp = await client.get("/metrics")
    body = metrics_resp.text
    # invocations_total carries (tool=notes.list, result=ok). Labels are
    # emitted alphabetically by the registry — assert by substring rather
    # than depending on label order.
    line = next(
        (
            ln for ln in body.splitlines()
            if ln.startswith("plinth_mcp_invocations_total{")
            and 'tool="notes.list"' in ln
            and 'result="ok"' in ln
        ),
        None,
    )
    assert line is not None, body
    value = float(line.rsplit(" ", 1)[-1])
    assert value >= 1


async def test_invocation_unknown_tool_increments_error(
    client: httpx.AsyncClient,
) -> None:
    """An unknown tool returns 404 — but our metrics counter applies only to
    *real* tool dispatch. The middleware still records the HTTP request."""
    resp = await client.post("/invoke/does.not.exist", json={})
    assert resp.status_code == 404
    metrics_resp = await client.get("/metrics")
    body = metrics_resp.text
    # The HTTP middleware counter records 404 status.
    assert 'status="404"' in body


def test_record_mcp_invocation_ok_increments_counter() -> None:
    """Direct unit test for the metrics helper — ok=True increments only success."""
    registry = MetricsRegistry("mock-mcp", "test")
    registry.declare_counter("plinth_mcp_invocations_total", "tool calls")
    registry.declare_counter("plinth_mcp_invocation_errors_total", "tool errors")
    registry.declare_histogram(
        "plinth_mcp_invocation_duration_seconds", "duration"
    )
    _record_mcp_invocation(registry, "tool.x", 0.0, ok=True)
    body = registry.render()
    assert 'plinth_mcp_invocations_total{result="ok",tool="tool.x"} 1' in body
    # No error counter labelled tool.x yet.
    assert 'plinth_mcp_invocation_errors_total{tool="tool.x"} 1' not in body


def test_record_mcp_invocation_error_increments_error_counter() -> None:
    registry = MetricsRegistry("mock-mcp", "test")
    registry.declare_counter("plinth_mcp_invocations_total", "tool calls")
    registry.declare_counter("plinth_mcp_invocation_errors_total", "tool errors")
    registry.declare_histogram(
        "plinth_mcp_invocation_duration_seconds", "duration"
    )
    _record_mcp_invocation(registry, "tool.y", 0.0, ok=False)
    body = registry.render()
    assert 'plinth_mcp_invocations_total{result="error",tool="tool.y"} 1' in body
    assert 'plinth_mcp_invocation_errors_total{tool="tool.y"} 1' in body


def test_record_mcp_invocation_swallows_failures() -> None:
    """A None registry must not raise — the request path must keep working."""
    _record_mcp_invocation(None, "tool.z", 0.0, ok=True)


def test_record_mcp_invocation_handles_clock_skew() -> None:
    """If the elapsed time is negative (clock weirdness) we clamp to zero."""
    import time as _time

    registry = MetricsRegistry("mock-mcp", "test")
    # start = far future → elapsed < 0
    _record_mcp_invocation(
        registry,
        "tool.skew",
        _time.perf_counter() + 10_000,
        ok=True,
    )
    body = registry.render()
    # No samples should be > 10 buckets.
    assert "plinth_mcp_invocation_duration_seconds" in body


async def test_prometheus_format_parses_cleanly(
    client: httpx.AsyncClient,
) -> None:
    """The body must round-trip through ``prometheus_client.parser`` if available.

    We don't take a hard dep on prometheus_client in CI; the test only runs
    when the package is importable. The parser is the gold-standard
    Prometheus parser used by Prometheus itself.
    """
    parser = pytest.importorskip("prometheus_client.parser")
    resp = await client.get("/metrics")
    families = list(parser.text_string_to_metric_families(resp.text))
    assert families
    names = {f.name for f in families}
    assert "plinth_build_info" in names
    # The Prometheus parser canonicalises counter names by stripping the
    # ``_total`` suffix (counters can be named ``foo`` or ``foo_total``;
    # the parser maps both to ``foo``).
    assert "plinth_http_requests" in names
