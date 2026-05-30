# SPDX-License-Identifier: Apache-2.0
"""Tests for the Azure OpenAI front door: the ``api-key`` header auth path and
the ``/openai/deployments/{deployment}/chat/completions`` route."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from starlette.requests import Request

from plinth_proxy.api import _extract_token, create_app
from plinth_proxy.settings import ProxySettings


def _request_with_headers(headers: dict[str, str]) -> Request:
    scope = {
        "type": "http",
        "method": "POST",
        "path": "/",
        "headers": [(k.lower().encode(), v.encode()) for k, v in headers.items()],
    }
    return Request(scope)


# ---------------------------------------------------------------------------
# _extract_token: Bearer or Azure api-key header
# ---------------------------------------------------------------------------


def test_extract_token_bearer():
    assert _extract_token(_request_with_headers({"Authorization": "Bearer abc123"})) == "abc123"


def test_extract_token_api_key_header():
    assert _extract_token(_request_with_headers({"api-key": "azkey"})) == "azkey"


def test_extract_token_bearer_wins_over_api_key():
    tok = _extract_token(
        _request_with_headers({"Authorization": "Bearer b", "api-key": "a"})
    )
    assert tok == "b"


def test_extract_token_absent():
    assert _extract_token(_request_with_headers({})) is None


# ---------------------------------------------------------------------------
# api-key header authenticates app-wide (not just the Azure route)
# ---------------------------------------------------------------------------


@pytest.fixture
def keyed_client():
    return TestClient(
        create_app(ProxySettings(demo_mode=True, api_keys="tenant-x:azkey:pro"))
    )


def test_api_key_header_authenticates(keyed_client):
    r = keyed_client.get("/v1/tier", headers={"api-key": "azkey"})
    assert r.status_code == 200
    body = r.json()
    assert body["tenant_id"] == "tenant-x"
    assert body["tier"] == "pro"


def test_bearer_still_authenticates(keyed_client):
    r = keyed_client.get("/v1/tier", headers={"Authorization": "Bearer azkey"})
    assert r.status_code == 200
    assert r.json()["tenant_id"] == "tenant-x"


def test_missing_token_is_401(keyed_client):
    assert keyed_client.get("/v1/tier").status_code == 401


def test_unknown_api_key_is_401(keyed_client):
    r = keyed_client.get("/v1/tier", headers={"api-key": "nope"})
    assert r.status_code == 401


# ---------------------------------------------------------------------------
# Azure deployment chat-completions route (demo / open mode)
# ---------------------------------------------------------------------------


@pytest.fixture
def demo_client():
    return TestClient(create_app(ProxySettings(demo_mode=True)))


def test_azure_route_defaults_model_to_deployment(demo_client):
    body = {"messages": [{"role": "user", "content": "hello"}]}
    r = demo_client.post(
        "/openai/deployments/my-gpt4o/chat/completions?api-version=2024-02-01",
        json=body,
    )
    assert r.status_code == 200
    data = r.json()
    assert data["object"] == "chat.completion"
    # Deployment name fills in the missing model field.
    assert data["model"] == "my-gpt4o"
    assert data["choices"][0]["message"]["role"] == "assistant"


def test_azure_route_preserves_explicit_model(demo_client):
    body = {"model": "gpt-4o-mini", "messages": [{"role": "user", "content": "hi"}]}
    r = demo_client.post("/openai/deployments/whatever/chat/completions", json=body)
    assert r.status_code == 200
    assert r.json()["model"] == "gpt-4o-mini"


def test_azure_route_tool_roundtrip_and_savings(demo_client):
    body = {
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
    r = demo_client.post("/openai/deployments/gpt-4o/chat/completions", json=body)
    assert r.status_code == 200
    data = r.json()
    assert data["choices"][0]["message"]["content"]  # summary text after the tool ran

    summary = demo_client.get("/v1/savings/summary").json()
    assert summary["total_calls"] >= 1


def test_azure_route_streams_when_requested(demo_client):
    body = {"messages": [{"role": "user", "content": "hi"}], "stream": True}
    r = demo_client.post("/openai/deployments/gpt-4o/chat/completions", json=body)
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/event-stream")
    assert "[DONE]" in r.text
