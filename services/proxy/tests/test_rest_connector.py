# SPDX-License-Identifier: Apache-2.0
"""Tests for the generic REST connector: spec parsing, request building,
the SSRF guard, HTTP dispatch, and registry / pipeline integration."""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from plinth_proxy import rest_connector as rc
from plinth_proxy.api import _build_state, _handle_tool_call, create_app
from plinth_proxy.connectors import ConnectorCall, ConnectorRegistry
from plinth_proxy.rest_connector import (
    RestConnectorError,
    RestConnectorSpec,
    RestEndpoint,
    SSRFError,
    assert_url_allowed,
    build_rest_connector,
    render_request,
    spec_from_dict,
    specs_from_json,
)
from plinth_proxy.settings import ProxySettings

# ---------------------------------------------------------------------------
# Fake httpx client (no network)
# ---------------------------------------------------------------------------


class _FakeResponse:
    def __init__(self, content: bytes, status_code: int = 200):
        self.content = content
        self.status_code = status_code


class _FakeClient:
    """Records the single request it receives and returns a canned response."""

    def __init__(self, captured: dict, response: _FakeResponse):
        self._captured = captured
        self._response = response

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def request(self, method, url, *, params=None, json=None, headers=None):
        self._captured.update(
            method=method, url=url, params=params, json=json, headers=headers
        )
        return self._response


@pytest.fixture
def patch_httpx(monkeypatch):
    """Patch rest_connector.httpx.AsyncClient; returns (captured, set_response)."""
    captured: dict = {}
    box: dict = {"response": _FakeResponse(b"{}")}

    def _factory(*_a, **_k):
        return _FakeClient(captured, box["response"])

    monkeypatch.setattr(rc.httpx, "AsyncClient", _factory)

    def set_response(content: bytes, status_code: int = 200):
        box["response"] = _FakeResponse(content, status_code)

    return captured, set_response


# ---------------------------------------------------------------------------
# Spec parsing
# ---------------------------------------------------------------------------


def test_spec_from_dict_basic():
    spec = spec_from_dict(
        {
            "name": "inventory",
            "base_url": "https://api.acme.test",
            "endpoints": [
                {"tool": "get_sku", "method": "get", "path": "/v1/skus/{sku_id}"},
                {"tool": "search", "path": "/v1/skus", "query": ["q"]},
            ],
        }
    )
    assert spec.name == "inventory"
    assert spec.base_url == "https://api.acme.test"
    assert spec.endpoints[0].method == "GET"  # upper-cased
    assert spec.endpoints[1].query == ("q",)
    assert spec.timeout_s == 30.0
    assert spec.allow_private_hosts is False


def test_header_env_expansion(monkeypatch):
    monkeypatch.setenv("INV_TOKEN", "s3cret")
    spec = spec_from_dict(
        {
            "name": "x",
            "base_url": "https://api.test",
            "headers": {"Authorization": "Bearer ${INV_TOKEN}"},
            "endpoints": [{"tool": "t", "path": "/"}],
        }
    )
    assert spec.headers["Authorization"] == "Bearer s3cret"


def test_specs_from_json_accepts_single_object_or_array():
    one = specs_from_json('{"name":"a","base_url":"https://a.test","endpoints":[]}')
    assert len(one) == 1 and one[0].name == "a"
    many = specs_from_json(
        '[{"name":"a","base_url":"https://a.test","endpoints":[]},'
        '{"name":"b","base_url":"https://b.test","endpoints":[]}]'
    )
    assert [s.name for s in many] == ["a", "b"]


def test_spec_missing_name_or_base_url_raises():
    with pytest.raises(RestConnectorError):
        spec_from_dict({"base_url": "https://x.test", "endpoints": []})
    with pytest.raises(RestConnectorError):
        spec_from_dict({"name": "x", "endpoints": []})


def test_endpoint_without_tool_raises():
    with pytest.raises(RestConnectorError):
        spec_from_dict(
            {"name": "x", "base_url": "https://x.test", "endpoints": [{"path": "/"}]}
        )


# ---------------------------------------------------------------------------
# Request rendering
# ---------------------------------------------------------------------------


def _spec(**kw):
    return RestConnectorSpec(name="c", base_url="https://api.test", **kw)


def test_render_path_template_and_get_query():
    ep = RestEndpoint(tool="get_sku", method="GET", path="/v1/skus/{sku_id}")
    url, query, body = render_request(_spec(), ep, {"sku_id": "ABC", "fields": "name"})
    assert url == "https://api.test/v1/skus/ABC"
    # sku_id is consumed by the path; the rest become query params on a GET.
    assert query == {"fields": "name"}
    assert body == {}


def test_render_post_routes_remaining_to_body():
    ep = RestEndpoint(tool="create", method="POST", path="/v1/notes")
    url, query, body = render_request(_spec(), ep, {"text": "hi", "pinned": True})
    assert url == "https://api.test/v1/notes"
    assert query == {}
    assert body == {"text": "hi", "pinned": True}


def test_render_explicit_query_on_post_excluded_from_default_body():
    ep = RestEndpoint(tool="x", method="POST", path="/v1/x", query=("a",))
    _url, query, body = render_request(_spec(), ep, {"a": 1, "b": 2})
    assert query == {"a": 1}
    assert body == {"b": 2}  # default body excludes the query-routed key


def test_render_missing_path_param_raises():
    ep = RestEndpoint(tool="get", method="GET", path="/v1/x/{id}")
    with pytest.raises(RestConnectorError):
        render_request(_spec(), ep, {"other": 1})


# ---------------------------------------------------------------------------
# SSRF guard
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1/x",          # loopback
        "http://10.0.0.5/x",           # private
        "http://192.168.1.1/x",        # private
        "http://169.254.169.254/latest/meta-data/",  # cloud metadata (link-local)
        "http://[::1]/x",              # IPv6 loopback
        "https://0.0.0.0/x",           # unspecified
    ],
)
def test_ssrf_blocks_non_public(url):
    with pytest.raises(SSRFError):
        assert_url_allowed(url, allow_private=False)


@pytest.mark.parametrize("url", ["file:///etc/passwd", "ftp://x.test/", "gopher://x/"])
def test_ssrf_blocks_non_http_schemes(url):
    with pytest.raises(SSRFError):
        assert_url_allowed(url, allow_private=False)


def test_ssrf_blocks_missing_host():
    with pytest.raises(SSRFError):
        assert_url_allowed("http:///nohost", allow_private=False)


def test_ssrf_allows_public_ip_literal():
    # Numeric IPs don't hit DNS; 8.8.8.8 is public → allowed.
    assert_url_allowed("https://8.8.8.8/x", allow_private=False)


def test_ssrf_allow_private_opt_in_bypasses_check():
    # With the on-prem opt-in, a private host is permitted (no resolution).
    assert_url_allowed("http://10.0.0.5/x", allow_private=True)
    assert_url_allowed("http://db.internal:5432/x", allow_private=True)


def test_ssrf_blocks_resolved_private_host(monkeypatch):
    # A hostname that resolves to a private address must be blocked even though
    # the name itself looks innocuous.
    def fake_getaddrinfo(host, port, **_kw):
        return [(2, 1, 6, "", ("10.1.2.3", port))]

    monkeypatch.setattr(rc.socket, "getaddrinfo", fake_getaddrinfo)
    with pytest.raises(SSRFError):
        assert_url_allowed("https://evil.test/x", allow_private=False)


# ---------------------------------------------------------------------------
# Handler dispatch
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_handler_builds_request_and_parses_json(patch_httpx):
    captured, set_response = patch_httpx
    set_response(json.dumps({"id": "ABC", "name": "Widget"}).encode())
    spec = RestConnectorSpec(
        name="inv",
        base_url="https://api.test",
        endpoints=(RestEndpoint(tool="get_sku", method="GET", path="/skus/{sku_id}"),),
        headers={"Authorization": "Bearer t"},
        allow_private_hosts=True,  # skip DNS in the guard for api.test
    )
    t2c, handler = build_rest_connector(spec)
    assert t2c == {"get_sku": "inv"}

    result = await handler(ConnectorCall(connector="inv", tool="get_sku", args={"sku_id": "ABC"}))
    assert result == {"id": "ABC", "name": "Widget"}
    assert captured["method"] == "GET"
    assert captured["url"] == "https://api.test/skus/ABC"
    assert captured["headers"]["Authorization"] == "Bearer t"


@pytest.mark.asyncio
async def test_handler_wraps_non_json_body(patch_httpx):
    _captured, set_response = patch_httpx
    set_response(b"plain text not json", status_code=200)
    spec = RestConnectorSpec(
        name="x",
        base_url="https://api.test",
        endpoints=(RestEndpoint(tool="t", path="/"),),
        allow_private_hosts=True,
    )
    _t2c, handler = build_rest_connector(spec)
    result = await handler(ConnectorCall(connector="x", tool="t", args={}))
    assert result["_http_status"] == 200
    assert result["body"] == "plain text not json"


@pytest.mark.asyncio
async def test_handler_surfaces_error_status(patch_httpx):
    _captured, set_response = patch_httpx
    set_response(json.dumps({"error": "not found"}).encode(), status_code=404)
    spec = RestConnectorSpec(
        name="x",
        base_url="https://api.test",
        endpoints=(RestEndpoint(tool="t", path="/"),),
        allow_private_hosts=True,
    )
    _t2c, handler = build_rest_connector(spec)
    result = await handler(ConnectorCall(connector="x", tool="t", args={}))
    assert result["_http_status"] == 404
    assert result["error"] == "not found"


@pytest.mark.asyncio
async def test_handler_blocks_ssrf_before_dispatch(patch_httpx):
    captured, _set = patch_httpx
    spec = RestConnectorSpec(
        name="x",
        base_url="http://169.254.169.254",
        endpoints=(RestEndpoint(tool="t", path="/latest/meta-data/"),),
        allow_private_hosts=False,
    )
    _t2c, handler = build_rest_connector(spec)
    with pytest.raises(SSRFError):
        await handler(ConnectorCall(connector="x", tool="t", args={}))
    assert captured == {}  # never dispatched


# ---------------------------------------------------------------------------
# Registry integration
# ---------------------------------------------------------------------------


def test_registry_register_with_tools_resolves():
    reg = ConnectorRegistry()
    reg.register("inv", lambda call: {"ok": True}, tools=["get_sku", "search"])
    assert reg.resolve("get_sku") == "inv"
    assert reg.resolve("search") == "inv"
    assert reg.has("get_sku") is True
    # Static map still resolves built-ins.
    assert reg.resolve("get_order") == "orders"
    # Unknown tool resolves to nothing.
    assert reg.resolve("nonexistent") is None


# ---------------------------------------------------------------------------
# Pipeline integration via _handle_tool_call
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_custom_rest_tool_flows_through_pipeline(patch_httpx, monkeypatch):
    captured, set_response = patch_httpx
    set_response(json.dumps({"sku": "ABC", "qty": 7}).encode())

    rest_cfg = json.dumps(
        [
            {
                "name": "inventory",
                "base_url": "https://api.test",
                "allow_private_hosts": True,
                "endpoints": [
                    {"tool": "get_sku", "method": "GET", "path": "/skus/{sku_id}"}
                ],
            }
        ]
    )
    # demo_tier defaults to "enterprise", which permits custom REST connectors.
    settings = ProxySettings(demo_mode=True, rest_connectors=rest_cfg)
    state = _build_state(settings)

    # The custom connector resolved and is registered.
    assert state.registry.resolve("get_sku") == "inventory"
    assert state.registry.has("get_sku") is True

    tool_call = {
        "id": "call_1",
        "type": "function",
        "function": {"name": "get_sku", "arguments": json.dumps({"sku_id": "ABC"})},
    }
    before = len(state.events)
    msg = await _handle_tool_call(state, tool_call, model="gpt-4o", tenant_id="demo")

    assert msg["role"] == "tool"
    assert msg["tool_call_id"] == "call_1"
    # Default (empty) policy → shaped == raw; the body round-trips.
    assert json.loads(msg["content"]) == {"sku": "ABC", "qty": 7}
    # A savings event was recorded against the resolved connector name.
    assert len(state.events) == before + 1
    assert state.events[-1].connector == "inventory"
    assert captured["url"] == "https://api.test/skus/ABC"


def test_custom_rest_skipped_when_tier_disallows():
    rest_cfg = json.dumps(
        [
            {
                "name": "inventory",
                "base_url": "https://api.test",
                "endpoints": [{"tool": "get_sku", "path": "/skus/{sku_id}"}],
            }
        ]
    )
    # Free tier does NOT permit custom REST connectors → registration skipped.
    settings = ProxySettings(demo_mode=True, demo_tier="free", rest_connectors=rest_cfg)
    state = _build_state(settings)
    assert state.registry.resolve("get_sku") is None
    assert state.registry.has("get_sku") is False


def test_custom_rest_tool_via_webhook_invoke(patch_httpx):
    """The generic /v1/tools/{tool}/invoke surface must dispatch custom REST
    tools too — not just the chat-completions path."""
    captured, set_response = patch_httpx
    set_response(json.dumps({"sku": "ABC", "qty": 7}).encode())
    rest_cfg = json.dumps(
        [
            {
                "name": "inventory",
                "base_url": "https://api.test",
                "allow_private_hosts": True,
                "endpoints": [
                    {"tool": "get_sku", "method": "GET", "path": "/skus/{sku_id}"}
                ],
            }
        ]
    )
    client = TestClient(create_app(ProxySettings(demo_mode=True, rest_connectors=rest_cfg)))
    r = client.post("/v1/tools/get_sku/invoke", json={"arguments": {"sku_id": "ABC"}})
    assert r.status_code == 200
    body = r.json()
    assert body["tool"] == "get_sku"
    assert body["connector"] == "inventory"
    assert body["result"] == {"sku": "ABC", "qty": 7}
    assert captured["url"] == "https://api.test/skus/ABC"


def test_connectors_endpoint_lists_builtin_and_custom():
    rest_cfg = json.dumps(
        [
            {
                "name": "inventory",
                "base_url": "https://api.test",
                "allow_private_hosts": True,
                "endpoints": [
                    {"tool": "get_sku", "path": "/skus/{sku_id}"},
                    {"tool": "search_skus", "path": "/skus"},
                ],
            }
        ]
    )
    client = TestClient(create_app(ProxySettings(demo_mode=True, rest_connectors=rest_cfg)))
    r = client.get("/v1/connectors")
    assert r.status_code == 200
    by_name = {c["connector"]: c for c in r.json()["connectors"]}
    # Custom REST connector shows up with both tools.
    assert "inventory" in by_name
    assert by_name["inventory"]["tools"] == ["get_sku", "search_skus"]
    assert by_name["inventory"]["tool_count"] == 2
    # Built-in mock connectors are listed too.
    assert "orders" in by_name
    assert "get_order" in by_name["orders"]["tools"]
