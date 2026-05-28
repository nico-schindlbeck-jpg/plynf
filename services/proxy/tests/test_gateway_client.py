# SPDX-License-Identifier: Apache-2.0
"""Tests for the MCP-gateway-backed connector registry."""

from __future__ import annotations

import json

import httpx
import pytest

from plinth_proxy import gateway_client as gc
from plinth_proxy.gateway_client import (
    GatewayClient,
    GatewayInvocationError,
    make_gateway_registry,
)

_REAL_ASYNC_CLIENT = httpx.AsyncClient  # captured before monkeypatch


def _mock_async_client(handler):
    """Return a factory that yields an httpx.AsyncClient with a MockTransport."""
    transport = httpx.MockTransport(handler)

    class _Factory:
        def __init__(self, **_kw):
            self._client = _REAL_ASYNC_CLIENT(transport=transport)

        async def __aenter__(self):
            return self._client

        async def __aexit__(self, *exc):
            await self._client.aclose()

    return _Factory


@pytest.mark.asyncio
async def test_gateway_client_posts_invoke_and_returns_result(monkeypatch):
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["body"] = json.loads(request.content)
        captured["auth"] = request.headers.get("authorization")
        return httpx.Response(
            200,
            json={
                "tool_id": "get_order",
                "arguments": captured["body"]["arguments"],
                "result": {"order_id": "12345", "status": "in_transit"},
                "cached": False,
                "duration_ms": 12,
                "audit_id": "aud-1",
                "cost_estimate_usd": 0.0,
            },
        )

    monkeypatch.setattr(gc.httpx, "AsyncClient", _mock_async_client(handler))

    client = GatewayClient("http://gateway:7422", default_auth_header="Bearer svc-tok")
    result = await client.invoke("get_order", {"order_id": "12345"})

    assert result == {"order_id": "12345", "status": "in_transit"}
    assert captured["url"].endswith("/v1/invoke")
    assert captured["body"]["tool_id"] == "get_order"
    assert captured["body"]["arguments"] == {"order_id": "12345"}
    assert captured["auth"] == "Bearer svc-tok"


@pytest.mark.asyncio
async def test_gateway_client_raises_on_4xx(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(403, text="forbidden by policy")

    monkeypatch.setattr(gc.httpx, "AsyncClient", _mock_async_client(handler))
    client = GatewayClient("http://gateway:7422")

    with pytest.raises(GatewayInvocationError) as exc:
        await client.invoke("get_lead", {"id": "1"})
    assert exc.value.status == 403
    assert "forbidden" in exc.value.body


@pytest.mark.asyncio
async def test_gateway_registry_executes_through_client(monkeypatch):
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = json.loads(request.content)
        captured["auth"] = request.headers.get("authorization")
        return httpx.Response(
            200,
            json={
                "tool_id": captured["body"]["tool_id"],
                "arguments": captured["body"]["arguments"],
                "result": {"hello": "from gateway"},
                "cached": False,
                "duration_ms": 1,
                "audit_id": "aud-2",
                "cost_estimate_usd": 0.0,
            },
        )

    monkeypatch.setattr(gc.httpx, "AsyncClient", _mock_async_client(handler))

    client = GatewayClient("http://gateway:7422")
    registry = make_gateway_registry(
        client, auth_header_provider=lambda: "Bearer caller-tok"
    )

    connector, result = await registry.execute("get_order", {"order_id": "12345"})
    assert connector == "orders"
    assert result == {"hello": "from gateway"}
    assert captured["auth"] == "Bearer caller-tok"
