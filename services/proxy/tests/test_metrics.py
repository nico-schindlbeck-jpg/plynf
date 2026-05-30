# SPDX-License-Identifier: Apache-2.0
"""Tests for the Prometheus /metrics exposition endpoint and renderer."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from plinth_proxy.api import create_app
from plinth_proxy.metrics import (
    CONTENT_TYPE,
    _escape_label_value,
    _fmt,
    render_metrics,
)
from plinth_proxy.savings import make_event
from plinth_proxy.settings import ProxySettings
from plinth_proxy.tier_gate import TierGate

# ---------------------------------------------------------------------------
# Tiny exposition parser (robust assertions without prometheus_client)
# ---------------------------------------------------------------------------


def _parse(text: str) -> tuple[dict[str, list[tuple[dict[str, str], str]]], dict[str, str]]:
    """Parse exposition text → ({name: [(labels, value), ...]}, {name: type})."""
    samples: dict[str, list[tuple[dict[str, str], str]]] = {}
    types: dict[str, str] = {}
    for line in text.splitlines():
        if not line or line.startswith("# HELP"):
            continue
        if line.startswith("# TYPE"):
            parts = line.split(maxsplit=3)
            types[parts[2]] = parts[3]
            continue
        if "{" in line:
            name, rest = line.split("{", 1)
            label_part, value = rest.rsplit("}", 1)
            labels: dict[str, str] = {}
            if label_part:
                for kv in label_part.split(","):
                    k, v = kv.split("=", 1)
                    labels[k] = v.strip().strip('"')
        else:
            name, value = line.split(" ", 1)
            labels = {}
        samples.setdefault(name.strip(), []).append((labels, value.strip()))
    return samples, types


def _by_label(samples: list[tuple[dict[str, str], str]], key: str) -> dict[str, str]:
    return {labels[key]: value for labels, value in samples}


# ---------------------------------------------------------------------------
# Format helpers
# ---------------------------------------------------------------------------


def test_fmt_int_and_float():
    assert _fmt(5) == "5"
    assert _fmt(0) == "0"
    assert _fmt(123.0) == "123"
    assert _fmt(0.0) == "0"
    assert _fmt(0.5) == "0.5"
    assert _fmt(0.1234) == "0.1234"
    # bool is an int subclass — must render numeric, not "True"/"False".
    assert _fmt(True) == "1"
    assert _fmt(False) == "0"


def test_escape_label_value():
    assert _escape_label_value('a"b') == 'a\\"b'
    assert _escape_label_value("a\\b") == "a\\\\b"
    assert _escape_label_value("a\nb") == "a\\nb"
    assert _escape_label_value("plain") == "plain"


def test_content_type_is_prom_v004():
    assert CONTENT_TYPE == "text/plain; version=0.0.4; charset=utf-8"


# ---------------------------------------------------------------------------
# render_metrics
# ---------------------------------------------------------------------------


def test_render_metrics_empty():
    text = render_metrics([], version="1.2.3")
    assert text.endswith("\n")
    samples, types = _parse(text)

    assert samples["plynf_savings_calls_total"][0][1] == "0"
    assert samples["plynf_raw_tokens_total"][0][1] == "0"
    assert samples["plynf_saved_tokens_total"][0][1] == "0"
    assert samples["plynf_savings_ratio"][0][1] == "0"
    assert samples["plynf_cache_hit_ratio"][0][1] == "0"
    # build_info always present, version carried as a label.
    assert samples["plynf_build_info"][0][0]["version"] == "1.2.3"
    # No connector / tenant families when there is no data.
    assert "plynf_connector_saved_tokens_total" not in samples
    assert "plynf_tenant_tokens_used" not in samples
    # Types are declared.
    assert types["plynf_savings_calls_total"] == "counter"
    assert types["plynf_savings_ratio"] == "gauge"
    assert types["plynf_build_info"] == "gauge"


def test_render_metrics_counts_and_breakdowns():
    events = [
        make_event(
            tenant_id="acme",
            agent_id=None,
            connector="orders",
            tool="get_order",
            model="gpt-4o",
            raw_response_tokens=1000,
            shaped_response_tokens=100,
            cache_hit=False,
            request_args={"order_id": "1"},
        ),
        make_event(
            tenant_id="acme",
            agent_id=None,
            connector="salesforce",
            tool="get_lead",
            model="gpt-4o",
            raw_response_tokens=500,
            shaped_response_tokens=500,
            cache_hit=True,  # saved == full raw (500)
            request_args={"lead_id": "x"},
        ),
    ]
    text = render_metrics(events, version="9.9.9", tenant_usage={"acme": 600})
    samples, types = _parse(text)

    assert samples["plynf_savings_calls_total"][0][1] == "2"
    assert samples["plynf_raw_tokens_total"][0][1] == "1500"
    assert samples["plynf_shaped_tokens_total"][0][1] == "600"
    # 900 (raw-shaped) + 500 (cache hit → full raw) == 1400
    assert samples["plynf_saved_tokens_total"][0][1] == "1400"
    assert samples["plynf_cache_hits_total"][0][1] == "1"

    conn = _by_label(samples["plynf_connector_saved_tokens_total"], "connector")
    assert conn["orders"] == "900"
    assert conn["salesforce"] == "500"

    tenants = _by_label(samples["plynf_tenant_tokens_used"], "tenant")
    assert tenants["acme"] == "600"
    assert types["plynf_tenant_tokens_used"] == "gauge"
    assert types["plynf_connector_saved_tokens_total"] == "counter"


def test_render_metrics_escapes_connector_label():
    events = [
        make_event(
            tenant_id="t",
            agent_id=None,
            connector='weird"name',
            tool="x",
            model="gpt-4o",
            raw_response_tokens=10,
            shaped_response_tokens=1,
            cache_hit=False,
            request_args={},
        )
    ]
    text = render_metrics(events, version="1.0.0")
    assert 'connector="weird\\"name"' in text


# ---------------------------------------------------------------------------
# TierGate.all_usage
# ---------------------------------------------------------------------------


def test_tiergate_all_usage_current_month():
    gate = TierGate()
    gate.record_tokens("t1", 100)
    gate.record_tokens("t2", 50)
    gate.record_tokens("t1", 25)
    assert gate.all_usage() == {"t1": 125, "t2": 50}
    # A tenant that never recorded is absent (not zero).
    assert "t3" not in gate.all_usage()


# ---------------------------------------------------------------------------
# Endpoint
# ---------------------------------------------------------------------------


@pytest.fixture
def demo_client():
    return TestClient(create_app(ProxySettings(demo_mode=True)))


def test_metrics_endpoint_basic(demo_client):
    r = demo_client.get("/metrics")
    assert r.status_code == 200
    ctype = r.headers["content-type"]
    assert ctype.startswith("text/plain")
    assert "version=0.0.4" in ctype
    body = r.text
    assert "# TYPE plynf_saved_tokens_total counter" in body
    assert "plynf_build_info{version=" in body


def test_metrics_endpoint_reflects_tool_call(demo_client):
    chat = {
        "model": "gpt-4o",
        "messages": [{"role": "user", "content": "where is my order?"}],
        "tools": [
            {
                "type": "function",
                "function": {
                    "name": "get_order",
                    "parameters": {"type": "object", "properties": {}},
                },
            }
        ],
    }
    assert demo_client.post("/v1/chat/completions", json=chat).status_code == 200

    samples, _ = _parse(demo_client.get("/metrics").text)
    assert int(samples["plynf_savings_calls_total"][0][1]) >= 1
    # The demo tenant accrued shaped tokens, so its gauge must appear.
    tenants = _by_label(samples["plynf_tenant_tokens_used"], "tenant")
    assert "demo" in tenants
    assert int(tenants["demo"]) >= 1
