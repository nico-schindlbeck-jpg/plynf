# SPDX-License-Identifier: Apache-2.0
"""Tests for the identity-service client + JWT-based auth path."""

from __future__ import annotations

import httpx
import pytest
from fastapi.testclient import TestClient

from plinth_proxy import identity_client as ic
from plinth_proxy.api import create_app
from plinth_proxy.identity_client import IdentityClient, _tier_from_scopes
from plinth_proxy.settings import ProxySettings

_REAL_ASYNC_CLIENT = httpx.AsyncClient


def _mock_async_client(handler):
    transport = httpx.MockTransport(handler)

    class _Factory:
        def __init__(self, **_kw):
            self._client = _REAL_ASYNC_CLIENT(transport=transport)

        async def __aenter__(self):
            return self._client

        async def __aexit__(self, *exc):
            await self._client.aclose()

    return _Factory


# ---------------------------------------------------------------------------
# _tier_from_scopes
# ---------------------------------------------------------------------------


def test_tier_from_scopes_default_free():
    assert _tier_from_scopes([]) == "free"
    assert _tier_from_scopes(["workspace:read"]) == "free"


def test_tier_from_scopes_picks_highest():
    assert _tier_from_scopes(["tier:free", "tier:pro"]) == "pro"
    assert _tier_from_scopes(["tier:pro", "tier:enterprise"]) == "enterprise"


def test_tier_from_scopes_ignores_unknown():
    assert _tier_from_scopes(["tier:platinum"]) == "free"
    assert _tier_from_scopes(["tier:platinum", "tier:pro"]) == "pro"


# ---------------------------------------------------------------------------
# IdentityClient — happy path / errors / cache
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_identity_client_verifies_and_extracts_tier(monkeypatch):
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["body"] = request.content
        return httpx.Response(
            200,
            json={
                "sub": "agent-1",
                "iss": "plinth-identity",
                "aud": "plynf",
                "iat": 1_700_000_000,
                "exp": 2_000_000_000,
                "jti": "jti-abc",
                "agent_id": "agent-1",
                "tenant_id": "tenant-7",
                "scopes": ["workspace:read", "tier:pro"],
            },
        )

    monkeypatch.setattr(ic.httpx, "AsyncClient", _mock_async_client(handler))

    client = IdentityClient("http://identity:7425")
    claims = await client.verify("token-xyz")

    assert claims.tenant_id == "tenant-7"
    assert claims.tier == "pro"
    assert claims.agent_id == "agent-1"
    assert "tier:pro" in claims.scopes
    assert captured["url"].endswith("/v1/tokens/verify")


@pytest.mark.asyncio
async def test_identity_client_propagates_401(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, text="bad token")

    monkeypatch.setattr(ic.httpx, "AsyncClient", _mock_async_client(handler))
    client = IdentityClient("http://identity:7425")
    with pytest.raises(ic.IdentityError) as exc:
        await client.verify("x")
    assert exc.value.status == 401


@pytest.mark.asyncio
async def test_identity_client_caches_within_ttl(monkeypatch):
    call_count = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        call_count["n"] += 1
        return httpx.Response(
            200,
            json={
                "sub": "a", "iss": "i", "aud": "p", "iat": 1, "exp": 2_000_000_000,
                "jti": "j", "agent_id": "a", "tenant_id": "t", "scopes": ["tier:free"],
            },
        )

    monkeypatch.setattr(ic.httpx, "AsyncClient", _mock_async_client(handler))
    client = IdentityClient("http://identity:7425", cache_ttl_s=60)
    await client.verify("same-token")
    await client.verify("same-token")
    await client.verify("same-token")
    assert call_count["n"] == 1


# ---------------------------------------------------------------------------
# End-to-end auth path
# ---------------------------------------------------------------------------


def test_chat_endpoint_uses_identity_when_static_keys_miss(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "sub": "a", "iss": "i", "aud": "p", "iat": 1, "exp": 2_000_000_000,
                "jti": "j", "agent_id": "a", "tenant_id": "from-identity",
                "scopes": ["tier:enterprise"],
            },
        )

    monkeypatch.setattr(ic.httpx, "AsyncClient", _mock_async_client(handler))

    settings = ProxySettings(
        demo_mode=True,
        identity_url="http://identity.local:7425",
    )
    app = create_app(settings)
    client = TestClient(app)

    r = client.get("/v1/tier", headers={"Authorization": "Bearer jwt-token"})
    assert r.status_code == 200
    body = r.json()
    assert body["tenant_id"] == "from-identity"
    assert body["tier"] == "enterprise"


def test_chat_endpoint_returns_401_when_identity_rejects(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(403, text="revoked")

    monkeypatch.setattr(ic.httpx, "AsyncClient", _mock_async_client(handler))

    settings = ProxySettings(
        demo_mode=True,
        identity_url="http://identity.local:7425",
    )
    app = create_app(settings)
    client = TestClient(app)

    r = client.get("/v1/tier", headers={"Authorization": "Bearer revoked-token"})
    assert r.status_code == 401


def test_static_api_keys_take_priority_over_identity(monkeypatch):
    # Identity would say enterprise, but the static map labels this key 'free'.
    # Static map wins — it's the fast path.
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "sub": "a", "iss": "i", "aud": "p", "iat": 1, "exp": 2_000_000_000,
                "jti": "j", "agent_id": "a", "tenant_id": "would-be",
                "scopes": ["tier:enterprise"],
            },
        )

    monkeypatch.setattr(ic.httpx, "AsyncClient", _mock_async_client(handler))

    settings = ProxySettings(
        demo_mode=True,
        api_keys="tenant-static:static-key:free",
        identity_url="http://identity.local:7425",
    )
    app = create_app(settings)
    client = TestClient(app)

    r = client.get("/v1/tier", headers={"Authorization": "Bearer static-key"})
    assert r.status_code == 200
    body = r.json()
    assert body["tenant_id"] == "tenant-static"
    assert body["tier"] == "free"
