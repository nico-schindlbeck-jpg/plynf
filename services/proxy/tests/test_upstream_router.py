# SPDX-License-Identifier: Apache-2.0
"""Multi-provider upstream routing.

Unit tests for the pure resolution logic (:mod:`plinth_proxy.upstream_router`)
plus integration tests that drive the chat-completions hot path through a fake
httpx client so we can assert *where* the proxy forwarded and *what* model it
sent — without any network.
"""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from plinth_proxy import api
from plinth_proxy.api import create_app
from plinth_proxy.settings import ProxySettings
from plinth_proxy.upstream_router import (
    ProviderRoute,
    UpstreamRouter,
    UpstreamTarget,
    parse_providers,
)

# ---------------------------------------------------------------------------
# parse_providers
# ---------------------------------------------------------------------------


def test_parse_providers_empty_returns_empty():
    assert parse_providers("") == []
    assert parse_providers("   ") == []


def test_parse_providers_basic_and_rstrips_base_url():
    raw = json.dumps(
        [{"name": "groq", "base_url": "https://api.groq.com/openai/", "api_key": "gk-1"}]
    )
    routes = parse_providers(raw)
    assert routes == [
        ProviderRoute(name="groq", base_url="https://api.groq.com/openai", api_key="gk-1")
    ]


def test_parse_providers_expands_env(monkeypatch):
    monkeypatch.setenv("MY_KEY", "secret-123")
    monkeypatch.setenv("MY_HOST", "https://host.example")
    raw = json.dumps([{"name": "x", "base_url": "${MY_HOST}", "api_key": "${MY_KEY}"}])
    (route,) = parse_providers(raw)
    assert route.base_url == "https://host.example"
    assert route.api_key == "secret-123"


def test_parse_providers_unset_env_becomes_empty(monkeypatch):
    monkeypatch.delenv("NOPE", raising=False)
    raw = json.dumps([{"name": "x", "base_url": "https://h.test", "api_key": "${NOPE}"}])
    (route,) = parse_providers(raw)
    assert route.api_key == ""


def test_parse_providers_skips_incomplete_entries():
    raw = json.dumps(
        [
            {"name": "ok", "base_url": "https://h.test"},
            {"name": "", "base_url": "https://h.test"},  # no name
            {"name": "nobase"},  # no base_url
            "not-an-object",
        ]
    )
    routes = parse_providers(raw)
    assert [r.name for r in routes] == ["ok"]


def test_parse_providers_dedupes_first_wins():
    raw = json.dumps(
        [
            {"name": "dup", "base_url": "https://first.test"},
            {"name": "dup", "base_url": "https://second.test"},
        ]
    )
    (route,) = parse_providers(raw)
    assert route.base_url == "https://first.test"


def test_parse_providers_malformed_json_raises():
    with pytest.raises(ValueError, match="not valid JSON"):
        parse_providers("{not json")


def test_parse_providers_non_array_raises():
    with pytest.raises(ValueError, match="must be a JSON array"):
        parse_providers(json.dumps({"name": "x"}))


# ---------------------------------------------------------------------------
# UpstreamRouter.resolve
# ---------------------------------------------------------------------------


def _router_with_groq(default_base="https://default.test", default_key="sk-def"):
    return UpstreamRouter(
        [ProviderRoute(name="groq", base_url="https://groq.test", api_key="gk-1")],
        default_base_url=default_base,
        default_api_key=default_key,
    )


def test_resolve_default_route_unchanged():
    r = UpstreamRouter(default_base_url="https://up.test/", default_api_key="sk-up")
    t = r.resolve("gpt-4o")
    assert t == UpstreamTarget(
        base_url="https://up.test", api_key="sk-up", model="gpt-4o", provider="default"
    )
    assert t.is_real is True


def test_resolve_provider_prefix_strips_and_routes():
    t = _router_with_groq().resolve("groq/llama-3.3-70b")
    assert t.base_url == "https://groq.test"
    assert t.api_key == "gk-1"
    assert t.model == "llama-3.3-70b"
    assert t.provider == "groq"


def test_resolve_only_strips_first_segment():
    r = UpstreamRouter(
        [ProviderRoute(name="openrouter", base_url="https://or.test")],
    )
    t = r.resolve("openrouter/anthropic/claude-3.5")
    assert t.provider == "openrouter"
    assert t.model == "anthropic/claude-3.5"  # remainder preserved


def test_resolve_unknown_prefix_falls_through_unmangled():
    # A HuggingFace-style id that isn't a configured provider must pass through
    # to the default upstream with the slash intact (never stripped).
    t = _router_with_groq().resolve("meta-llama/Llama-3-70b")
    assert t.provider == "default"
    assert t.base_url == "https://default.test"
    assert t.model == "meta-llama/Llama-3-70b"


def test_resolve_empty_remainder_not_treated_as_prefix():
    t = _router_with_groq().resolve("groq/")
    assert t.provider == "default"
    assert t.model == "groq/"


def test_resolve_header_override_wins_over_prefix():
    t = _router_with_groq().resolve(
        "groq/llama-3.3-70b",
        header_base_url="https://override.test/",
        header_api_key="hk-9",
    )
    assert t.provider == "header"
    assert t.base_url == "https://override.test"  # rstripped
    assert t.api_key == "hk-9"
    assert t.model == "groq/llama-3.3-70b"  # header route does not strip


def test_resolve_header_without_key_reuses_default_key():
    t = _router_with_groq(default_key="sk-def").resolve(
        "gpt-4o", header_base_url="https://override.test"
    )
    assert t.provider == "header"
    assert t.api_key == "sk-def"


def test_resolve_header_explicit_empty_key_is_keyless():
    t = _router_with_groq().resolve(
        "gpt-4o", header_base_url="https://override.test", header_api_key=""
    )
    assert t.api_key == ""  # caller deliberately sent an empty key


def test_resolve_no_default_unknown_model_is_not_real():
    r = UpstreamRouter()  # nothing configured
    t = r.resolve("gpt-4o")
    assert t.is_real is False
    assert t.base_url == ""


def test_resolve_provider_works_without_default_upstream():
    # Headline capability: configure ONLY providers, no global upstream.
    r = UpstreamRouter([ProviderRoute(name="groq", base_url="https://groq.test")])
    assert r.has_default is False
    t = r.resolve("groq/llama")
    assert t.is_real is True
    assert t.base_url == "https://groq.test"


def test_target_url_helpers():
    t = UpstreamTarget(base_url="https://h.test/", api_key="", model="m", provider="x")
    assert t.url("/v1/embeddings") == "https://h.test/v1/embeddings"
    assert t.chat_completions_url() == "https://h.test/v1/chat/completions"


def test_provider_names_sorted():
    r = UpstreamRouter(
        [
            ProviderRoute(name="zeta", base_url="https://z.test"),
            ProviderRoute(name="alpha", base_url="https://a.test"),
        ]
    )
    assert r.provider_names == ["alpha", "zeta"]


# ---------------------------------------------------------------------------
# UpstreamRouter.from_settings
# ---------------------------------------------------------------------------


def test_from_settings_builds_providers_and_default():
    s = ProxySettings(
        upstream_base_url="https://up.test",
        upstream_api_key="sk-up",
        providers=json.dumps([{"name": "groq", "base_url": "https://groq.test"}]),
    )
    r = UpstreamRouter.from_settings(s)
    assert r.provider_names == ["groq"]
    assert r.resolve("gpt-4o").base_url == "https://up.test"
    assert r.resolve("groq/x").base_url == "https://groq.test"


def test_from_settings_degrades_on_bad_json():
    s = ProxySettings(upstream_base_url="https://up.test", providers="{bad json")
    r = UpstreamRouter.from_settings(s)  # must not raise
    assert r.provider_names == []
    assert r.resolve("gpt-4o").base_url == "https://up.test"


# ---------------------------------------------------------------------------
# Integration: chat-completions hot path through a fake httpx client
# ---------------------------------------------------------------------------


class _FakeResp:
    def __init__(self, payload, status_code=200):
        self._payload = payload
        self.status_code = status_code
        self.text = json.dumps(payload)

    def json(self):
        return self._payload


class _FakePostClient:
    """Captures the single chat POST and returns a tool-call-free completion."""

    def __init__(self, captured: dict):
        self._captured = captured

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def post(self, url, json=None, headers=None):
        self._captured.update(url=url, json=json, headers=headers)
        return _FakeResp(
            {
                "id": "chatcmpl-test",
                "object": "chat.completion",
                "model": (json or {}).get("model", "?"),
                "choices": [
                    {
                        "index": 0,
                        "message": {"role": "assistant", "content": "ok"},
                        "finish_reason": "stop",
                    }
                ],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
            }
        )


def _client(monkeypatch, captured, **settings_kwargs):
    monkeypatch.setattr(
        api.httpx, "AsyncClient", lambda *a, **k: _FakePostClient(captured)
    )
    settings = ProxySettings(demo_mode=False, **settings_kwargs)
    return TestClient(create_app(settings))


def _chat(client, model, headers=None):
    return client.post(
        "/v1/chat/completions",
        json={"model": model, "messages": [{"role": "user", "content": "hi"}]},
        headers=headers or {},
    )


def test_chat_default_upstream_unchanged(monkeypatch):
    # Regression: with no providers, behave exactly like single-provider mode.
    captured: dict = {}
    client = _client(
        monkeypatch, captured, upstream_base_url="https://up.test", upstream_api_key="sk-up"
    )
    r = _chat(client, "gpt-4o")
    assert r.status_code == 200
    assert captured["url"] == "https://up.test/v1/chat/completions"
    assert captured["json"]["model"] == "gpt-4o"
    assert captured["headers"]["Authorization"] == "Bearer sk-up"


def test_chat_provider_prefix_routes_and_strips(monkeypatch):
    captured: dict = {}
    client = _client(
        monkeypatch,
        captured,
        upstream_base_url="https://up.test",
        upstream_api_key="sk-up",
        providers=json.dumps(
            [{"name": "groq", "base_url": "https://groq.test", "api_key": "gk-1"}]
        ),
    )
    r = _chat(client, "groq/llama-3.3-70b")
    assert r.status_code == 200
    assert captured["url"] == "https://groq.test/v1/chat/completions"
    assert captured["json"]["model"] == "llama-3.3-70b"  # prefix stripped
    assert captured["headers"]["Authorization"] == "Bearer gk-1"


def test_chat_provider_routing_without_default(monkeypatch):
    # No global upstream at all — provider config alone is enough.
    captured: dict = {}
    client = _client(
        monkeypatch,
        captured,
        providers=json.dumps(
            [{"name": "groq", "base_url": "https://groq.test", "api_key": "gk-1"}]
        ),
    )
    r = _chat(client, "groq/llama-3.3-70b")
    assert r.status_code == 200
    assert captured["url"] == "https://groq.test/v1/chat/completions"


def test_chat_header_override_wins(monkeypatch):
    captured: dict = {}
    client = _client(
        monkeypatch,
        captured,
        upstream_base_url="https://up.test",
        upstream_api_key="sk-up",
        providers=json.dumps([{"name": "groq", "base_url": "https://groq.test"}]),
    )
    r = _chat(
        client,
        "groq/llama-3.3-70b",
        headers={"X-Plynf-Upstream": "https://hdr.test", "X-Plynf-Upstream-Key": "hk-9"},
    )
    assert r.status_code == 200
    assert captured["url"] == "https://hdr.test/v1/chat/completions"
    assert captured["headers"]["Authorization"] == "Bearer hk-9"


def test_chat_header_override_reuses_default_key(monkeypatch):
    captured: dict = {}
    client = _client(
        monkeypatch, captured, upstream_base_url="https://up.test", upstream_api_key="sk-up"
    )
    r = _chat(client, "gpt-4o", headers={"X-Plynf-Upstream": "https://hdr.test"})
    assert r.status_code == 200
    assert captured["url"] == "https://hdr.test/v1/chat/completions"
    assert captured["headers"]["Authorization"] == "Bearer sk-up"


def test_chat_demo_mode_never_calls_upstream(monkeypatch):
    # demo_mode wins over any routing config: the fake client is never touched.
    captured: dict = {}
    monkeypatch.setattr(
        api.httpx, "AsyncClient", lambda *a, **k: _FakePostClient(captured)
    )
    settings = ProxySettings(
        demo_mode=True,
        providers=json.dumps([{"name": "groq", "base_url": "https://groq.test"}]),
    )
    client = TestClient(create_app(settings))
    r = _chat(client, "groq/llama-3.3-70b")
    assert r.status_code == 200
    assert captured == {}  # upstream not called — served by the mock


# ---------------------------------------------------------------------------
# Drop-in surface: /v1/embeddings + /v1/completions honor routing too
# ---------------------------------------------------------------------------


class _FakeForwardClient:
    """Captures the GET/request forward used by the drop-in endpoints."""

    def __init__(self, captured: dict, payload):
        self._captured = captured
        self._payload = payload

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def get(self, url, headers=None):
        self._captured.update(method="GET", url=url, headers=headers, json=None)
        return _FakeResp(self._payload)

    async def request(self, method, url, json=None, headers=None):
        self._captured.update(method=method, url=url, headers=headers, json=json)
        return _FakeResp(self._payload)


def _forward_client(monkeypatch, captured, payload, **settings_kwargs):
    monkeypatch.setattr(
        api.httpx, "AsyncClient", lambda *a, **k: _FakeForwardClient(captured, payload)
    )
    settings = ProxySettings(demo_mode=False, **settings_kwargs)
    return TestClient(create_app(settings))


def test_embeddings_default_upstream_unchanged(monkeypatch):
    captured: dict = {}
    client = _forward_client(
        monkeypatch,
        captured,
        {"object": "list", "data": []},
        upstream_base_url="https://up.test",
        upstream_api_key="sk-up",
    )
    r = client.post("/v1/embeddings", json={"input": "hi", "model": "text-embedding-3-small"})
    assert r.status_code == 200
    assert captured["url"] == "https://up.test/v1/embeddings"
    assert captured["json"]["model"] == "text-embedding-3-small"
    assert captured["headers"]["Authorization"] == "Bearer sk-up"


def test_embeddings_provider_prefix_routes_and_strips(monkeypatch):
    captured: dict = {}
    client = _forward_client(
        monkeypatch,
        captured,
        {"object": "list", "data": []},
        upstream_base_url="https://up.test",
        upstream_api_key="sk-up",
        providers=json.dumps(
            [{"name": "mistral", "base_url": "https://mistral.test", "api_key": "mk-1"}]
        ),
    )
    r = client.post("/v1/embeddings", json={"input": "hi", "model": "mistral/mistral-embed"})
    assert r.status_code == 200
    assert captured["url"] == "https://mistral.test/v1/embeddings"
    assert captured["json"]["model"] == "mistral-embed"  # prefix stripped
    assert captured["headers"]["Authorization"] == "Bearer mk-1"


def test_embeddings_header_override(monkeypatch):
    captured: dict = {}
    client = _forward_client(
        monkeypatch,
        captured,
        {"object": "list", "data": []},
        upstream_base_url="https://up.test",
        upstream_api_key="sk-up",
    )
    r = client.post(
        "/v1/embeddings",
        json={"input": "hi", "model": "text-embedding-3-small"},
        headers={"X-Plynf-Upstream": "https://hdr.test", "X-Plynf-Upstream-Key": "hk-2"},
    )
    assert r.status_code == 200
    assert captured["url"] == "https://hdr.test/v1/embeddings"
    assert captured["headers"]["Authorization"] == "Bearer hk-2"


def test_completions_provider_prefix_routes_and_strips(monkeypatch):
    captured: dict = {}
    client = _forward_client(
        monkeypatch,
        captured,
        {"object": "text_completion", "choices": []},
        upstream_base_url="https://up.test",
        upstream_api_key="sk-up",
        providers=json.dumps(
            [{"name": "together", "base_url": "https://together.test", "api_key": "tk-1"}]
        ),
    )
    r = client.post("/v1/completions", json={"prompt": "hi", "model": "together/mixtral"})
    assert r.status_code == 200
    assert captured["url"] == "https://together.test/v1/completions"
    assert captured["json"]["model"] == "mixtral"
    assert captured["headers"]["Authorization"] == "Bearer tk-1"


def test_models_listing_still_uses_default_upstream(monkeypatch):
    # GET /v1/models has no model to route on → always the default upstream.
    captured: dict = {}
    client = _forward_client(
        monkeypatch,
        captured,
        {"object": "list", "data": []},
        upstream_base_url="https://up.test",
        upstream_api_key="sk-up",
        providers=json.dumps([{"name": "groq", "base_url": "https://groq.test"}]),
    )
    r = client.get("/v1/models")
    assert r.status_code == 200
    assert captured["url"] == "https://up.test/v1/models"


# ---------------------------------------------------------------------------
# /v1/providers discoverability
# ---------------------------------------------------------------------------


def test_providers_endpoint_lists_configured_names():
    settings = ProxySettings(
        demo_mode=True,
        upstream_base_url="https://up.test",
        providers=json.dumps(
            [
                {"name": "groq", "base_url": "https://groq.test", "api_key": "secret"},
                {"name": "mistral", "base_url": "https://mistral.test"},
            ]
        ),
    )
    client = TestClient(create_app(settings))
    r = client.get("/v1/providers")
    assert r.status_code == 200
    body = r.json()
    assert body["providers"] == ["groq", "mistral"]  # sorted
    assert body["default"] is True
    assert body["prefix_routing"] is True
    # Never leak base URLs or keys.
    assert "secret" not in r.text
    assert "groq.test" not in r.text


def test_providers_endpoint_empty_when_none_configured():
    client = TestClient(create_app(ProxySettings(demo_mode=True)))
    r = client.get("/v1/providers")
    assert r.status_code == 200
    body = r.json()
    assert body["providers"] == []
    assert body["default"] is False
    assert body["prefix_routing"] is False
