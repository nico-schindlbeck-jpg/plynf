# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 The Plinth Authors
"""Tests for the v0.4 RS256 + JWKS verification path on the gateway."""

from __future__ import annotations

import base64
import hashlib
from collections.abc import AsyncIterator
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx
import jwt as pyjwt
import pytest
import pytest_asyncio
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from httpx import ASGITransport, AsyncClient

from plinth_gateway.api import create_app
from plinth_gateway.jwt_auth import (
    AuthContext,
    JWKSCache,
    extract_auth_context_async,
    reset_jwks_cache,
)
from plinth_gateway.settings import Settings

UTC = timezone.utc

ISSUER = "http://identity.test"
AUDIENCE = "plinth"


def _gen_keypair() -> tuple[bytes, bytes, str]:
    private = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    private_pem = private.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )
    public_pem = private.public_key().public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    kid = hashlib.sha256(public_pem).hexdigest()[:16]
    return private_pem, public_pem, kid


def _b64url_uint(n: int) -> str:
    byte_len = (n.bit_length() + 7) // 8 or 1
    raw = n.to_bytes(byte_len, "big")
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _jwk_from_public_key(public_pem: bytes, kid: str) -> dict:
    public_key = serialization.load_pem_public_key(public_pem)
    nums = public_key.public_numbers()
    return {
        "kty": "RSA",
        "kid": kid,
        "alg": "RS256",
        "use": "sig",
        "n": _b64url_uint(nums.n),
        "e": _b64url_uint(nums.e),
    }


def _mint_rs256(
    private_pem: bytes,
    kid: str,
    *,
    agent_id: str = "agt_rs",
    tenant_id: str = "default",
    scopes: list[str] | None = None,
    iat: datetime | None = None,
    exp: datetime | None = None,
    audience: str = AUDIENCE,
    issuer: str = ISSUER,
    jti: str = "jti_rs",
) -> str:
    iat = iat or datetime.now(UTC)
    exp = exp or iat + timedelta(hours=1)
    payload = {
        "sub": agent_id,
        "iss": issuer,
        "aud": audience,
        "iat": int(iat.timestamp()),
        "exp": int(exp.timestamp()),
        "jti": jti,
        "agent_id": agent_id,
        "tenant_id": tenant_id,
        "workspace_id": None,
        "scopes": scopes or [],
    }
    token = pyjwt.encode(payload, private_pem, algorithm="RS256", headers={"kid": kid})
    if isinstance(token, bytes):
        token = token.decode("ascii")
    return token


def _mint_hs256(
    secret: str,
    *,
    agent_id: str = "agt_hs",
    tenant_id: str = "default",
    audience: str = AUDIENCE,
    issuer: str = ISSUER,
    jti: str = "jti_hs",
) -> str:
    iat = datetime.now(UTC)
    payload = {
        "sub": agent_id,
        "iss": issuer,
        "aud": audience,
        "iat": int(iat.timestamp()),
        "exp": int((iat + timedelta(hours=1)).timestamp()),
        "jti": jti,
        "agent_id": agent_id,
        "tenant_id": tenant_id,
        "scopes": [],
    }
    token = pyjwt.encode(payload, secret, algorithm="HS256")
    if isinstance(token, bytes):
        token = token.decode("ascii")
    return token


# ---------------------------------------------------------------------------
# Fixtures


class FakeJwks:
    def __init__(self, keys: list[dict]) -> None:
        self.keys = list(keys)
        self.fetch_count = 0

    def transport(self) -> httpx.MockTransport:
        async def handler(request: httpx.Request) -> httpx.Response:
            assert request.url.path == "/v1/.well-known/jwks.json"
            self.fetch_count += 1
            return httpx.Response(200, json={"keys": self.keys})

        return httpx.MockTransport(handler)


@pytest_asyncio.fixture()
async def rs_settings(tmp_path: Path) -> AsyncIterator[Settings]:
    settings = Settings(
        data_dir=tmp_path,
        gateway_host="127.0.0.1",
        gateway_port=7422,
        log_level="WARNING",
        log_format="console",
        backend_timeout_seconds=5.0,
        inbound_auth_required=False,
        rate_limits_enabled=False,
        auth_mode="verify_local",
        identity_url=ISSUER,
        jwt_audience=AUDIENCE,
        identity_jwks_cache_ttl_seconds=300,
    )
    yield settings
    await reset_jwks_cache(settings)


# ---------------------------------------------------------------------------
# JWKSCache unit tests


@pytest.mark.asyncio
async def test_jwks_cache_fetches_once_within_ttl():
    _, public_pem, kid = _gen_keypair()
    fake = FakeJwks([_jwk_from_public_key(public_pem, kid)])
    async with httpx.AsyncClient(transport=fake.transport()) as http:
        cache = JWKSCache(ISSUER, ttl_seconds=300, http_client=http)
        await cache.get(kid)
        await cache.get(kid)
        assert fake.fetch_count == 1


@pytest.mark.asyncio
async def test_jwks_cache_refreshes_when_kid_unknown():
    _, public_pem_a, kid_a = _gen_keypair()
    _, public_pem_b, kid_b = _gen_keypair()
    state = {"phase": "before"}

    async def handler(request: httpx.Request) -> httpx.Response:
        if state["phase"] == "before":
            return httpx.Response(200, json={"keys": [_jwk_from_public_key(public_pem_a, kid_a)]})
        return httpx.Response(
            200,
            json={
                "keys": [
                    _jwk_from_public_key(public_pem_a, kid_a),
                    _jwk_from_public_key(public_pem_b, kid_b),
                ]
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        cache = JWKSCache(ISSUER, ttl_seconds=300, http_client=http)
        await cache.get(kid_a)
        state["phase"] = "after"
        pem_b = await cache.get(kid_b)
        assert pem_b


@pytest.mark.asyncio
async def test_jwks_cache_unknown_kid_after_refresh_raises():
    _, public_pem_a, kid_a = _gen_keypair()
    fake = FakeJwks([_jwk_from_public_key(public_pem_a, kid_a)])
    async with httpx.AsyncClient(transport=fake.transport()) as http:
        cache = JWKSCache(ISSUER, ttl_seconds=300, http_client=http)
        from plinth_gateway.exceptions import Unauthorized

        with pytest.raises(Unauthorized):
            await cache.get("unknown-kid")


@pytest.mark.asyncio
async def test_jwks_cache_propagates_http_errors():
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        cache = JWKSCache(ISSUER, ttl_seconds=300, http_client=http)
        from plinth_gateway.exceptions import Unauthorized

        with pytest.raises(Unauthorized):
            await cache.get("any")


# ---------------------------------------------------------------------------
# Direct extract_auth_context_async tests


@pytest.mark.asyncio
async def test_extract_async_rs256_validates(monkeypatch, rs_settings):
    private_pem, public_pem, kid = _gen_keypair()
    fake = FakeJwks([_jwk_from_public_key(public_pem, kid)])

    from plinth_gateway import jwt_auth as auth_mod

    test_cache = JWKSCache(
        rs_settings.identity_url,
        ttl_seconds=300,
        http_client=httpx.AsyncClient(transport=fake.transport()),
    )
    monkeypatch.setitem(auth_mod._jwks_caches, id(rs_settings), test_cache)

    token = _mint_rs256(private_pem, kid, agent_id="agt_x", tenant_id="t-x")
    ctx = await extract_auth_context_async(f"Bearer {token}", rs_settings)
    assert ctx.tenant_id == "t-x"
    assert ctx.agent_id == "agt_x"


@pytest.mark.asyncio
async def test_extract_async_hs256_still_works(rs_settings):
    secret = "test-secret-44chars-for-hs256-cZbR3lKp9q8WxYzAAA="
    rs_settings.identity_jwt_secret = secret
    token = _mint_hs256(secret, tenant_id="legacy-hs")
    ctx = await extract_auth_context_async(f"Bearer {token}", rs_settings)
    assert ctx.tenant_id == "legacy-hs"


@pytest.mark.asyncio
async def test_extract_async_unknown_kid_rejected(monkeypatch, rs_settings):
    private_pem, _, _ = _gen_keypair()
    _, other_public, other_kid = _gen_keypair()
    fake = FakeJwks([_jwk_from_public_key(other_public, other_kid)])

    from plinth_gateway import jwt_auth as auth_mod
    from plinth_gateway.exceptions import Unauthorized

    test_cache = JWKSCache(
        rs_settings.identity_url,
        ttl_seconds=300,
        http_client=httpx.AsyncClient(transport=fake.transport()),
    )
    monkeypatch.setitem(auth_mod._jwks_caches, id(rs_settings), test_cache)

    token = _mint_rs256(private_pem, "made-up-kid")
    with pytest.raises(Unauthorized):
        await extract_auth_context_async(f"Bearer {token}", rs_settings)


@pytest.mark.asyncio
async def test_extract_async_expired_rs256_rejected(monkeypatch, rs_settings):
    private_pem, public_pem, kid = _gen_keypair()
    fake = FakeJwks([_jwk_from_public_key(public_pem, kid)])
    from plinth_gateway import jwt_auth as auth_mod
    from plinth_gateway.exceptions import Unauthorized

    test_cache = JWKSCache(
        rs_settings.identity_url,
        ttl_seconds=300,
        http_client=httpx.AsyncClient(transport=fake.transport()),
    )
    monkeypatch.setitem(auth_mod._jwks_caches, id(rs_settings), test_cache)

    expired = _mint_rs256(
        private_pem,
        kid,
        iat=datetime.now(UTC) - timedelta(hours=2),
        exp=datetime.now(UTC) - timedelta(hours=1),
    )
    with pytest.raises(Unauthorized) as exc:
        await extract_auth_context_async(f"Bearer {expired}", rs_settings)
    assert exc.value.code == "TOKEN_EXPIRED"


@pytest.mark.asyncio
async def test_extract_async_mixed_alg_deployment(monkeypatch, rs_settings):
    """Both HS256 and RS256 tokens flow through the same gateway."""

    secret = "test-secret-44chars-for-hs256-cZbR3lKp9q8WxYzAAA="
    rs_settings.identity_jwt_secret = secret

    private_pem, public_pem, kid = _gen_keypair()
    fake = FakeJwks([_jwk_from_public_key(public_pem, kid)])
    from plinth_gateway import jwt_auth as auth_mod

    test_cache = JWKSCache(
        rs_settings.identity_url,
        ttl_seconds=300,
        http_client=httpx.AsyncClient(transport=fake.transport()),
    )
    monkeypatch.setitem(auth_mod._jwks_caches, id(rs_settings), test_cache)

    rs_token = _mint_rs256(private_pem, kid, agent_id="rs-agt", tenant_id="rs-t")
    hs_token = _mint_hs256(secret, agent_id="hs-agt", tenant_id="hs-t")

    rs_ctx = await extract_auth_context_async(f"Bearer {rs_token}", rs_settings)
    hs_ctx = await extract_auth_context_async(f"Bearer {hs_token}", rs_settings)
    assert rs_ctx.tenant_id == "rs-t"
    assert hs_ctx.tenant_id == "hs-t"


# ---------------------------------------------------------------------------
# End-to-end via FastAPI


@pytest_asyncio.fixture()
async def rs_gateway_client(
    rs_settings: Settings, monkeypatch
) -> AsyncIterator[tuple[AsyncClient, FakeJwks, bytes, str]]:
    private_pem, public_pem, kid = _gen_keypair()
    fake = FakeJwks([_jwk_from_public_key(public_pem, kid)])

    from plinth_gateway import jwt_auth as auth_mod

    test_cache = JWKSCache(
        rs_settings.identity_url,
        ttl_seconds=300,
        http_client=httpx.AsyncClient(transport=fake.transport()),
    )
    monkeypatch.setitem(auth_mod._jwks_caches, id(rs_settings), test_cache)

    app = create_app(rs_settings)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as c, \
            app.router.lifespan_context(app):
        yield c, fake, private_pem, kid


def _tool_body(tool_id: str = "rs.tool") -> dict:
    return {
        "tool_id": tool_id,
        "name": tool_id,
        "description": "test",
        "transport": "http",
        "endpoint": "http://mcp.test/invoke",
        "input_schema": {"type": "object"},
        "output_schema": {"type": "object"},
    }


@pytest.mark.asyncio
async def test_middleware_accepts_rs256_token(rs_gateway_client):
    client, fake, private_pem, kid = rs_gateway_client
    token = _mint_rs256(private_pem, kid, tenant_id="rs-mid")
    r = await client.post(
        "/v1/tools/register",
        json=_tool_body(),
        headers={"Authorization": f"Bearer {token}"},
    )
    assert r.status_code == 201, r.text
    assert fake.fetch_count == 1


@pytest.mark.asyncio
async def test_middleware_rejects_unknown_kid(rs_gateway_client):
    client, _, private_pem, _ = rs_gateway_client
    bogus = _mint_rs256(private_pem, "not-in-jwks", tenant_id="t")
    r = await client.get(
        "/v1/tools",
        headers={"Authorization": f"Bearer {bogus}"},
    )
    assert r.status_code == 401


@pytest.mark.asyncio
async def test_middleware_jwks_caching_avoids_repeated_fetches(rs_gateway_client):
    client, fake, private_pem, kid = rs_gateway_client
    token = _mint_rs256(private_pem, kid, tenant_id="caching-test")
    for _ in range(5):
        r = await client.get(
            "/v1/tools",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert r.status_code == 200
    assert fake.fetch_count == 1


@pytest.mark.asyncio
async def test_middleware_rs256_filters_by_tenant(rs_gateway_client):
    client, fake, private_pem, kid = rs_gateway_client
    a_token = _mint_rs256(private_pem, kid, tenant_id="a", jti="jti_a")
    b_token = _mint_rs256(private_pem, kid, tenant_id="b", jti="jti_b")

    await client.post(
        "/v1/tools/register",
        json=_tool_body("only.in.a"),
        headers={"Authorization": f"Bearer {a_token}"},
    )
    a_list = await client.get(
        "/v1/tools",
        headers={"Authorization": f"Bearer {a_token}"},
    )
    b_list = await client.get(
        "/v1/tools",
        headers={"Authorization": f"Bearer {b_token}"},
    )
    a_ids = [t["tool_id"] for t in a_list.json()["tools"]]
    b_ids = [t["tool_id"] for t in b_list.json()["tools"]]
    assert "only.in.a" in a_ids
    assert "only.in.a" not in b_ids


# ---------------------------------------------------------------------------
# Sync facade — back-compat


def test_sync_extract_auth_context_handles_hs256(rs_settings):
    secret = "test-secret-44chars-for-hs256-cZbR3lKp9q8WxYzAAA="
    rs_settings.identity_jwt_secret = secret
    token = _mint_hs256(secret, tenant_id="legacy-2")
    from plinth_gateway.jwt_auth import extract_auth_context

    ctx = extract_auth_context(f"Bearer {token}", rs_settings)
    assert ctx.tenant_id == "legacy-2"


def test_sync_extract_auth_context_rejects_rs256(rs_settings):
    private_pem, _, kid = _gen_keypair()
    token = _mint_rs256(private_pem, kid)
    from plinth_gateway.exceptions import Unauthorized
    from plinth_gateway.jwt_auth import extract_auth_context

    with pytest.raises(Unauthorized):
        extract_auth_context(f"Bearer {token}", rs_settings)


def test_auth_context_dataclass_remains_stable():
    ctx = AuthContext(tenant_id="x", agent_id="y", scopes=["a"], jti="z", authenticated=True)
    assert ctx.tenant_id == "x"
    assert ctx.has_scope("a") is True
