# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 The Plinth Authors
"""Tests for the workspace metrics module + /metrics endpoint."""

from __future__ import annotations

import httpx
import pytest

from plinth_workspace.metrics import (
    DEFAULT_DURATION_BUCKETS,
    Counter,
    Gauge,
    Histogram,
    MetricsRegistry,
)


# ---------------------------------------------------------------------------
# Primitive types


def test_counter_inc_default():
    c = Counter()
    c.inc()
    c.inc()
    assert c.value == 2.0


def test_counter_inc_custom_amount():
    c = Counter()
    c.inc(2.5)
    c.inc(0.5)
    assert c.value == 3.0


def test_counter_negative_inc_is_ignored():
    c = Counter()
    c.inc(5)
    c.inc(-2)  # MUST NOT decrease
    assert c.value == 5


def test_gauge_set():
    g = Gauge()
    g.set(7)
    assert g.value == 7
    g.set(3)
    assert g.value == 3


def test_gauge_inc_dec():
    g = Gauge()
    g.inc(5)
    g.dec(2)
    assert g.value == 3


def test_histogram_observes_buckets():
    h = Histogram(buckets=(0.1, 0.5, 1.0))
    h.observe(0.05)
    h.observe(0.3)
    h.observe(2.0)
    assert h.count_value == 3
    assert pytest.approx(h.sum_value) == 2.35
    # Cumulative bucket counts: 0.05 hits all three, 0.3 hits 0.5+1.0,
    # 2.0 hits none.
    assert h.counts == [1, 2, 2]


# ---------------------------------------------------------------------------
# Registry


def test_registry_build_info_set():
    r = MetricsRegistry("workspace", "1.0.0")
    text = r.render()
    assert "plinth_build_info" in text
    assert 'service="workspace"' in text
    assert 'version="1.0.0"' in text


def test_registry_counter_inc_renders():
    r = MetricsRegistry("workspace", "1.0.0")
    r.counter("plinth_kv_writes_total", {"tenant_id": "acme"}).inc(3)
    text = r.render()
    assert 'plinth_kv_writes_total{tenant_id="acme"} 3' in text


def test_registry_histogram_renders_buckets_and_counts():
    r = MetricsRegistry("gateway", "1.0.0")
    r.histogram(
        "plinth_http_request_duration_seconds",
        {"service": "gateway", "method": "GET"},
    ).observe(0.05)
    text = r.render()
    assert "plinth_http_request_duration_seconds_bucket" in text
    assert "plinth_http_request_duration_seconds_sum" in text
    assert "plinth_http_request_duration_seconds_count" in text
    assert 'le="+Inf"' in text


def test_registry_gauge_set_renders():
    r = MetricsRegistry("identity", "1.0.0")
    r.gauge("plinth_tokens_active", {"service": "identity"}).set(42)
    text = r.render()
    assert 'plinth_tokens_active{service="identity"} 42' in text


def test_registry_default_buckets_match_prometheus_convention():
    assert 0.005 in DEFAULT_DURATION_BUCKETS
    assert 10.0 in DEFAULT_DURATION_BUCKETS


def test_registry_label_escaping():
    r = MetricsRegistry("workspace", "1.0.0")
    r.counter("plinth_test", {"path": 'a"b'}).inc()
    text = r.render()
    # The escape inserts a backslash before the quote.
    assert 'a\\"b' in text


# ---------------------------------------------------------------------------
# /metrics endpoint smoke (workspace)


@pytest.mark.asyncio
async def test_metrics_endpoint_returns_text(client: httpx.AsyncClient):
    resp = await client.get("/metrics")
    assert resp.status_code == 200
    assert "version=0.0.4" in resp.headers.get("content-type", "")
    body = resp.text
    assert "plinth_build_info" in body
    assert "# TYPE plinth_http_requests_total counter" in body


@pytest.mark.asyncio
async def test_metrics_endpoint_exposes_workspace_specific_series(
    client: httpx.AsyncClient,
):
    resp = await client.get("/metrics")
    body = resp.text
    # Pre-declared series should appear even with zero observations.
    for name in (
        "plinth_kv_writes_total",
        "plinth_files_writes_total",
        "plinth_workspaces_total",
        "plinth_workflows_active",
        "plinth_workers_active",
        "plinth_load_shed_total",
    ):
        assert f"# TYPE {name}" in body, f"missing series: {name}"


@pytest.mark.asyncio
async def test_metrics_records_http_request(client: httpx.AsyncClient):
    # Exercise an endpoint to bump the http counter.
    await client.post("/v1/workspaces", json={"name": "test-ws"})
    resp = await client.get("/metrics")
    body = resp.text
    # POST counter should be present.
    assert 'method="POST"' in body
    assert "plinth_http_request_duration_seconds_sum" in body


@pytest.mark.asyncio
async def test_metrics_excludes_health_and_metrics_paths(
    client: httpx.AsyncClient,
):
    await client.get("/healthz")
    await client.get("/metrics")
    await client.get("/metrics")
    resp = await client.get("/metrics")
    body = resp.text
    # /healthz and /metrics MUST NOT appear in the per-path counter.
    assert 'path="/healthz"' not in body
    assert 'path="/metrics"' not in body


@pytest.mark.asyncio
async def test_metrics_kv_write_increments_counter(
    client: httpx.AsyncClient, workspace_id: str
):
    # Two KV writes — counter should jump by 2.
    await client.put(
        f"/v1/workspaces/{workspace_id}/kv/foo",
        json={"value": "bar"},
    )
    await client.put(
        f"/v1/workspaces/{workspace_id}/kv/foo2",
        json={"value": "bar2"},
    )
    resp = await client.get("/metrics")
    body = resp.text
    assert "plinth_kv_writes_total" in body
    # The counter line carries the numeric increment.
    found = False
    for line in body.splitlines():
        if line.startswith("plinth_kv_writes_total{") and not line.startswith("#"):
            tokens = line.rsplit(" ", 1)
            if len(tokens) == 2:
                value = tokens[1]
                # Either an int "2" or float "2.0" — accept both.
                if value.startswith(("2", "3", "4")):
                    found = True
    assert found, "expected plinth_kv_writes_total >= 2"
