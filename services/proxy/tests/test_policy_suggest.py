# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 The Plynf Authors
"""Tests for the server-side auto keep-list suggestion (P1.1)."""

from __future__ import annotations

from fastapi.testclient import TestClient

from plinth_proxy.api import create_app
from plinth_proxy.policy_engine import ToolPolicy, apply
from plinth_proxy.policy_suggest import render_policy_yaml, suggest_keep_list
from plinth_proxy.settings import ProxySettings

ORDER = {
    "orderId": "ORD-2024-78421",
    "customerName": "Acme GmbH",
    "status": "in_transit",
    "trackingNumber": "1Z999AA10123456784",
    "totalAmount": 1249.99,
    "internalRoutingCode": "WH3-X-441",
    "paymentProcessorRef": "stripe_pi_3NqK8aLkdIwHu7ix1JZQbR2v",
    "fraudScore": 0.02,
    "warehouseNotes": None,
    "giftMessage": "",
    "createdAt": "2026-06-01T10:02:11Z",
    "etag": 'W/"order-78421-v7"',
    "_version": 7,
    "lineItems": [
        {"sku": "WID-PRO-2026", "qty": 3, "warehouseBin": "A-12-3", "supplierRef": "SUP-441"}
    ],
}


def test_keeps_business_fields_drops_plumbing():
    s = suggest_keep_list(ORDER)
    for f in ("status", "customerName", "totalAmount", "trackingNumber", "orderId",
              "lineItems.sku"):
        assert f in s.keep_list, f"{f} must be kept"
    for f in ("internalRoutingCode", "paymentProcessorRef", "createdAt", "etag",
              "lineItems.warehouseBin"):
        assert f not in s.keep_list, f"{f} must be dropped"
    assert not s.used_fallback


def test_suggestion_round_trips_through_the_engine():
    s = suggest_keep_list(ORDER)
    shaped = apply(
        ORDER,
        ToolPolicy(
            tool="get_order",
            allow_fields=tuple(s.keep_list),
            strip_metadata=True,
            drop_empty_fields=True,
        ),
    )
    assert shaped["status"] == "in_transit"
    assert "internalRoutingCode" not in shaped
    assert "paymentProcessorRef" not in shaped


def test_value_shape_never_beats_name_intent():
    # "Negotiation/Review" is 18 machine-y chars; the field NAME wins.
    s = suggest_keep_list({"Id": "0068b00001PqRsTUAV", "StageName": "Negotiation/Review",
                           "Amount": 184000.5, "OwnerId": "0058b00000GxTqPAAV"})
    assert "StageName" in s.keep_list
    assert "Id" in s.keep_list
    assert "OwnerId" not in s.keep_list, "foreign keys must be dropped"
    assert not s.used_fallback


def test_fallback_never_suggests_blanking():
    s = suggest_keep_list({"x1": "zz", "x2": "yy", "q9": 4})
    assert s.used_fallback
    assert len(s.keep_list) >= 3


def test_render_policy_yaml():
    yaml_text = render_policy_yaml("get_order", ["order_id", "status"], connector="orders")
    assert "connector: orders" in yaml_text
    assert "  get_order:" in yaml_text
    assert "      - order_id" in yaml_text
    assert "strip_metadata: true" in yaml_text
    assert render_policy_yaml("  ", ["a"]).count("my_tool:") == 1


def test_suggest_policy_endpoint():
    client = TestClient(create_app(ProxySettings(demo_mode=True)))
    resp = client.post(
        "/v1/suggest-policy",
        headers={"Authorization": "Bearer demo"},
        json={"tool": "get_order", "raw_response": ORDER, "connector": "orders"},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert "status" in data["suggestion"]["keep_list"]
    assert "connector: orders" in data["policy_yaml"]
    preview = data["preview"]
    assert preview["raw_response_tokens"] > preview["shaped_response_tokens"] > 0
    assert preview["savings_pct"] > 0.3
    # Reasons are attached per field, so the operator can audit the proposal.
    reasons = {f["path"]: f["reason"] for f in data["suggestion"]["fields"]}
    assert reasons["internalRoutingCode"] == "internal plumbing"


def test_suggest_policy_requires_raw_response():
    client = TestClient(create_app(ProxySettings(demo_mode=True)))
    resp = client.post(
        "/v1/suggest-policy",
        headers={"Authorization": "Bearer demo"},
        json={"tool": "get_order"},
    )
    assert resp.status_code == 400
