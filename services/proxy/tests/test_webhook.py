# SPDX-License-Identifier: Apache-2.0
"""Tests for the generic webhook endpoint /v1/tools/{tool}/invoke."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from plinth_proxy.api import create_app
from plinth_proxy.settings import ProxySettings


@pytest.fixture
def client():
    return TestClient(create_app(ProxySettings(demo_mode=True)))


def test_webhook_invokes_get_order_and_shapes_response(client):
    r = client.post("/v1/tools/get_order/invoke", json={"arguments": {"order_id": "12345"}})
    assert r.status_code == 200
    body = r.json()
    assert body["tool"] == "get_order"
    assert body["connector"] == "orders"
    assert body["savings"]["savings_pct"] > 0.5
    # Whitelisted fields only.
    keys = set(body["result"].keys())
    assert keys <= {
        "order_id", "customer_name", "status", "tracking_number",
        "estimated_delivery", "carrier", "items_summary",
        "last_status_update", "total_amount",
    }


def test_webhook_returns_404_for_unknown_tool(client):
    r = client.post("/v1/tools/definitely_not_a_tool/invoke", json={"arguments": {}})
    assert r.status_code == 404


def test_webhook_works_without_body(client):
    # Bedrock Lambda sometimes sends an empty body when the tool takes no args.
    r = client.post("/v1/tools/get_order/invoke", content=b"")
    assert r.status_code == 200


def test_webhook_returns_cache_hit_on_second_call(client):
    client.post("/v1/tools/get_order/invoke", json={"arguments": {"order_id": "12345"}})
    r2 = client.post("/v1/tools/get_order/invoke", json={"arguments": {"order_id": "12345"}})
    assert r2.json()["cache_hit"] is True
