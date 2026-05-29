# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 The Plinth Authors
"""End-to-end tests for the identity FastAPI app."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import httpx
import jwt as pyjwt
import pytest

from plinth_identity.jwt_io import JWT_ALG

UTC = timezone.utc


@pytest.mark.asyncio
async def test_healthz(client: httpx.AsyncClient):
    r = await client.get("/healthz")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ok"
    assert body["service"] == "identity"
    assert body["version"] == "0.3.0"


@pytest.mark.asyncio
async def test_jwks_returns_empty_keys_for_hs256(client: httpx.AsyncClient):
    r = await client.get("/v1/.well-known/jwks.json")
    assert r.status_code == 200
    assert r.json() == {"keys": []}


@pytest.mark.asyncio
async def test_issue_then_verify_roundtrip(client: httpx.AsyncClient):
    r = await client.post(
        "/v1/tokens",
        json={
            "agent_id": "agt_1",
            "tenant_id": "acme",
            "scopes": ["tool:web.fetch:read", "workspace:ws_x:write"],
            "workspace_id": "ws_x",
            "ttl_seconds": 600,
            "metadata": {"created_by": "tests"},
        },
    )
    assert r.status_code == 201, r.text
    body = r.json()
    assert "token" in body
    assert body["jti"].startswith("jti_")
    assert body["claims"]["agent_id"] == "agt_1"
    assert body["claims"]["tenant_id"] == "acme"
    assert body["claims"]["scopes"] == ["tool:web.fetch:read", "workspace:ws_x:write"]

    verify = await client.post(
        "/v1/tokens/verify",
        json={"token": body["token"]},
    )
    assert verify.status_code == 200
    claims = verify.json()
    assert claims["agent_id"] == "agt_1"
    assert claims["tenant_id"] == "acme"
    assert claims["jti"] == body["jti"]


@pytest.mark.asyncio
async def test_issue_with_minimal_body(client: httpx.AsyncClient):
    r = await client.post(
        "/v1/tokens",
        json={"agent_id": "agt_min", "scopes": []},
    )
    assert r.status_code == 201
    body = r.json()
    assert body["claims"]["tenant_id"] == "default"  # default value


@pytest.mark.asyncio
async def test_issue_rejects_empty_agent_id(client: httpx.AsyncClient):
    r = await client.post(
        "/v1/tokens",
        json={"agent_id": "", "scopes": []},
    )
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "INVALID_ARGUMENTS"


@pytest.mark.asyncio
async def test_issue_rejects_zero_ttl(client: httpx.AsyncClient):
    r = await client.post(
        "/v1/tokens",
        json={"agent_id": "x", "scopes": [], "ttl_seconds": 0},
    )
    assert r.status_code == 400


@pytest.mark.asyncio
async def test_issue_rejects_excessive_ttl(client: httpx.AsyncClient):
    """Tokens with ttl > max are rejected at issue time."""

    r = await client.post(
        "/v1/tokens",
        json={"agent_id": "x", "scopes": [], "ttl_seconds": 86401},
    )
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "INVALID_ARGUMENTS"
    assert "max" in r.json()["error"]["details"]


@pytest.mark.asyncio
async def test_issue_accepts_max_ttl(client: httpx.AsyncClient):
    """Tokens with ttl == max are accepted."""

    r = await client.post(
        "/v1/tokens",
        json={"agent_id": "x", "scopes": [], "ttl_seconds": 86400},
    )
    assert r.status_code == 201


@pytest.mark.asyncio
async def test_verify_tampered_token_returns_401(client: httpx.AsyncClient):
    issue = await client.post(
        "/v1/tokens",
        json={"agent_id": "x", "scopes": []},
    )
    token = issue.json()["token"]
    parts = token.split(".")
    # Flip the FIRST signature char, not the last: the last base64url char of a
    # 32-byte HMAC encodes only 2 used bits, so flipping it can decode to the
    # same bytes and leave the signature valid (flaky, time-dependent). The
    # first char always changes signature byte 0, so verification reliably fails.
    sig = parts[2]
    tampered = ".".join([*parts[:2], ("A" if sig[0] != "A" else "B") + sig[1:]])
    r = await client.post("/v1/tokens/verify", json={"token": tampered})
    assert r.status_code == 401
    assert r.json()["error"]["code"] == "INVALID_TOKEN"


@pytest.mark.asyncio
async def test_verify_garbage_token_returns_401(client: httpx.AsyncClient):
    r = await client.post("/v1/tokens/verify", json={"token": "not-a-jwt"})
    assert r.status_code == 401
    assert r.json()["error"]["code"] == "INVALID_TOKEN"


@pytest.mark.asyncio
async def test_verify_expired_token_returns_401(client: httpx.AsyncClient):
    # Hand-mint an already-expired token using the test secret + issuer.
    secret = "test-secret-44chars-for-hs256-cZbR3lKp9q8WxYzAAA="
    payload = {
        "sub": "x",
        "iss": "http://identity.test",
        "aud": "plinth",
        "iat": int((datetime.now(UTC) - timedelta(hours=2)).timestamp()),
        "exp": int((datetime.now(UTC) - timedelta(hours=1)).timestamp()),
        "jti": "jti_expired",
        "agent_id": "x",
        "tenant_id": "default",
        "scopes": [],
    }
    expired = pyjwt.encode(payload, secret, algorithm=JWT_ALG)
    r = await client.post("/v1/tokens/verify", json={"token": expired})
    assert r.status_code == 401
    assert r.json()["error"]["code"] == "TOKEN_EXPIRED"


@pytest.mark.asyncio
async def test_verify_revoked_token_returns_401(client: httpx.AsyncClient):
    issue = await client.post(
        "/v1/tokens",
        json={"agent_id": "x", "scopes": []},
    )
    token = issue.json()["token"]
    jti = issue.json()["jti"]

    revoke = await client.post(f"/v1/tokens/{jti}/revoke")
    assert revoke.status_code == 204

    r = await client.post("/v1/tokens/verify", json={"token": token})
    assert r.status_code == 401
    assert r.json()["error"]["code"] == "TOKEN_REVOKED"


@pytest.mark.asyncio
async def test_verify_empty_token_returns_400(client: httpx.AsyncClient):
    r = await client.post("/v1/tokens/verify", json={"token": ""})
    assert r.status_code == 400


@pytest.mark.asyncio
async def test_get_token_info_never_returns_jwt(client: httpx.AsyncClient):
    issue = await client.post(
        "/v1/tokens",
        json={
            "agent_id": "agt_x",
            "tenant_id": "acme",
            "scopes": ["a", "b"],
            "metadata": {"reason": "test"},
        },
    )
    body = issue.json()
    info = await client.get(f"/v1/tokens/{body['jti']}")
    assert info.status_code == 200
    info_body = info.json()
    assert "token" not in info_body
    assert info_body["jti"] == body["jti"]
    assert info_body["agent_id"] == "agt_x"
    assert info_body["tenant_id"] == "acme"
    assert info_body["scopes"] == ["a", "b"]
    assert info_body["revoked"] is False
    assert info_body["metadata"] == {"reason": "test"}


@pytest.mark.asyncio
async def test_get_token_info_404_for_unknown(client: httpx.AsyncClient):
    r = await client.get("/v1/tokens/jti_does_not_exist")
    assert r.status_code == 404
    assert r.json()["error"]["code"] == "TOKEN_NOT_FOUND"


@pytest.mark.asyncio
async def test_revoke_unknown_returns_404(client: httpx.AsyncClient):
    r = await client.post("/v1/tokens/jti_unknown/revoke")
    assert r.status_code == 404
    assert r.json()["error"]["code"] == "TOKEN_NOT_FOUND"


@pytest.mark.asyncio
async def test_get_token_info_after_revoke_shows_revoked(client: httpx.AsyncClient):
    issue = await client.post(
        "/v1/tokens",
        json={"agent_id": "agt_x", "scopes": []},
    )
    jti = issue.json()["jti"]
    await client.post(f"/v1/tokens/{jti}/revoke")
    info = await client.get(f"/v1/tokens/{jti}")
    body = info.json()
    assert body["revoked"] is True
    assert body["revoked_at"] is not None


@pytest.mark.asyncio
async def test_request_id_header_is_echoed(client: httpx.AsyncClient):
    r = await client.get("/healthz", headers={"x-request-id": "req-test-42"})
    assert r.headers.get("x-request-id") == "req-test-42"


@pytest.mark.asyncio
async def test_validation_error_uses_envelope(client: httpx.AsyncClient):
    r = await client.post("/v1/tokens", json={"scopes": []})  # no agent_id
    assert r.status_code == 400
    body = r.json()
    assert body["error"]["code"] == "INVALID_ARGUMENTS"


@pytest.mark.asyncio
async def test_token_info_includes_workspace_id(client: httpx.AsyncClient):
    issue = await client.post(
        "/v1/tokens",
        json={"agent_id": "x", "scopes": [], "workspace_id": "ws_42"},
    )
    jti = issue.json()["jti"]
    info = (await client.get(f"/v1/tokens/{jti}")).json()
    assert info["workspace_id"] == "ws_42"


@pytest.mark.asyncio
async def test_settings_resolve_secret_from_disk(tmp_path):
    """If a secret file exists on disk, settings prefer it over auto-gen."""

    from plinth_identity.settings import Settings

    s = Settings(data_dir=tmp_path, identity_jwt_secret=None)
    s.secret_path.parent.mkdir(parents=True, exist_ok=True)
    s.secret_path.write_text("fixed-secret-from-disk", encoding="utf-8")
    assert s.resolve_secret() == "fixed-secret-from-disk"


@pytest.mark.asyncio
async def test_settings_auto_generate_persists(tmp_path):
    """Auto-generation writes a file we can re-read."""

    from plinth_identity.settings import Settings

    s = Settings(
        data_dir=tmp_path,
        identity_jwt_secret=None,
        identity_auto_generate_secret=True,
    )
    secret1 = s.resolve_secret()
    secret2 = s.resolve_secret()
    assert secret1 == secret2
    assert s.secret_path.exists()


@pytest.mark.asyncio
async def test_settings_auto_generate_disabled_raises(tmp_path):
    from plinth_identity.settings import Settings

    s = Settings(
        data_dir=tmp_path,
        identity_jwt_secret=None,
        identity_auto_generate_secret=False,
    )
    with pytest.raises(RuntimeError):
        s.resolve_secret()
