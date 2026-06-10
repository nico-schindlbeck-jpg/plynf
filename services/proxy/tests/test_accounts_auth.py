# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 The Plynf Authors
"""Proxy auth via the dashboard accounts resolver (self-serve keys)."""

from __future__ import annotations

import httpx
import pytest
from fastapi import HTTPException

from plinth_proxy.accounts_client import AccountsKeyClient

KEY = "plynf_sk_live_" + "ab" * 24


def make_client(handler, **kwargs) -> AccountsKeyClient:
    transport = httpx.MockTransport(handler)
    http = httpx.AsyncClient(transport=transport)
    return AccountsKeyClient("http://dash:7424", "s3cret", client=http, **kwargs)


async def test_resolve_known_key():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["x-internal-secret"] == "s3cret"
        assert request.url.path == f"/internal/keys/{KEY}"
        return httpx.Response(200, json={"tenant_id": "t_1", "tier": "pro"})

    client = make_client(handler)
    assert await client.resolve(KEY) == ("t_1", "pro")


async def test_resolve_unknown_key_none_and_negative_cache():
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(404, json={"error": {}})

    client = make_client(handler)
    assert await client.resolve(KEY) is None
    assert await client.resolve(KEY) is None  # served from negative cache
    assert calls["n"] == 1


async def test_resolve_caches_positive_results():
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(200, json={"tenant_id": "t_1", "tier": "free"})

    client = make_client(handler)
    await client.resolve(KEY)
    await client.resolve(KEY)
    assert calls["n"] == 1
    client.clear_cache()
    await client.resolve(KEY)
    assert calls["n"] == 2


async def test_resolver_outage_degrades_gracefully():
    state = {"up": True}

    def handler(request: httpx.Request) -> httpx.Response:
        if not state["up"]:
            raise httpx.ConnectError("down")
        return httpx.Response(200, json={"tenant_id": "t_1", "tier": "pro"})

    client = make_client(handler)
    assert await client.resolve(KEY) == ("t_1", "pro")
    state["up"] = False
    client.clear_cache()
    # No cache and control plane down → None (request will 401, not 500).
    assert await client.resolve(KEY) is None


# -- end-to-end through the proxy's _authenticate ----------------------------


async def test_authenticate_uses_accounts_resolver(monkeypatch):
    from plinth_proxy import api as proxy_api
    from plinth_proxy.settings import ProxySettings

    def handler(request: httpx.Request) -> httpx.Response:
        if KEY in request.url.path:
            return httpx.Response(200, json={"tenant_id": "t_77", "tier": "pro"})
        return httpx.Response(404, json={"error": {}})

    settings = ProxySettings(accounts_url="http://dash:7424", internal_secret="s3cret")
    state = proxy_api._build_state(settings)
    assert state.accounts is not None
    state.accounts._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))

    class FakeRequest:
        def __init__(self, token: str | None):
            self.headers = {"authorization": f"Bearer {token}"} if token else {}

    tenant, tier = await proxy_api._authenticate(FakeRequest(KEY), state)
    assert (tenant, tier) == ("t_77", "pro")

    # Unknown key → 401 (not open mode!) because accounts mode is enabled.
    with pytest.raises(HTTPException) as e:
        await proxy_api._authenticate(FakeRequest("plynf_sk_live_unknown"), state)
    assert e.value.status_code == 401

    # Missing token → 401 in accounts mode.
    with pytest.raises(HTTPException) as e:
        await proxy_api._authenticate(FakeRequest(None), state)
    assert e.value.status_code == 401
