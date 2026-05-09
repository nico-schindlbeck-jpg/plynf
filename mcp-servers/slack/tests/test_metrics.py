# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 The Plinth Authors
"""Tests for the slack-mcp Prometheus metrics surface.

Mirrors the github-mcp metrics tests; the surface is intentionally identical
across MCP servers so operators can write one set of dashboards/alerts.
"""

from __future__ import annotations

import httpx
import pytest

from slack_mcp.metrics import MetricsRegistry
from slack_mcp.server import _record_mcp_invocation


async def test_metrics_endpoint_content_type(client: httpx.AsyncClient) -> None:
    resp = await client.get("/metrics")
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/plain")
    assert "version=0.0.4" in resp.headers["content-type"]


async def test_metrics_endpoint_includes_build_info(
    client: httpx.AsyncClient,
) -> None:
    resp = await client.get("/metrics")
    body = resp.text
    assert "plinth_build_info" in body
    line = next(ln for ln in body.splitlines() if ln.startswith("plinth_build_info{"))
    assert 'service="slack-mcp"' in line
    assert line.endswith(" 1")


async def test_metrics_endpoint_pre_declared_series(
    client: httpx.AsyncClient,
) -> None:
    resp = await client.get("/metrics")
    body = resp.text
    assert "# TYPE plinth_mcp_invocations_total counter" in body
    assert "# TYPE plinth_mcp_invocation_errors_total counter" in body
    assert "# TYPE plinth_mcp_invocation_duration_seconds histogram" in body


async def test_middleware_records_request_counter(
    client: httpx.AsyncClient,
) -> None:
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
    assert line is not None
    value = float(line.rsplit(" ", 1)[-1])
    assert value >= 1


async def test_middleware_excludes_metrics_path(
    client: httpx.AsyncClient,
) -> None:
    await client.get("/metrics")
    resp = await client.get("/metrics")
    assert 'path="/metrics"' not in resp.text


async def test_middleware_excludes_healthz_path(
    client: httpx.AsyncClient,
) -> None:
    await client.get("/healthz")
    resp = await client.get("/metrics")
    assert 'path="/healthz"' not in resp.text


async def test_unknown_tool_returns_404_and_metrics_still_work(
    client: httpx.AsyncClient,
) -> None:
    bad = await client.post("/invoke/no.such.tool", json={})
    assert bad.status_code == 404
    resp = await client.get("/metrics")
    assert 'status="404"' in resp.text


def test_record_mcp_invocation_ok() -> None:
    registry = MetricsRegistry("slack-mcp", "test")
    registry.declare_counter("plinth_mcp_invocations_total", "calls")
    registry.declare_counter("plinth_mcp_invocation_errors_total", "errors")
    registry.declare_histogram(
        "plinth_mcp_invocation_duration_seconds", "duration"
    )
    _record_mcp_invocation(registry, "channels.list", 0.0, ok=True)
    body = registry.render()
    assert 'plinth_mcp_invocations_total{result="ok",tool="channels.list"} 1' in body


def test_record_mcp_invocation_error_increments_error_counter() -> None:
    registry = MetricsRegistry("slack-mcp", "test")
    registry.declare_counter("plinth_mcp_invocations_total", "calls")
    registry.declare_counter("plinth_mcp_invocation_errors_total", "errors")
    registry.declare_histogram(
        "plinth_mcp_invocation_duration_seconds", "duration"
    )
    _record_mcp_invocation(registry, "messages.post", 0.0, ok=False)
    body = registry.render()
    assert (
        'plinth_mcp_invocations_total{result="error",tool="messages.post"} 1'
        in body
    )
    assert 'plinth_mcp_invocation_errors_total{tool="messages.post"} 1' in body


def test_record_mcp_invocation_swallows_none_registry() -> None:
    _record_mcp_invocation(None, "anything", 0.0, ok=True)


async def test_prometheus_format_parses_cleanly(
    client: httpx.AsyncClient,
) -> None:
    parser = pytest.importorskip("prometheus_client.parser")
    resp = await client.get("/metrics")
    families = list(parser.text_string_to_metric_families(resp.text))
    names = {f.name for f in families}
    assert "plinth_build_info" in names
    assert "plinth_http_requests" in names
    assert "plinth_http_request_duration_seconds" in names
