# SPDX-License-Identifier: Apache-2.0
"""``x-request-id`` propagation.

Vendor SDKs surface a request-id for support correlation (the OpenAI SDK
exposes ``response.request_id`` and attaches it to raised errors; Anthropic
reads ``request-id``). A client fronting Plynf must not lose that id, so every
response — success, dialect error envelope, or SSE stream — carries one: the
inbound id when present, else a freshly minted ``req_<hex>``.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from plinth_proxy.api import create_app
from plinth_proxy.settings import ProxySettings

_PLAIN_BODY = {"messages": [{"role": "user", "content": "hi"}]}


@pytest.fixture
def demo_client():
    return TestClient(create_app(ProxySettings(demo_mode=True)))


def test_mints_request_id_when_absent(demo_client):
    r = demo_client.post("/v1/chat/completions", json=_PLAIN_BODY)
    assert r.status_code == 200
    assert r.headers["x-request-id"].startswith("req_")


def test_echoes_inbound_request_id(demo_client):
    r = demo_client.post(
        "/v1/chat/completions",
        headers={"x-request-id": "req_client_supplied_123"},
        json=_PLAIN_BODY,
    )
    assert r.headers["x-request-id"] == "req_client_supplied_123"


def test_blank_inbound_id_is_replaced(demo_client):
    # A whitespace-only inbound id is treated as absent → a real one is minted.
    r = demo_client.post(
        "/v1/chat/completions", headers={"x-request-id": "   "}, json=_PLAIN_BODY
    )
    assert r.headers["x-request-id"].startswith("req_")


def test_overlong_inbound_id_is_capped(demo_client):
    r = demo_client.post(
        "/v1/chat/completions",
        headers={"x-request-id": "x" * 500},
        json=_PLAIN_BODY,
    )
    assert len(r.headers["x-request-id"]) == 200


def test_request_id_present_on_health_and_metrics(demo_client):
    # Even unauthenticated infra endpoints carry the id (uniform middleware).
    assert "x-request-id" in demo_client.get("/healthz").headers
    assert "x-request-id" in demo_client.get("/metrics").headers


def test_request_id_on_error_envelope():
    # A 401 flows back out through the middleware, so the error envelope is
    # stamped too — exactly when an SDK most wants the id for a support ticket.
    client = TestClient(create_app(ProxySettings(demo_mode=True, api_keys="t:key:free")))
    r = client.post("/v1/chat/completions", json={"messages": []})
    assert r.status_code == 401
    assert r.headers["x-request-id"].startswith("req_")


def test_request_id_on_streaming_response(demo_client):
    body = dict(_PLAIN_BODY, stream=True)
    r = demo_client.post("/v1/chat/completions", json=body)
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/event-stream")
    # Pure-ASGI middleware stamps the id without buffering the SSE body.
    assert r.headers["x-request-id"].startswith("req_")


def test_unique_ids_across_requests(demo_client):
    a = demo_client.post("/v1/chat/completions", json=_PLAIN_BODY).headers["x-request-id"]
    b = demo_client.post("/v1/chat/completions", json=_PLAIN_BODY).headers["x-request-id"]
    assert a != b
