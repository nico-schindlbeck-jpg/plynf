# SPDX-License-Identifier: Apache-2.0
"""Tests for plinth.proxy_client — OpenAI drop-in and wrap_tools."""

from __future__ import annotations

import json

import httpx
import pytest

from plinth.proxy_client import OpenAI, wrap_tool, wrap_tools
from plinth.proxy_client.openai_drop_in import OpenAIProxyError
from plinth.proxy_client.tools_wrap import ShapeError


_REAL_CLIENT = httpx.Client  # captured before any monkeypatching


def _mock_client_factory(handler):
    transport = httpx.MockTransport(handler)

    class _Factory:
        def __init__(self, **_kw):
            self._client = _REAL_CLIENT(transport=transport)

        def __enter__(self):
            return self._client

        def __exit__(self, *exc):
            self._client.close()

    return _Factory


def test_openai_drop_in_routes_to_plynf_url(monkeypatch):
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["auth"] = request.headers.get("authorization")
        captured["body"] = json.loads(request.content)
        return httpx.Response(
            200,
            json={
                "id": "chatcmpl-1",
                "object": "chat.completion",
                "model": "gpt-4o",
                "choices": [{"index": 0, "message": {"role": "assistant", "content": "ok"}}],
            },
        )

    monkeypatch.setattr(
        "plinth.proxy_client.openai_drop_in.httpx.Client",
        _mock_client_factory(handler),
    )

    client = OpenAI(api_key="sk-test", plynf_url="http://plynf.test")
    resp = client.chat.completions.create(
        model="gpt-4o", messages=[{"role": "user", "content": "hi"}]
    )

    assert resp["choices"][0]["message"]["content"] == "ok"
    assert captured["url"].endswith("/v1/chat/completions")
    assert captured["auth"] == "Bearer sk-test"
    assert captured["body"]["model"] == "gpt-4o"
    assert captured["body"]["stream"] is False


def test_openai_drop_in_propagates_4xx(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, text="bad key")

    monkeypatch.setattr(
        "plinth.proxy_client.openai_drop_in.httpx.Client",
        _mock_client_factory(handler),
    )
    client = OpenAI(api_key="sk-x", plynf_url="http://plynf.test")
    with pytest.raises(OpenAIProxyError) as exc:
        client.chat.completions.create(model="gpt-4o", messages=[])
    assert exc.value.status == 401


def test_wrap_tool_calls_shape_endpoint(monkeypatch):
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["body"] = json.loads(request.content)
        return httpx.Response(
            200,
            json={
                "shaped": {"order_id": "12345", "status": "in_transit"},
                "shaped_by_plynf": True,
                "raw_response_tokens": 200,
                "shaped_response_tokens": 12,
                "saved_tokens": 188,
                "savings_pct": 0.94,
            },
        )

    monkeypatch.setattr(
        "plinth.proxy_client.tools_wrap.httpx.Client",
        _mock_client_factory(handler),
    )

    def get_order(order_id: str) -> dict:
        return {"order_id": order_id, "status": "in_transit", "noise": "x" * 100}

    wrapped = wrap_tool(get_order, plynf_url="http://plynf.test", api_key="sk")
    result = wrapped(order_id="12345")

    assert result == {"order_id": "12345", "status": "in_transit"}
    assert captured["url"].endswith("/v1/shape")
    assert captured["body"]["tool"] == "get_order"
    # Wrapped function preserves the original tool name + name attr.
    assert wrapped.__name__ == "get_order"
    assert getattr(wrapped, "__plynf_wrapped__", False) is True


def test_wrap_tool_fails_open_when_plynf_unreachable(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="upstream broke")

    monkeypatch.setattr(
        "plinth.proxy_client.tools_wrap.httpx.Client",
        _mock_client_factory(handler),
    )

    def get_lead() -> dict:
        return {"Id": "1", "Name": "Jane"}

    wrapped = wrap_tool(get_lead, plynf_url="http://plynf.test", api_key="sk")
    # ShapeError is caught internally; agent still gets the raw response.
    result = wrapped()
    assert result == {"Id": "1", "Name": "Jane"}


def test_wrap_tools_batch_preserves_order_and_names(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"shaped": "ok"})

    monkeypatch.setattr(
        "plinth.proxy_client.tools_wrap.httpx.Client",
        _mock_client_factory(handler),
    )

    def a():
        return {}

    def b():
        return {}

    wrapped = wrap_tools([a, b], plynf_url="http://plynf.test", api_key="sk")
    assert [w.__name__ for w in wrapped] == ["a", "b"]
    assert all(getattr(w, "__plynf_wrapped__", False) for w in wrapped)
