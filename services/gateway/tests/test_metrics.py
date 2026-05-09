# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 The Plinth Authors
"""Tests for the gateway metrics module + /metrics endpoint."""

from __future__ import annotations

import httpx
import pytest

from plinth_gateway.metrics import (
    Counter,
    MetricsRegistry,
    metrics_response,
)


def test_counter_inc_default():
    c = Counter()
    c.inc()
    assert c.value == 1.0


def test_registry_pre_declared_series_render():
    r = MetricsRegistry("gateway", "1.0.0")
    r.declare_counter("plinth_tool_invocations_total", "test")
    text = r.render()
    assert "# TYPE plinth_tool_invocations_total counter" in text


def test_metrics_response_content_type():
    r = MetricsRegistry("gateway", "1.0.0")
    resp = metrics_response(r)
    assert "text/plain" in resp.media_type
    assert b"plinth_build_info" in resp.body


@pytest.mark.asyncio
async def test_gateway_metrics_endpoint_returns_text(client: httpx.AsyncClient):
    resp = await client.get("/metrics")
    assert resp.status_code == 200
    body = resp.text
    assert "plinth_build_info" in body
    assert 'service="gateway"' in body


@pytest.mark.asyncio
async def test_gateway_metrics_exposes_canonical_series(client: httpx.AsyncClient):
    resp = await client.get("/metrics")
    body = resp.text
    expected = (
        "plinth_tool_invocations_total",
        "plinth_tool_invocation_duration_seconds",
        "plinth_tool_invocation_cost_usd_total",
        "plinth_oauth_connections_active",
        "plinth_rate_limit_rejections_total",
        "plinth_quota_rejections_total",
        "plinth_audit_chain_verified",
        "plinth_load_shed_total",
    )
    for name in expected:
        assert f"# TYPE {name}" in body, f"missing series: {name}"


@pytest.mark.asyncio
async def test_gateway_metrics_records_http_request(client: httpx.AsyncClient):
    await client.get("/v1/tools")
    resp = await client.get("/metrics")
    body = resp.text
    assert "plinth_http_requests_total" in body
    # Some HTTP traffic must have been observed.
    assert "plinth_http_request_duration_seconds_sum" in body
