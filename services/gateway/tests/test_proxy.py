# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 The Plinth Authors
"""Direct tests for ``proxy.HttpProxy``."""

from __future__ import annotations

import httpx
import pytest
import respx
from httpx import Response

from plinth_gateway.exceptions import ToolInvocationError, TransportNotSupported
from plinth_gateway.models import Tool
from plinth_gateway.proxy import HttpProxy


def _tool(transport: str = "http", auth_method: str = "none", **kwargs) -> Tool:
    base = {
        "tool_id": "web.fetch",
        "name": "web fetch",
        "description": "fetch URLs",
        "transport": transport,
        "endpoint": "http://mcp.test/invoke/fetch",
        "input_schema": {},
        "output_schema": {},
        "idempotent": True,
        "side_effects": "read",
        "cache_ttl_seconds": 30,
        "auth_method": auth_method,
        "auth_config": {},
        "created_at": "2026-01-01T00:00:00+00:00",
        "updated_at": "2026-01-01T00:00:00+00:00",
    }
    base.update(kwargs)
    return Tool.model_validate(base)


async def test_invoke_http_success() -> None:
    proxy = HttpProxy(timeout_seconds=2.0)
    try:
        with respx.mock(assert_all_called=True) as mock:
            mock.post("http://mcp.test/invoke/fetch").mock(
                return_value=Response(200, json={"content": "ok"})
            )
            result = await proxy.invoke(_tool(), {"url": "u"})
            assert result == {"content": "ok"}
    finally:
        await proxy.aclose()


async def test_invoke_http_4xx_raises() -> None:
    proxy = HttpProxy(timeout_seconds=2.0)
    try:
        with respx.mock(assert_all_called=True) as mock:
            mock.post("http://mcp.test/invoke/fetch").mock(
                return_value=Response(404, json={"err": "no"})
            )
            with pytest.raises(ToolInvocationError) as exc:
                await proxy.invoke(_tool(), {"url": "u"})
            assert exc.value.details.get("status_code") == 404
    finally:
        await proxy.aclose()


async def test_invoke_http_non_json_raises() -> None:
    proxy = HttpProxy(timeout_seconds=2.0)
    try:
        with respx.mock(assert_all_called=True) as mock:
            mock.post("http://mcp.test/invoke/fetch").mock(
                return_value=Response(200, content=b"<html>not json</html>")
            )
            with pytest.raises(ToolInvocationError):
                await proxy.invoke(_tool(), {"url": "u"})
    finally:
        await proxy.aclose()


async def test_invoke_http_network_error_raises() -> None:
    proxy = HttpProxy(timeout_seconds=2.0)
    try:
        with respx.mock(assert_all_called=True) as mock:
            mock.post("http://mcp.test/invoke/fetch").mock(
                side_effect=httpx.ConnectError("boom")
            )
            with pytest.raises(ToolInvocationError):
                await proxy.invoke(_tool(), {})
    finally:
        await proxy.aclose()


async def test_invoke_stdio_unsupported() -> None:
    proxy = HttpProxy()
    try:
        with pytest.raises(TransportNotSupported):
            await proxy.invoke(_tool(transport="stdio"), {})
    finally:
        await proxy.aclose()


async def test_invoke_attaches_bearer_header() -> None:
    proxy = HttpProxy()
    captured: dict = {}

    def _capture(request):
        captured["auth"] = request.headers.get("Authorization")
        return Response(200, json={"ok": True})

    try:
        with respx.mock(assert_all_called=True) as mock:
            mock.post("http://mcp.test/invoke/fetch").mock(side_effect=_capture)
            tool = _tool(auth_method="bearer", auth_config={"token": "t1"})
            await proxy.invoke(tool, {})
            assert captured["auth"] == "Bearer t1"
    finally:
        await proxy.aclose()


async def test_invoke_oauth_mock_token() -> None:
    proxy = HttpProxy()
    captured: dict = {}

    def _capture(request):
        captured["auth"] = request.headers.get("Authorization")
        return Response(200, json={"ok": True})

    try:
        with respx.mock(assert_all_called=True) as mock:
            mock.post("http://mcp.test/invoke/fetch").mock(side_effect=_capture)
            tool = _tool(auth_method="oauth2", auth_config={"mock_token": "t2"})
            await proxy.invoke(tool, {})
            assert captured["auth"] == "Bearer t2"
    finally:
        await proxy.aclose()


async def test_invoke_no_auth_attaches_no_header() -> None:
    proxy = HttpProxy()
    captured: dict = {}

    def _capture(request):
        captured["auth"] = request.headers.get("Authorization")
        return Response(200, json={"ok": True})

    try:
        with respx.mock(assert_all_called=True) as mock:
            mock.post("http://mcp.test/invoke/fetch").mock(side_effect=_capture)
            await proxy.invoke(_tool(), {})
            assert captured["auth"] is None
    finally:
        await proxy.aclose()


async def test_proxy_uses_injected_client() -> None:
    async with httpx.AsyncClient() as client:
        proxy = HttpProxy(client=client)
        with respx.mock(assert_all_called=True) as mock:
            mock.post("http://mcp.test/invoke/fetch").mock(
                return_value=Response(200, json={"ok": True})
            )
            r = await proxy.invoke(_tool(), {})
            assert r == {"ok": True}
            # property exposed for inspection
            assert proxy.client is client
        await proxy.aclose()  # should NOT close the injected client
        assert client.is_closed is False


def test_outbound_headers_no_token_for_bearer_returns_empty() -> None:
    from plinth_gateway.auth import outbound_headers

    assert outbound_headers("bearer", {}) == {}
    assert outbound_headers("bearer", {"token": ""}) == {}


def test_outbound_headers_no_token_for_oauth2_returns_empty() -> None:
    from plinth_gateway.auth import outbound_headers

    assert outbound_headers("oauth2", {}) == {}
    assert outbound_headers("oauth2", {"mock_token": ""}) == {}


def test_outbound_headers_unknown_method_returns_empty() -> None:
    from plinth_gateway.auth import outbound_headers

    assert outbound_headers("none", {}) == {}
    assert outbound_headers("future", {}) == {}
