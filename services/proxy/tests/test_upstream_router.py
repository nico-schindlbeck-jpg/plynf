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
    parse_aliases,
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


def test_models_listing_uses_default_upstream_without_providers(monkeypatch):
    # No providers / aliases configured → the listing forwards to the default
    # upstream unchanged (the multi-provider aggregator never engages). When
    # providers ARE configured the catalog is aggregated instead — see the
    # "/v1/models catalog aggregation" section below.
    captured: dict = {}
    client = _forward_client(
        monkeypatch,
        captured,
        {"object": "list", "data": []},
        upstream_base_url="https://up.test",
        upstream_api_key="sk-up",
    )
    r = client.get("/v1/models")
    assert r.status_code == 200
    assert captured["url"] == "https://up.test/v1/models"
    assert captured["headers"]["Authorization"] == "Bearer sk-up"


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
    assert body["aliases"] == []
    assert body["default"] is False
    assert body["prefix_routing"] is False


# ---------------------------------------------------------------------------
# Model aliases
# ---------------------------------------------------------------------------


def test_parse_aliases_empty():
    assert parse_aliases("") == {}
    assert parse_aliases("   ") == {}


def test_parse_aliases_basic_and_skips_blanks():
    raw = json.dumps({"fast": "groq/llama-3.1-8b", "blank": "", "": "x", "smart": "gpt-4o"})
    assert parse_aliases(raw) == {"fast": "groq/llama-3.1-8b", "smart": "gpt-4o"}


def test_parse_aliases_malformed_raises():
    with pytest.raises(ValueError, match="not valid JSON"):
        parse_aliases("{nope")


def test_parse_aliases_non_object_raises():
    with pytest.raises(ValueError, match="must be a JSON object"):
        parse_aliases(json.dumps(["fast", "smart"]))


def test_resolve_alias_to_provider_strips():
    r = UpstreamRouter(
        [ProviderRoute(name="groq", base_url="https://groq.test", api_key="gk-1")],
        aliases={"fast": "groq/llama-3.1-8b"},
    )
    t = r.resolve("fast")
    assert t.provider == "groq"
    assert t.model == "llama-3.1-8b"
    assert t.base_url == "https://groq.test"


def test_resolve_alias_to_plain_model_uses_default():
    r = UpstreamRouter(default_base_url="https://up.test", aliases={"smart": "gpt-4o"})
    t = r.resolve("smart")
    assert t.provider == "default"
    assert t.model == "gpt-4o"


def test_resolve_alias_is_single_level():
    # An alias whose target is itself an alias is NOT expanded recursively.
    r = UpstreamRouter(default_base_url="https://up.test", aliases={"a": "b", "b": "gpt-4o"})
    t = r.resolve("a")
    assert t.model == "b"


def test_resolve_non_alias_passes_through():
    r = UpstreamRouter(default_base_url="https://up.test", aliases={"fast": "gpt-4o"})
    assert r.resolve("gpt-3.5-turbo").model == "gpt-3.5-turbo"


def test_alias_names_sorted():
    r = UpstreamRouter(aliases={"zed": "x", "abe": "y"})
    assert r.alias_names == ["abe", "zed"]


def test_from_settings_parses_aliases():
    s = ProxySettings(
        upstream_base_url="https://up.test",
        model_aliases=json.dumps({"fast": "groq/llama"}),
    )
    r = UpstreamRouter.from_settings(s)
    assert r.alias_names == ["fast"]


def test_from_settings_degrades_on_bad_aliases():
    s = ProxySettings(upstream_base_url="https://up.test", model_aliases="{bad")
    r = UpstreamRouter.from_settings(s)  # must not raise
    assert r.alias_names == []


def test_chat_alias_routes_to_provider(monkeypatch):
    captured: dict = {}
    client = _client(
        monkeypatch,
        captured,
        upstream_base_url="https://up.test",
        upstream_api_key="sk-up",
        providers=json.dumps(
            [{"name": "groq", "base_url": "https://groq.test", "api_key": "gk-1"}]
        ),
        model_aliases=json.dumps({"fast": "groq/llama-3.3-70b"}),
    )
    r = _chat(client, "fast")
    assert r.status_code == 200
    assert captured["url"] == "https://groq.test/v1/chat/completions"
    assert captured["json"]["model"] == "llama-3.3-70b"
    assert captured["headers"]["Authorization"] == "Bearer gk-1"


def test_providers_endpoint_lists_alias_names_not_targets():
    settings = ProxySettings(
        demo_mode=True,
        model_aliases=json.dumps({"fast": "groq/llama-secret-model"}),
    )
    client = TestClient(create_app(settings))
    r = client.get("/v1/providers")
    assert r.status_code == 200
    assert r.json()["aliases"] == ["fast"]
    assert "llama-secret-model" not in r.text  # only names exposed


# ---------------------------------------------------------------------------
# Per-provider extra headers
# ---------------------------------------------------------------------------


def test_parse_providers_parses_headers():
    raw = json.dumps(
        [
            {
                "name": "or",
                "base_url": "https://or.test",
                "headers": {"HTTP-Referer": "https://app.test", "X-Title": "App"},
            }
        ]
    )
    (route,) = parse_providers(raw)
    assert route.headers == {"HTTP-Referer": "https://app.test", "X-Title": "App"}


def test_parse_providers_expands_env_in_headers(monkeypatch):
    monkeypatch.setenv("ORG", "org-42")
    raw = json.dumps(
        [{"name": "g", "base_url": "https://g.test", "headers": {"X-Org-Id": "${ORG}"}}]
    )
    (route,) = parse_providers(raw)
    assert route.headers == {"X-Org-Id": "org-42"}


def test_parse_providers_ignores_non_dict_headers():
    raw = json.dumps([{"name": "g", "base_url": "https://g.test", "headers": "nope"}])
    (route,) = parse_providers(raw)
    assert route.headers == {}


def test_parse_providers_skips_blank_header_keys():
    raw = json.dumps(
        [{"name": "g", "base_url": "https://g.test", "headers": {"  ": "v", "X-Ok": "1"}}]
    )
    (route,) = parse_providers(raw)
    assert route.headers == {"X-Ok": "1"}


def test_parse_providers_no_headers_defaults_empty():
    raw = json.dumps([{"name": "g", "base_url": "https://g.test"}])
    (route,) = parse_providers(raw)
    assert route.headers == {}


def test_resolve_prefix_carries_extra_headers():
    r = UpstreamRouter(
        [ProviderRoute(name="or", base_url="https://or.test", headers={"X-Title": "A"})]
    )
    t = r.resolve("or/some-model")
    assert t.extra_headers == {"X-Title": "A"}


def test_resolve_default_has_no_extra_headers():
    r = UpstreamRouter(default_base_url="https://up.test")
    assert r.resolve("gpt-4o").extra_headers == {}


def test_resolve_header_override_has_no_extra_headers():
    r = UpstreamRouter(
        [ProviderRoute(name="or", base_url="https://or.test", headers={"X-Title": "A"})]
    )
    t = r.resolve("or/m", header_base_url="https://hdr.test")
    assert t.extra_headers == {}


def test_chat_provider_extra_headers_forwarded(monkeypatch):
    captured: dict = {}
    client = _client(
        monkeypatch,
        captured,
        providers=json.dumps(
            [
                {
                    "name": "or",
                    "base_url": "https://or.test",
                    "api_key": "or-1",
                    "headers": {"HTTP-Referer": "https://app.test", "X-Title": "App"},
                }
            ]
        ),
    )
    r = _chat(client, "or/anthropic/claude-3.5")
    assert r.status_code == 200
    assert captured["url"] == "https://or.test/v1/chat/completions"
    assert captured["json"]["model"] == "anthropic/claude-3.5"  # only first segment stripped
    assert captured["headers"]["Authorization"] == "Bearer or-1"
    assert captured["headers"]["HTTP-Referer"] == "https://app.test"
    assert captured["headers"]["X-Title"] == "App"


def test_chat_provider_header_overrides_authorization(monkeypatch):
    # A provider that authenticates with a custom header wins over our Bearer.
    captured: dict = {}
    client = _client(
        monkeypatch,
        captured,
        providers=json.dumps(
            [
                {
                    "name": "az",
                    "base_url": "https://az.test",
                    "api_key": "ignored",
                    "headers": {"Authorization": "Custom abc", "api-key": "az-key"},
                }
            ]
        ),
    )
    r = _chat(client, "az/gpt-4o")
    assert r.status_code == 200
    assert captured["headers"]["Authorization"] == "Custom abc"
    assert captured["headers"]["api-key"] == "az-key"


def test_embeddings_provider_extra_headers_forwarded(monkeypatch):
    captured: dict = {}
    client = _forward_client(
        monkeypatch,
        captured,
        {"object": "list", "data": []},
        providers=json.dumps(
            [
                {
                    "name": "mistral",
                    "base_url": "https://mistral.test",
                    "api_key": "mk-1",
                    "headers": {"X-Org-Id": "org-7"},
                }
            ]
        ),
    )
    r = client.post("/v1/embeddings", json={"input": "hi", "model": "mistral/mistral-embed"})
    assert r.status_code == 200
    assert captured["headers"]["Authorization"] == "Bearer mk-1"
    assert captured["headers"]["X-Org-Id"] == "org-7"


def test_chat_default_route_sends_no_extra_headers(monkeypatch):
    # Regression: default upstream still sees exactly Authorization + Content-Type.
    captured: dict = {}
    client = _client(
        monkeypatch, captured, upstream_base_url="https://up.test", upstream_api_key="sk-up"
    )
    r = _chat(client, "gpt-4o")
    assert r.status_code == 200
    assert set(captured["headers"]) == {"Authorization", "Content-Type"}


def test_embeddings_provider_only_no_default_forwards(monkeypatch):
    # Regression: providers-only deployment (no upstream_base_url) must still
    # forward a prefixed model — previously the drop-in guard fell back to mock.
    captured: dict = {}
    client = _forward_client(
        monkeypatch,
        captured,
        {"object": "list", "data": []},
        providers=json.dumps(
            [{"name": "mistral", "base_url": "https://mistral.test", "api_key": "mk-1"}]
        ),
    )
    r = client.post("/v1/embeddings", json={"input": "hi", "model": "mistral/mistral-embed"})
    assert r.status_code == 200
    assert captured["url"] == "https://mistral.test/v1/embeddings"
    assert captured["json"]["model"] == "mistral-embed"


def test_completions_provider_only_no_default_forwards(monkeypatch):
    captured: dict = {}
    client = _forward_client(
        monkeypatch,
        captured,
        {"object": "text_completion", "choices": []},
        providers=json.dumps(
            [{"name": "together", "base_url": "https://together.test", "api_key": "tk-1"}]
        ),
    )
    r = client.post("/v1/completions", json={"prompt": "hi", "model": "together/mixtral"})
    assert r.status_code == 200
    assert captured["url"] == "https://together.test/v1/completions"
    assert captured["json"]["model"] == "mixtral"


def test_embeddings_no_upstream_at_all_mocks(monkeypatch):
    # No default, no provider match → deterministic mock, upstream untouched.
    captured: dict = {}
    client = _forward_client(monkeypatch, captured, {"object": "list", "data": []})
    r = client.post("/v1/embeddings", json={"input": "hi", "model": "gpt-4o"})
    assert r.status_code == 200
    assert captured == {}  # upstream not called — served by the mock


def test_embeddings_header_override_without_default_forwards(monkeypatch):
    # X-Plynf-Upstream alone (no default, no providers) routes the drop-in path.
    captured: dict = {}
    client = _forward_client(monkeypatch, captured, {"object": "list", "data": []})
    r = client.post(
        "/v1/embeddings",
        json={"input": "hi", "model": "text-embedding-3-small"},
        headers={"X-Plynf-Upstream": "https://hdr.test", "X-Plynf-Upstream-Key": "hk-9"},
    )
    assert r.status_code == 200
    assert captured["url"] == "https://hdr.test/v1/embeddings"
    assert captured["headers"]["Authorization"] == "Bearer hk-9"


# ---------------------------------------------------------------------------
# Routing observability — X-Plynf-Upstream-Provider response header
# ---------------------------------------------------------------------------


def test_chat_response_advertises_provider_prefix(monkeypatch):
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
    assert r.headers["x-plynf-upstream-provider"] == "groq"


def test_chat_response_advertises_default_provider(monkeypatch):
    captured: dict = {}
    client = _client(
        monkeypatch, captured, upstream_base_url="https://up.test", upstream_api_key="sk-up"
    )
    r = _chat(client, "gpt-4o")
    assert r.headers["x-plynf-upstream-provider"] == "default"


def test_chat_response_advertises_header_override(monkeypatch):
    captured: dict = {}
    client = _client(
        monkeypatch, captured, upstream_base_url="https://up.test", upstream_api_key="sk-up"
    )
    r = _chat(client, "gpt-4o", headers={"X-Plynf-Upstream": "https://hdr.test"})
    assert r.headers["x-plynf-upstream-provider"] == "header"


def test_chat_demo_mode_omits_provider_header():
    # Mock mode contacts no upstream → it must not claim a provider.
    client = TestClient(create_app(ProxySettings(demo_mode=True)))
    r = client.post(
        "/v1/chat/completions",
        json={"model": "gpt-4o", "messages": [{"role": "user", "content": "hi"}]},
    )
    assert r.status_code == 200
    assert "x-plynf-upstream-provider" not in r.headers


def test_chat_streaming_advertises_provider_header(monkeypatch):
    captured: dict = {}
    client = _client(
        monkeypatch,
        captured,
        providers=json.dumps(
            [{"name": "groq", "base_url": "https://groq.test", "api_key": "gk-1"}]
        ),
    )
    r = client.post(
        "/v1/chat/completions",
        json={
            "model": "groq/llama-3.3-70b",
            "messages": [{"role": "user", "content": "hi"}],
            "stream": True,
        },
    )
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/event-stream")
    assert r.headers["x-plynf-upstream-provider"] == "groq"


def test_embeddings_response_advertises_provider_header(monkeypatch):
    captured: dict = {}
    client = _forward_client(
        monkeypatch,
        captured,
        {"object": "list", "data": []},
        providers=json.dumps(
            [{"name": "mistral", "base_url": "https://mistral.test", "api_key": "mk-1"}]
        ),
    )
    r = client.post("/v1/embeddings", json={"input": "hi", "model": "mistral/mistral-embed"})
    assert r.headers["x-plynf-upstream-provider"] == "mistral"


# ---------------------------------------------------------------------------
# Uniform per-request routing across the native-dialect front doors
# (X-Plynf-Upstream override + provider/model prefix + provider header).
# ---------------------------------------------------------------------------

_GROQ_PROVIDERS = json.dumps(
    [{"name": "groq", "base_url": "https://groq.test", "api_key": "gk-1"}]
)
_OVERRIDE = {"X-Plynf-Upstream": "https://hdr.test", "X-Plynf-Upstream-Key": "hk-9"}


def test_anthropic_messages_honors_upstream_override(monkeypatch):
    captured: dict = {}
    client = _client(monkeypatch, captured, upstream_base_url="https://up.test")
    r = client.post(
        "/v1/messages",
        json={
            "model": "claude-3-5-sonnet",
            "max_tokens": 1024,
            "messages": [{"role": "user", "content": "Hello"}],
        },
        headers=_OVERRIDE,
    )
    assert r.status_code == 200
    assert captured["url"] == "https://hdr.test/v1/chat/completions"
    assert captured["headers"]["Authorization"] == "Bearer hk-9"
    assert r.headers["x-plynf-upstream-provider"] == "header"


def test_anthropic_messages_honors_provider_prefix(monkeypatch):
    captured: dict = {}
    client = _client(monkeypatch, captured, providers=_GROQ_PROVIDERS)
    r = client.post(
        "/v1/messages",
        json={
            "model": "groq/claude-x",
            "max_tokens": 1024,
            "messages": [{"role": "user", "content": "Hello"}],
        },
    )
    assert r.status_code == 200
    assert captured["url"] == "https://groq.test/v1/chat/completions"
    assert captured["json"]["model"] == "claude-x"  # prefix stripped
    assert r.headers["x-plynf-upstream-provider"] == "groq"


def test_cohere_chat_honors_upstream_override(monkeypatch):
    captured: dict = {}
    client = _client(monkeypatch, captured, upstream_base_url="https://up.test")
    r = client.post(
        "/v2/chat",
        json={"model": "command-r-plus", "messages": [{"role": "user", "content": "hi"}]},
        headers=_OVERRIDE,
    )
    assert r.status_code == 200
    assert captured["url"] == "https://hdr.test/v1/chat/completions"
    assert r.headers["x-plynf-upstream-provider"] == "header"


def test_responses_honors_provider_prefix(monkeypatch):
    captured: dict = {}
    client = _client(monkeypatch, captured, providers=_GROQ_PROVIDERS)
    r = client.post("/v1/responses", json={"model": "groq/gpt-x", "input": "hello"})
    assert r.status_code == 200
    assert captured["url"] == "https://groq.test/v1/chat/completions"
    assert captured["json"]["model"] == "gpt-x"
    assert r.headers["x-plynf-upstream-provider"] == "groq"


def test_gemini_generate_content_honors_upstream_override(monkeypatch):
    # Gemini carries the model in the URL (no slashes), so the header override
    # is the per-request routing mechanism here — assert it now threads through.
    captured: dict = {}
    client = _client(monkeypatch, captured, upstream_base_url="https://up.test")
    r = client.post(
        "/v1beta/models/gemini-1.5-pro:generateContent",
        json={"contents": [{"role": "user", "parts": [{"text": "hi"}]}]},
        headers=_OVERRIDE,
    )
    assert r.status_code == 200
    assert captured["url"] == "https://hdr.test/v1/chat/completions"
    assert r.headers["x-plynf-upstream-provider"] == "header"


def test_bedrock_converse_honors_upstream_override(monkeypatch):
    captured: dict = {}
    client = _client(monkeypatch, captured, upstream_base_url="https://up.test")
    r = client.post(
        "/model/anthropic.claude-3-5-sonnet/converse",
        json={"messages": [{"role": "user", "content": [{"text": "Hello"}]}]},
        headers=_OVERRIDE,
    )
    assert r.status_code == 200
    assert captured["url"] == "https://hdr.test/v1/chat/completions"
    assert r.headers["x-plynf-upstream-provider"] == "header"


def test_bedrock_converse_honors_provider_prefix(monkeypatch):
    # The {model_id:path} route captures slashes, so a provider prefix in the
    # Bedrock model path routes too (groq/llama-3.3-70b → groq + llama-3.3-70b).
    captured: dict = {}
    client = _client(monkeypatch, captured, providers=_GROQ_PROVIDERS)
    r = client.post(
        "/model/groq/llama-3.3-70b/converse",
        json={"messages": [{"role": "user", "content": [{"text": "Hello"}]}]},
    )
    assert r.status_code == 200
    assert captured["url"] == "https://groq.test/v1/chat/completions"
    assert captured["json"]["model"] == "llama-3.3-70b"
    assert r.headers["x-plynf-upstream-provider"] == "groq"


# ---------------------------------------------------------------------------
# GET /v1/models — multi-provider catalog aggregation
# ---------------------------------------------------------------------------


class _FakeModelsClient:
    """Per-source fake for the ``/v1/models`` fan-out: maps GET URL → ListModels.

    ``state`` is shared across the (one-per-source) instances the aggregation
    constructs, so ``state["calls"]`` accumulates every URL hit. A URL listed in
    ``state["fail"]`` raises, simulating an unreachable provider.
    """

    def __init__(self, state: dict):
        self._state = state

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def get(self, url, headers=None):
        self._state.setdefault("calls", []).append(url)
        if url in self._state.get("fail", set()):
            raise RuntimeError("provider unreachable")
        payload = self._state.get("by_url", {}).get(url, {"object": "list", "data": []})
        return _FakeResp(payload)


def _models_client(monkeypatch, state, **settings_kwargs):
    monkeypatch.setattr(
        api.httpx, "AsyncClient", lambda *a, **k: _FakeModelsClient(state)
    )
    settings = ProxySettings(demo_mode=False, **settings_kwargs)
    return TestClient(create_app(settings))


def _models_payload(*ids):
    return {"object": "list", "data": [{"id": i, "object": "model"} for i in ids]}


def _ids(resp):
    return [m["id"] for m in resp.json()["data"]]


def test_models_aggregates_default_and_providers(monkeypatch):
    state = {
        "by_url": {
            "https://up.test/v1/models": _models_payload("gpt-4o", "gpt-4o-mini"),
            "https://groq.test/v1/models": _models_payload("llama-3.3-70b"),
        }
    }
    client = _models_client(
        monkeypatch,
        state,
        upstream_base_url="https://up.test",
        upstream_api_key="sk-up",
        providers=_GROQ_PROVIDERS,
    )
    r = client.get("/v1/models")
    assert r.status_code == 200
    ids = _ids(r)
    assert "gpt-4o" in ids  # default upstream — unprefixed
    assert "gpt-4o-mini" in ids
    assert "groq/llama-3.3-70b" in ids  # provider — prefixed, directly routable
    assert set(state["calls"]) == {
        "https://up.test/v1/models",
        "https://groq.test/v1/models",
    }


def test_models_providers_only_no_default(monkeypatch):
    state = {"by_url": {"https://groq.test/v1/models": _models_payload("llama-3.3-70b")}}
    client = _models_client(monkeypatch, state, providers=_GROQ_PROVIDERS)
    r = client.get("/v1/models")
    assert r.status_code == 200
    assert _ids(r) == ["groq/llama-3.3-70b"]
    assert state["calls"] == ["https://groq.test/v1/models"]


def test_models_includes_aliases_as_synthetic(monkeypatch):
    state = {"by_url": {"https://groq.test/v1/models": _models_payload("llama-3.3-70b")}}
    client = _models_client(
        monkeypatch,
        state,
        providers=_GROQ_PROVIDERS,
        model_aliases=json.dumps({"fast": "groq/llama-3.3-70b"}),
    )
    r = client.get("/v1/models")
    data = {m["id"]: m for m in r.json()["data"]}
    assert "groq/llama-3.3-70b" in data
    assert data["fast"]["owned_by"] == "plynf-alias"  # alias is itself routable


def test_models_skips_unreachable_provider(monkeypatch):
    # groq is reachable; "down" raises — the catalog still returns groq's models
    # rather than 500-ing the whole listing.
    providers = json.dumps(
        [
            {"name": "groq", "base_url": "https://groq.test", "api_key": "gk-1"},
            {"name": "down", "base_url": "https://down.test", "api_key": "dk"},
        ]
    )
    state = {
        "by_url": {"https://groq.test/v1/models": _models_payload("llama-3.3-70b")},
        "fail": {"https://down.test/v1/models"},
    }
    client = _models_client(monkeypatch, state, providers=providers)
    r = client.get("/v1/models")
    assert r.status_code == 200
    assert _ids(r) == ["groq/llama-3.3-70b"]


def test_models_alias_does_not_duplicate_existing_id(monkeypatch):
    state = {"by_url": {"https://up.test/v1/models": _models_payload("gpt-4o")}}
    client = _models_client(
        monkeypatch,
        state,
        upstream_base_url="https://up.test",
        model_aliases=json.dumps({"gpt-4o": "openai/gpt-4o"}),
    )
    r = client.get("/v1/models")
    assert _ids(r).count("gpt-4o") == 1  # alias didn't duplicate the upstream id


def test_models_demo_mode_serves_mock_without_fanout(monkeypatch):
    state: dict = {"by_url": {}}
    monkeypatch.setattr(
        api.httpx, "AsyncClient", lambda *a, **k: _FakeModelsClient(state)
    )
    settings = ProxySettings(demo_mode=True, providers=_GROQ_PROVIDERS)
    client = TestClient(create_app(settings))
    r = client.get("/v1/models")
    assert r.status_code == 200
    assert state.get("calls", []) == []  # demo mode never fans out to upstreams
    assert r.json()["object"] == "list"


def test_models_single_provider_path_unchanged(monkeypatch):
    # No providers, no aliases: the legacy direct-forward path runs (NOT the
    # aggregator), so the upstream payload is returned verbatim — an extra
    # top-level field survives instead of being rebuilt into a clean envelope.
    raw = {"object": "list", "data": [{"id": "gpt-4o", "object": "model"}], "x_extra": 1}
    state = {"by_url": {"https://up.test/v1/models": raw}}
    client = _models_client(monkeypatch, state, upstream_base_url="https://up.test")
    r = client.get("/v1/models")
    assert r.status_code == 200
    assert r.json() == raw  # verbatim forward — extra field preserved
