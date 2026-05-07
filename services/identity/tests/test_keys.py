# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 The Plinth Authors
"""Tests for the v0.4 RS256 + key rotation feature.

Covers the unit-level :class:`KeyStore` behaviour plus end-to-end
verification via the FastAPI app in RS256 mode (issue → JWKS → verify),
the admin endpoints, and the back-compat HS256 path.
"""

from __future__ import annotations

import asyncio
import base64
import os
from collections.abc import AsyncIterator
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx
import jwt as pyjwt
import pytest
import pytest_asyncio
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.rsa import RSAPrivateKey
from httpx import ASGITransport

from plinth_identity.api import create_app
from plinth_identity.keys import (
    KeyStore,
    decrypt_private_key,
    encrypt_private_key,
    generate_rsa_keypair,
    init_keys_schema,
    jwk_to_pem,
    kid_for,
    public_pem_to_jwk,
)
from plinth_identity.settings import Settings

UTC = timezone.utc

TEST_ENC_KEY_B64 = base64.b64encode(b"\x01" * 32).decode("ascii")


# ---------------------------------------------------------------------------
# Fixtures


@pytest.fixture()
def rs256_settings(tmp_path: Path) -> Settings:
    return Settings(
        data_dir=tmp_path / "plinth-data",
        identity_port=17425,
        identity_host="127.0.0.1",
        identity_url="http://identity.test",
        identity_jwt_audience="plinth",
        identity_jwt_alg="RS256",
        identity_keys_encryption_key=TEST_ENC_KEY_B64,
        identity_key_rotation_days=30,
        log_level="WARNING",
        log_format="console",
    )


@pytest_asyncio.fixture()
async def keystore(rs256_settings: Settings) -> KeyStore:
    rs256_settings.data_dir.mkdir(parents=True, exist_ok=True)
    await init_keys_schema(rs256_settings.db_path)
    store = KeyStore(rs256_settings.db_path, rs256_settings)
    return store


@pytest_asyncio.fixture()
async def rs256_client(rs256_settings: Settings) -> AsyncIterator[httpx.AsyncClient]:
    """A FastAPI client in RS256 mode (full lifespan, including KeyStore.init)."""

    rs256_settings.data_dir.mkdir(parents=True, exist_ok=True)
    app = create_app(rs256_settings)
    transport = ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport,
        base_url="http://test",
    ) as c, app.router.lifespan_context(app):
        yield c


# ---------------------------------------------------------------------------
# Unit tests — primitives


def test_generate_rsa_keypair_produces_valid_2048_keys():
    private_pem, public_pem = generate_rsa_keypair()
    assert b"-----BEGIN PRIVATE KEY-----" in private_pem
    assert b"-----BEGIN PUBLIC KEY-----" in public_pem
    private_key = serialization.load_pem_private_key(private_pem, password=None)
    assert isinstance(private_key, RSAPrivateKey)
    assert private_key.key_size == 2048


def test_kid_for_is_stable_for_same_pem():
    _, public_pem = generate_rsa_keypair()
    assert kid_for(public_pem) == kid_for(public_pem)


def test_kid_for_differs_between_keypairs():
    _, public_a = generate_rsa_keypair()
    _, public_b = generate_rsa_keypair()
    assert kid_for(public_a) != kid_for(public_b)


def test_kid_format_is_16_hex_chars():
    _, public_pem = generate_rsa_keypair()
    kid = kid_for(public_pem)
    assert len(kid) == 16
    assert all(c in "0123456789abcdef" for c in kid)


def test_encrypt_decrypt_roundtrip():
    enc_key = os.urandom(32)
    payload = b"-----BEGIN PRIVATE KEY-----\nfake\n-----END PRIVATE KEY-----\n"
    blob = encrypt_private_key(payload, enc_key)
    decrypted = decrypt_private_key(blob, enc_key)
    assert decrypted == payload


def test_encrypt_uses_distinct_nonces():
    enc_key = os.urandom(32)
    a = encrypt_private_key(b"same plaintext", enc_key)
    b = encrypt_private_key(b"same plaintext", enc_key)
    assert a != b  # AES-GCM with random nonce: ciphertexts always differ


def test_decrypt_with_wrong_key_raises():
    a = os.urandom(32)
    b = os.urandom(32)
    blob = encrypt_private_key(b"secret", a)
    with pytest.raises(Exception):
        decrypt_private_key(blob, b)


def test_encrypt_rejects_short_key():
    with pytest.raises(ValueError):
        encrypt_private_key(b"x", b"\x00" * 31)


def test_public_pem_to_jwk_and_back_roundtrip():
    _, public_pem = generate_rsa_keypair()
    kid = kid_for(public_pem)
    jwk = public_pem_to_jwk(public_pem, kid=kid)
    assert jwk["kty"] == "RSA"
    assert jwk["alg"] == "RS256"
    assert jwk["use"] == "sig"
    assert jwk["kid"] == kid
    # Reconstructing the PEM from the JWK should produce a public key
    # equal to the original (same modulus, same exponent).
    rebuilt = jwk_to_pem(jwk)
    a = serialization.load_pem_public_key(public_pem)
    b = serialization.load_pem_public_key(rebuilt)
    assert a.public_numbers() == b.public_numbers()


# ---------------------------------------------------------------------------
# KeyStore unit tests


@pytest.mark.asyncio
async def test_keystore_init_creates_first_key_in_rs256(keystore: KeyStore):
    await keystore.init()
    keys = await keystore.list_keys()
    assert len(keys) == 1
    assert keys[0].active is True
    assert keys[0].alg == "RS256"


@pytest.mark.asyncio
async def test_keystore_init_skips_keygen_in_hs256(rs256_settings: Settings):
    rs256_settings.identity_jwt_alg = "HS256"
    rs256_settings.data_dir.mkdir(parents=True, exist_ok=True)
    store = KeyStore(rs256_settings.db_path, rs256_settings)
    await store.init()
    assert await store.list_keys() == []


@pytest.mark.asyncio
async def test_keystore_active_key_returns_most_recent(keystore: KeyStore):
    await keystore.init()
    first_active = await keystore.active_key()
    rotated = await keystore.rotate()
    new_active = await keystore.active_key()
    assert rotated.kid == new_active.kid
    assert new_active.kid != first_active.kid


@pytest.mark.asyncio
async def test_keystore_rotate_demotes_previous_active(keystore: KeyStore):
    await keystore.init()
    first = await keystore.active_key()
    await keystore.rotate()
    keys = await keystore.list_keys()
    actives = [k for k in keys if k.active]
    assert len(actives) == 1
    assert actives[0].kid != first.kid
    # The previous active is still in the table (verifying old tokens).
    assert any(k.kid == first.kid and not k.active for k in keys)


@pytest.mark.asyncio
async def test_keystore_list_keys_orders_newest_first(keystore: KeyStore):
    await keystore.init()
    second = await keystore.rotate()
    third = await keystore.rotate()
    keys = await keystore.list_keys()
    assert [k.kid for k in keys[:2]] == [third.kid, second.kid]


@pytest.mark.asyncio
async def test_keystore_list_jwks_caps_at_max(keystore: KeyStore):
    await keystore.init()
    for _ in range(5):
        await keystore.rotate()
    jwks_keys = await keystore.list_jwks_keys()
    assert len(jwks_keys) == 3  # default jwks_max_keys


@pytest.mark.asyncio
async def test_keystore_get_by_kid_returns_match(keystore: KeyStore):
    await keystore.init()
    active = await keystore.active_key()
    fetched = await keystore.get_by_kid(active.kid)
    assert fetched is not None
    assert fetched.kid == active.kid


@pytest.mark.asyncio
async def test_keystore_get_by_kid_returns_none_for_unknown(keystore: KeyStore):
    await keystore.init()
    assert await keystore.get_by_kid("does-not-exist") is None


@pytest.mark.asyncio
async def test_keystore_expire_marks_inactive_and_invalid(keystore: KeyStore):
    await keystore.init()
    active = await keystore.active_key()
    await keystore.expire(active.kid)
    expired = await keystore.get_by_kid(active.kid)
    assert expired is not None
    assert expired.active is False
    assert expired.expires_at <= datetime.now(UTC) + timedelta(seconds=1)


@pytest.mark.asyncio
async def test_keystore_expire_unknown_kid_raises_key_error(keystore: KeyStore):
    with pytest.raises(KeyError):
        await keystore.expire("nope")


@pytest.mark.asyncio
async def test_keystore_auto_rotate_skips_when_recent(keystore: KeyStore):
    await keystore.init()
    rotated = await keystore.auto_rotate_if_due()
    assert rotated is None


@pytest.mark.asyncio
async def test_keystore_auto_rotate_fires_when_due(keystore: KeyStore):
    """Backdate the active key past the rotation window and verify rotation."""

    await keystore.init()
    # Reach into the table to backdate created_at on the active key.
    import aiosqlite

    far_past = datetime.now(UTC) - timedelta(days=60)
    async with aiosqlite.connect(keystore.db_path) as conn:
        await conn.execute(
            "UPDATE signing_keys SET created_at = ? WHERE active = 1",
            (far_past.isoformat(),),
        )
        await conn.commit()
    rotated = await keystore.auto_rotate_if_due()
    assert rotated is not None
    assert rotated.active is True


@pytest.mark.asyncio
async def test_keystore_get_private_pem_decrypts_under_settings_key(keystore: KeyStore):
    await keystore.init()
    active = await keystore.active_key()
    pem = await keystore.get_private_pem(active.kid)
    private_key = serialization.load_pem_private_key(pem, password=None)
    assert isinstance(private_key, RSAPrivateKey)


@pytest.mark.asyncio
async def test_keystore_get_private_pem_unknown_kid_raises(keystore: KeyStore):
    await keystore.init()
    with pytest.raises(KeyError):
        await keystore.get_private_pem("nope")


@pytest.mark.asyncio
async def test_keystore_to_jwk_includes_required_jwk_fields(keystore: KeyStore):
    await keystore.init()
    active = await keystore.active_key()
    jwk = keystore.to_jwk(active)
    assert {"kty", "kid", "alg", "use", "n", "e"} <= jwk.keys()
    assert jwk["alg"] == "RS256"


# ---------------------------------------------------------------------------
# Settings — encryption key resolution


def test_settings_resolve_keys_encryption_key_from_env(rs256_settings: Settings):
    raw = rs256_settings.resolve_keys_encryption_key()
    assert len(raw) == 32


def test_settings_auto_generates_keys_encryption_key(tmp_path: Path):
    s = Settings(
        data_dir=tmp_path,
        identity_jwt_alg="RS256",
        identity_keys_encryption_key="",
        identity_auto_generate_secret=True,
    )
    a = s.resolve_keys_encryption_key()
    b = s.resolve_keys_encryption_key()
    assert a == b
    assert len(a) == 32
    assert s.keys_encryption_key_path.exists()


def test_settings_rejects_disabled_auto_gen_for_keys(tmp_path: Path):
    s = Settings(
        data_dir=tmp_path,
        identity_jwt_alg="RS256",
        identity_keys_encryption_key="",
        identity_auto_generate_secret=False,
    )
    with pytest.raises(RuntimeError):
        s.resolve_keys_encryption_key()


def test_settings_rejects_bad_length_keys_encryption_key(tmp_path: Path):
    s = Settings(
        data_dir=tmp_path,
        identity_jwt_alg="RS256",
        identity_keys_encryption_key=base64.b64encode(b"\x00" * 16).decode("ascii"),
    )
    with pytest.raises(ValueError):
        s.resolve_keys_encryption_key()


# ---------------------------------------------------------------------------
# End-to-end via FastAPI


@pytest.mark.asyncio
async def test_jwks_returns_keys_in_rs256_mode(rs256_client: httpx.AsyncClient):
    r = await rs256_client.get("/v1/.well-known/jwks.json")
    assert r.status_code == 200
    body = r.json()
    assert len(body["keys"]) == 1
    jwk = body["keys"][0]
    assert jwk["kty"] == "RSA"
    assert jwk["alg"] == "RS256"
    assert jwk["use"] == "sig"
    assert "n" in jwk and "e" in jwk


@pytest.mark.asyncio
async def test_jwks_empty_in_hs256_mode(client: httpx.AsyncClient):
    """The HS256 default keeps publishing an empty keys list."""

    r = await client.get("/v1/.well-known/jwks.json")
    assert r.status_code == 200
    assert r.json() == {"keys": []}


@pytest.mark.asyncio
async def test_rs256_issue_then_verify_roundtrip(rs256_client: httpx.AsyncClient):
    issue = await rs256_client.post(
        "/v1/tokens",
        json={
            "agent_id": "agt_rs",
            "tenant_id": "acme",
            "scopes": ["tool:web.fetch:read"],
            "ttl_seconds": 300,
        },
    )
    assert issue.status_code == 201, issue.text
    token = issue.json()["token"]

    # The token's header should declare RS256 + carry a kid.
    header = pyjwt.get_unverified_header(token)
    assert header["alg"] == "RS256"
    assert header["kid"]

    verify = await rs256_client.post("/v1/tokens/verify", json={"token": token})
    assert verify.status_code == 200, verify.text
    claims = verify.json()
    assert claims["agent_id"] == "agt_rs"
    assert claims["tenant_id"] == "acme"


@pytest.mark.asyncio
async def test_rs256_token_verifies_against_published_jwks(rs256_client: httpx.AsyncClient):
    """A third-party verifier reading JWKS can validate a freshly issued token."""

    issue = await rs256_client.post(
        "/v1/tokens",
        json={"agent_id": "agt_rs", "scopes": [], "ttl_seconds": 300},
    )
    token = issue.json()["token"]
    header = pyjwt.get_unverified_header(token)
    kid = header["kid"]

    jwks = (await rs256_client.get("/v1/.well-known/jwks.json")).json()
    matching = next(k for k in jwks["keys"] if k["kid"] == kid)
    pem = jwk_to_pem(matching)

    decoded = pyjwt.decode(
        token,
        pem,
        algorithms=["RS256"],
        audience="plinth",
        issuer="http://identity.test",
    )
    assert decoded["agent_id"] == "agt_rs"


@pytest.mark.asyncio
async def test_rs256_verify_after_rotation_still_works(rs256_client: httpx.AsyncClient):
    """Tokens issued before a rotation must still verify until they expire."""

    issue = await rs256_client.post(
        "/v1/tokens",
        json={"agent_id": "before-rotate", "scopes": [], "ttl_seconds": 600},
    )
    token = issue.json()["token"]

    rotate = await rs256_client.post("/v1/keys/rotate")
    assert rotate.status_code == 201

    verify = await rs256_client.post("/v1/tokens/verify", json={"token": token})
    assert verify.status_code == 200
    assert verify.json()["agent_id"] == "before-rotate"


@pytest.mark.asyncio
async def test_rs256_verify_fails_when_signing_key_expired(
    rs256_client: httpx.AsyncClient,
    rs256_settings: Settings,
):
    """Forcibly expiring the signing key invalidates outstanding tokens."""

    issue = await rs256_client.post(
        "/v1/tokens",
        json={"agent_id": "doomed", "scopes": [], "ttl_seconds": 600},
    )
    token = issue.json()["token"]
    kid = pyjwt.get_unverified_header(token)["kid"]

    expire = await rs256_client.delete(f"/v1/keys/{kid}")
    assert expire.status_code == 204

    verify = await rs256_client.post("/v1/tokens/verify", json={"token": token})
    assert verify.status_code == 401


@pytest.mark.asyncio
async def test_keys_list_endpoint_returns_signing_keys(rs256_client: httpx.AsyncClient):
    r = await rs256_client.get("/v1/keys")
    assert r.status_code == 200
    keys = r.json()["keys"]
    assert len(keys) == 1
    assert "private" not in keys[0]  # never expose private material
    assert "public_key_pem" in keys[0]
    assert keys[0]["active"] is True


@pytest.mark.asyncio
async def test_keys_list_in_hs256_mode_returns_empty(client: httpx.AsyncClient):
    r = await client.get("/v1/keys")
    assert r.status_code == 200
    assert r.json() == {"keys": []}


@pytest.mark.asyncio
async def test_keys_rotate_endpoint_creates_new_active(rs256_client: httpx.AsyncClient):
    list_before = (await rs256_client.get("/v1/keys")).json()["keys"]
    assert len(list_before) == 1

    rotate = await rs256_client.post("/v1/keys/rotate")
    assert rotate.status_code == 201
    new_key = rotate.json()
    assert new_key["active"] is True

    list_after = (await rs256_client.get("/v1/keys")).json()["keys"]
    actives = [k for k in list_after if k["active"]]
    assert len(actives) == 1
    assert actives[0]["kid"] == new_key["kid"]


@pytest.mark.asyncio
async def test_keys_rotate_endpoint_returns_400_in_hs256(client: httpx.AsyncClient):
    """The HS256 deployment rejects rotation calls with a clear error."""

    r = await client.post("/v1/keys/rotate")
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "INVALID_ARGUMENTS"


@pytest.mark.asyncio
async def test_keys_delete_endpoint_force_expires(rs256_client: httpx.AsyncClient):
    keys = (await rs256_client.get("/v1/keys")).json()["keys"]
    kid = keys[0]["kid"]

    delete = await rs256_client.delete(f"/v1/keys/{kid}")
    assert delete.status_code == 204

    keys_after = (await rs256_client.get("/v1/keys?include_expired=true")).json()["keys"]
    matching = next(k for k in keys_after if k["kid"] == kid)
    assert matching["active"] is False


@pytest.mark.asyncio
async def test_keys_delete_unknown_returns_404(rs256_client: httpx.AsyncClient):
    r = await rs256_client.delete("/v1/keys/nope")
    assert r.status_code == 404
    assert r.json()["error"]["code"] == "SIGNING_KEY_NOT_FOUND"


@pytest.mark.asyncio
async def test_keys_delete_in_hs256_returns_400(client: httpx.AsyncClient):
    r = await client.delete("/v1/keys/anything")
    assert r.status_code == 400


@pytest.mark.asyncio
async def test_rs256_token_rejected_by_hs256_server(client: httpx.AsyncClient):
    """An HS256 server must not accept an RS256 token (alg confusion)."""

    private_pem, _ = generate_rsa_keypair()
    forged = pyjwt.encode(
        {
            "sub": "x",
            "iss": "http://identity.test",
            "aud": "plinth",
            "iat": int(datetime.now(UTC).timestamp()),
            "exp": int((datetime.now(UTC) + timedelta(hours=1)).timestamp()),
            "jti": "jti_x",
        },
        private_pem,
        algorithm="RS256",
        headers={"kid": "dontknow"},
    )
    r = await client.post("/v1/tokens/verify", json={"token": forged})
    assert r.status_code == 401


@pytest.mark.asyncio
async def test_hs256_token_rejected_by_rs256_server(rs256_client: httpx.AsyncClient):
    """An RS256 server must not accept an HS256 token (alg confusion)."""

    forged = pyjwt.encode(
        {
            "sub": "x",
            "iss": "http://identity.test",
            "aud": "plinth",
            "iat": int(datetime.now(UTC).timestamp()),
            "exp": int((datetime.now(UTC) + timedelta(hours=1)).timestamp()),
            "jti": "jti_x",
        },
        "shared-secret-of-some-kind",
        algorithm="HS256",
    )
    r = await rs256_client.post("/v1/tokens/verify", json={"token": forged})
    assert r.status_code == 401


@pytest.mark.asyncio
async def test_rs256_unknown_kid_returns_401(rs256_client: httpx.AsyncClient):
    """A token with a kid the server doesn't recognise must be rejected."""

    private_pem, public_pem = generate_rsa_keypair()
    forged = pyjwt.encode(
        {
            "sub": "x",
            "iss": "http://identity.test",
            "aud": "plinth",
            "iat": int(datetime.now(UTC).timestamp()),
            "exp": int((datetime.now(UTC) + timedelta(hours=1)).timestamp()),
            "jti": "jti_x",
        },
        private_pem,
        algorithm="RS256",
        headers={"kid": kid_for(public_pem)},
    )
    r = await rs256_client.post("/v1/tokens/verify", json={"token": forged})
    assert r.status_code == 401


@pytest.mark.asyncio
async def test_rotation_loop_can_be_cancelled():
    """The background rotation task tolerates clean cancellation on shutdown."""

    from plinth_identity.api import _rotation_loop
    from plinth_identity.logging_config import get_logger

    settings = Settings(
        data_dir=Path("/tmp/keystore-rotation-test"),
        identity_jwt_alg="RS256",
        identity_keys_encryption_key=TEST_ENC_KEY_B64,
        log_level="WARNING",
    )
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    await init_keys_schema(settings.db_path)
    store = KeyStore(settings.db_path, settings)
    await store.init()
    task = asyncio.create_task(_rotation_loop(store, 3600, get_logger()))
    await asyncio.sleep(0.05)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
