# SPDX-License-Identifier: Apache-2.0
"""Tests for the /v1/shape endpoint used by the client-side SDK."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from plinth_proxy.api import create_app
from plinth_proxy.settings import ProxySettings

FIXTURES = Path(__file__).resolve().parent.parent.parent.parent / "examples" / "customer-support"


@pytest.fixture
def client():
    settings = ProxySettings(demo_mode=True)
    return TestClient(create_app(settings))


def _raw_order():
    return json.loads((FIXTURES / "get_order.json").read_text(encoding="utf-8"))


def test_shape_shrinks_known_tool_response(client):
    r = client.post("/v1/shape", json={"tool": "get_order", "raw_response": _raw_order()})
    assert r.status_code == 200
    body = r.json()
    assert body["shaped_by_plynf"] is True
    assert body["saved_tokens"] > 0
    assert body["savings_pct"] > 0.5
    # Only whitelisted fields should be present.
    keys = set(body["shaped"].keys())
    expected = {
        "order_id", "customer_name", "status", "tracking_number",
        "estimated_delivery", "carrier", "items_summary",
        "last_status_update", "total_amount",
    }
    assert keys.issubset(expected)


def test_shape_unknown_tool_passes_through(client):
    r = client.post(
        "/v1/shape",
        json={"tool": "definitely_not_registered", "raw_response": {"x": 1}},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["shaped_by_plynf"] is False
    assert body["shaped"] == {"x": 1}


def test_shape_missing_tool_field_returns_400(client):
    r = client.post("/v1/shape", json={"raw_response": {}})
    assert r.status_code == 400


def test_shape_emits_savings_event(client):
    client.post("/v1/shape", json={"tool": "get_order", "raw_response": _raw_order()})
    summary = client.get("/v1/savings/summary").json()
    assert summary["total_calls"] >= 1
    assert summary["total_saved_tokens"] > 1000  # 150-field order, big delta
