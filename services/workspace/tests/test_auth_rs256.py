# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 The Plinth Authors
"""Tests for the v0.4 RS256 + JWKS verification path on the workspace.

Mocks the identity service's JWKS endpoint with httpx (so the workspace
fetches a fresh keyset without spinning up a second FastAPI app), then
exercises the verifier directly + via the FastAPI middleware.
"""

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
from httpx import ASGITransport

from plinth_workspace.api import create_app
from plinth_workspace.auth import (
    AuthContext,
    JWKSCache,
    extract_auth_context_async,
    reset_jwks_cache,
)
from plinth_workspace.db import init_db
from plinth_workspace.settings import Settings

UTC = timezone.utc

ISSUER = "http://identity.test"
AUDIENCE = "plinth"


def _gen_keypair() -> tuple[rsa.RSAPrivateKey, bytes, bytes, str]:
    """Generate an RSA keypair and return (private, private_pem, public_pem, kid)."""

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
    return private, private_pem, public_pem, kid


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
    """Records JWKS fetches and returns a configurable keyset."""

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
    """A workspace Settings configured for RS256 verify_local mode."""

    data_dir = tmp_path / "plinth-data"
    data_dir.mkdir(parents=True, exist_ok=True)
    settings = Settings(
        data_dir=data_dir,
        workspace_port=17421,
        workspace_host="127.0.0.1",
        log_level="WARNING",
        log_format="console",
        auth_required=False,
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
    _, _, public_pem, kid = _gen_keypair()
    fake = FakeJwks([_jwk_from_public_key(public_pem, kid)])
    async with httpx.AsyncClient(transport=fake.transport()) as http:
        cache = JWKSCache(ISSUER, ttl_seconds=300, http_client=http)
        pem1 = await cache.get(kid)
        pem2 = await cache.get(kid)
        assert pem1 == pem2
        assert fake.fetch_count == 1


@pytest.mark.asyncio
async def test_jwks_cache_refreshes_when_kid_unknown():
    _, _, public_pem_a, kid_a = _gen_keypair()
    _, _, public_pem_b, kid_b = _gen_keypair()
    # First response has only key A; the second has both A and B.
    initial = [_jwk_from_public_key(public_pem_a, kid_a)]
    after_rotation = [
        _jwk_from_public_key(public_pem_a, kid_a),
        _jwk_from_public_key(public_pem_b, kid_b),
    ]
    state = {"phase": "before"}

    async def handler(request: httpx.Request) -> httpx.Response:
        if state["phase"] == "before":
            return httpx.Response(200, json={"keys": initial})
        return httpx.Response(200, json={"keys": after_rotation})

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as http:
        cache = JWKSCache(ISSUER, ttl_seconds=300, http_client=http)
        await cache.get(kid_a)
        # Now the kid we're asking for isn't cached → forces a refresh.
        state["phase"] = "after"
        pem_b = await cache.get(kid_b)
        assert pem_b


@pytest.mark.asyncio
async def test_jwks_cache_unknown_kid_after_refresh_raises():
    _, _, public_pem_a, kid_a = _gen_keypair()
    fake = FakeJwks([_jwk_from_public_key(public_pem_a, kid_a)])
    async with httpx.AsyncClient(transport=fake.transport()) as http:
        cache = JWKSCache(ISSUER, ttl_seconds=300, http_client=http)
        from plinth_workspace.exceptions import Unauthorized

        with pytest.raises(Unauthorized):
            await cache.get("non-existent-kid")


@pytest.mark.asyncio
async def test_jwks_cache_propagates_http_errors():
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        cache = JWKSCache(ISSUER, ttl_seconds=300, http_client=http)
        from plinth_workspace.exceptions import Unauthorized

        with pytest.raises(Unauthorized):
            await cache.get("any")


# ---------------------------------------------------------------------------
# extract_auth_context_async — RS256 + HS256 mixed


@pytest.mark.asyncio
async def test_extract_async_rs256_token_validates(monkeypatch, rs_settings):
    private, private_pem, public_pem, kid = _gen_keypair()
    fake = FakeJwks([_jwk_from_public_key(public_pem, kid)])

    # Inject our mock httpx transport into the cache.
    from plinth_workspace import auth as auth_mod

    test_cache = JWKSCache(
        rs_settings.identity_url,
        ttl_seconds=300,
        http_client=httpx.AsyncClient(transport=fake.transport()),
    )
    monkeypatch.setitem(auth_mod._jwks_caches, id(rs_settings), test_cache)

    token = _mint_rs256(private_pem, kid, agent_id="agt_a", tenant_id="acme")
    ctx = await extract_auth_context_async(f"Bearer {token}", rs_settings)
    assert ctx.tenant_id == "acme"
    assert ctx.agent_id == "agt_a"
    assert ctx.authenticated


@pytest.mark.asyncio
async def test_extract_async_hs256_still_works(rs_settings):
    """A workspace can verify HS256 tokens even when configured for RS256-friendly mode."""

    secret = "test-secret-44chars-for-hs256-cZbR3lKp9q8WxYzAAA="
    rs_settings.identity_jwt_secret = secret
    token = _mint_hs256(secret, agent_id="agt_hs", tenant_id="hs-tenant")
    ctx = await extract_auth_context_async(f"Bearer {token}", rs_settings)
    assert ctx.tenant_id == "hs-tenant"
    assert ctx.authenticated


@pytest.mark.asyncio
async def test_extract_async_unknown_kid_returns_unauthorized(monkeypatch, rs_settings):
    """A token with an unrecognised kid is rejected after a JWKS refresh."""

    _, private_pem, public_pem, _ = _gen_keypair()
    # Mint with one kid, but the JWKS only knows a different one.
    _, _, _, other_kid = _gen_keypair()
    fake = FakeJwks([_jwk_from_public_key(public_pem, other_kid)])

    from plinth_workspace import auth as auth_mod
    from plinth_workspace.exceptions import Unauthorized

    test_cache = JWKSCache(
        rs_settings.identity_url,
        ttl_seconds=300,
        http_client=httpx.AsyncClient(transport=fake.transport()),
    )
    monkeypatch.setitem(auth_mod._jwks_caches, id(rs_settings), test_cache)

    token = _mint_rs256(private_pem, "unknown-kid")
    with pytest.raises(Unauthorized):
        await extract_auth_context_async(f"Bearer {token}", rs_settings)


@pytest.mark.asyncio
async def test_extract_async_expired_rs256_token_rejected(monkeypatch, rs_settings):
    private, private_pem, public_pem, kid = _gen_keypair()
    fake = FakeJwks([_jwk_from_public_key(public_pem, kid)])
    from plinth_workspace import auth as auth_mod
    from plinth_workspace.exceptions import Unauthorized

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
async def test_extract_async_wrong_audience_rs256_rejected(monkeypatch, rs_settings):
    private, private_pem, public_pem, kid = _gen_keypair()
    fake = FakeJwks([_jwk_from_public_key(public_pem, kid)])
    from plinth_workspace import auth as auth_mod
    from plinth_workspace.exceptions import Unauthorized

    test_cache = JWKSCache(
        rs_settings.identity_url,
        ttl_seconds=300,
        http_client=httpx.AsyncClient(transport=fake.transport()),
    )
    monkeypatch.setitem(auth_mod._jwks_caches, id(rs_settings), test_cache)

    token = _mint_rs256(private_pem, kid, audience="someone-else")
    with pytest.raises(Unauthorized):
        await extract_auth_context_async(f"Bearer {token}", rs_settings)


@pytest.mark.asyncio
async def test_extract_async_rs256_and_hs256_both_accepted(monkeypatch, rs_settings):
    """Mixed deployment: a single workspace verifies both RS256 and HS256 tokens."""

    secret = "test-secret-44chars-for-hs256-cZbR3lKp9q8WxYzAAA="
    rs_settings.identity_jwt_secret = secret

    private, private_pem, public_pem, kid = _gen_keypair()
    fake = FakeJwks([_jwk_from_public_key(public_pem, kid)])
    from plinth_workspace import auth as auth_mod

    test_cache = JWKSCache(
        rs_settings.identity_url,
        ttl_seconds=300,
        http_client=httpx.AsyncClient(transport=fake.transport()),
    )
    monkeypatch.setitem(auth_mod._jwks_caches, id(rs_settings), test_cache)

    rs_token = _mint_rs256(private_pem, kid, agent_id="agent-rs", tenant_id="rs-tenant")
    hs_token = _mint_hs256(secret, agent_id="agent-hs", tenant_id="hs-tenant")

    rs_ctx = await extract_auth_context_async(f"Bearer {rs_token}", rs_settings)
    hs_ctx = await extract_auth_context_async(f"Bearer {hs_token}", rs_settings)
    assert rs_ctx.tenant_id == "rs-tenant"
    assert hs_ctx.tenant_id == "hs-tenant"


@pytest.mark.asyncio
async def test_extract_async_missing_kid_rs256_rejected(rs_settings):
    """An RS256 token with no ``kid`` header is rejected before the JWKS round-trip."""

    private, private_pem, _, _ = _gen_keypair()
    payload = {
        "sub": "x",
        "iss": ISSUER,
        "aud": AUDIENCE,
        "iat": int(datetime.now(UTC).timestamp()),
        "exp": int((datetime.now(UTC) + timedelta(hours=1)).timestamp()),
        "jti": "jti_x",
    }
    forged = pyjwt.encode(payload, private_pem, algorithm="RS256")
    if isinstance(forged, bytes):
        forged = forged.decode("ascii")
    from plinth_workspace.exceptions import Unauthorized

    with pytest.raises(Unauthorized):
        await extract_auth_context_async(f"Bearer {forged}", rs_settings)


# ---------------------------------------------------------------------------
# End-to-end via FastAPI middleware


@pytest_asyncio.fixture()
async def rs_workspace_client(
    rs_settings: Settings, monkeypatch
) -> AsyncIterator[tuple[httpx.AsyncClient, FakeJwks, bytes, str]]:
    private, private_pem, public_pem, kid = _gen_keypair()
    fake = FakeJwks([_jwk_from_public_key(public_pem, kid)])

    from plinth_workspace import auth as auth_mod

    test_cache = JWKSCache(
        rs_settings.identity_url,
        ttl_seconds=300,
        http_client=httpx.AsyncClient(transport=fake.transport()),
    )
    monkeypatch.setitem(auth_mod._jwks_caches, id(rs_settings), test_cache)

    rs_settings.data_dir.mkdir(parents=True, exist_ok=True)
    rs_settings.blobs_dir.mkdir(parents=True, exist_ok=True)
    await init_db(rs_settings.db_path)
    app = create_app(rs_settings)
    transport = ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c, fake, private_pem, kid


@pytest.mark.asyncio
async def test_middleware_accepts_rs256_token(rs_workspace_client):
    client, fake, private_pem, kid = rs_workspace_client
    token = _mint_rs256(private_pem, kid, agent_id="agt_e2e", tenant_id="e2e")
    r = await client.post(
        "/v1/workspaces",
        json={"name": "ws-rs"},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert r.status_code == 201, r.text
    assert r.json()["tenant_id"] == "e2e"
    assert fake.fetch_count == 1


@pytest.mark.asyncio
async def test_middleware_rejects_unknown_kid(rs_workspace_client):
    client, fake, private_pem, _ = rs_workspace_client
    bogus = _mint_rs256(private_pem, "unknown-kid", agent_id="agt", tenant_id="t")
    r = await client.get(
        "/v1/workspaces",
        headers={"Authorization": f"Bearer {bogus}"},
    )
    assert r.status_code == 401


@pytest.mark.asyncio
async def test_middleware_jwks_caching_avoids_repeated_fetches(rs_workspace_client):
    client, fake, private_pem, kid = rs_workspace_client
    token = _mint_rs256(private_pem, kid, tenant_id="cache-test")
    for _ in range(5):
        r = await client.get(
            "/v1/workspaces",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert r.status_code == 200
    # Within TTL → one fetch only.
    assert fake.fetch_count == 1


# ---------------------------------------------------------------------------
# Sync facade — back-compat


def test_sync_extract_auth_context_still_handles_hs256(rs_settings):
    """The sync helper kept for v0.3 callers continues to work for HS256."""

    secret = "test-secret-44chars-for-hs256-cZbR3lKp9q8WxYzAAA="
    rs_settings.identity_jwt_secret = secret
    token = _mint_hs256(secret, tenant_id="legacy")
    from plinth_workspace.auth import extract_auth_context

    ctx = extract_auth_context(f"Bearer {token}", rs_settings)
    assert ctx.tenant_id == "legacy"


def test_sync_extract_auth_context_rejects_rs256(rs_settings):
    """Sync helper refuses RS256 tokens (forces callers to the async path)."""

    private, private_pem, _, kid = _gen_keypair()
    token = _mint_rs256(private_pem, kid)
    from plinth_workspace.auth import extract_auth_context
    from plinth_workspace.exceptions import Unauthorized

    with pytest.raises(Unauthorized):
        extract_auth_context(f"Bearer {token}", rs_settings)


def test_auth_context_dataclass_fields_remain_stable():
    """The public fields of AuthContext must survive the v0.4 changes."""

    ctx = AuthContext(tenant_id="x", agent_id="y", scopes=["a"], jti="z", authenticated=True)
    assert ctx.tenant_id == "x"
    assert ctx.agent_id == "y"
    assert ctx.scopes == ["a"]
    assert ctx.jti == "z"
    assert ctx.authenticated is True
