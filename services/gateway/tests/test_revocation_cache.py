# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 The Plinth Authors
"""Tests for the v0.6 gateway revocation cache.

Mirrors the workspace tests since the implementations are intentionally
parallel — the gateway and workspace each carry their own copy of the
cache so neither depends on the other.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import datetime, timezone
from pathlib import Path

import httpx
import jwt as pyjwt
import pytest
import pytest_asyncio
import respx
from httpx import ASGITransport, Response

from plinth_gateway.api import create_app
from plinth_gateway.revocation_cache import RevocationCache
from plinth_gateway.settings import Settings

UTC = timezone.utc

TEST_SECRET = "test-secret-44chars-for-hs256-cZbR3lKp9q8WxYzAAA="
IDENTITY_URL = "http://identity.test"


def _mint_token(
    *,
    jti: str = "jti_test",
    agent_id: str = "agt_1",
    tenant_id: str = "default",
    secret: str = TEST_SECRET,
    audience: str = "plinth",
    issuer: str = "http://identity.test",
    ttl_seconds: int = 600,
    scopes: list[str] | None = None,
) -> str:
    now = int(datetime.now(UTC).timestamp())
    payload = {
        "iss": issuer,
        "aud": audience,
        "sub": agent_id,
        "iat": now,
        "exp": now + ttl_seconds,
        "jti": jti,
        "agent_id": agent_id,
        "tenant_id": tenant_id,
        "scopes": scopes or [],
    }
    return pyjwt.encode(payload, secret, algorithm="HS256")


# ---------------------------------------------------------------------------
# Pure cache behavior


def test_is_revoked_returns_false_for_unknown_jti():
    cache = RevocationCache(identity_url=IDENTITY_URL, poll_interval=60)
    assert cache.is_revoked("jti_unknown") is False
    assert cache.is_revoked(None) is False
    assert cache.is_revoked("") is False


def test_force_revoke_helper_marks_jti_revoked():
    cache = RevocationCache(identity_url=IDENTITY_URL, poll_interval=60)
    cache._force_revoke("jti_a")
    assert cache.is_revoked("jti_a") is True
    assert cache.is_revoked("jti_b") is False


def test_stats_reflect_initial_state():
    cache = RevocationCache(identity_url=IDENTITY_URL, poll_interval=60)
    stats = cache.stats
    assert stats["size"] == 0
    assert stats["cursor"] == 0
    assert stats["last_poll_at"] is None
    assert stats["last_poll_error"] is None
    assert stats["running"] is False
    assert stats["identity_url"] == IDENTITY_URL


# ---------------------------------------------------------------------------
# Polling


@pytest.mark.asyncio
async def test_poll_once_fills_cache_from_identity():
    cache = RevocationCache(identity_url=IDENTITY_URL, poll_interval=60)
    with respx.mock(assert_all_called=True) as mock:
        mock.get(f"{IDENTITY_URL}/v1/revocations").mock(
            return_value=Response(
                200,
                json={
                    "revocations": [
                        {
                            "jti": "jti_a",
                            "revoked_at": datetime.now(UTC).isoformat(),
                            "agent_id": "agt_1",
                            "tenant_id": "t",
                        },
                        {
                            "jti": "jti_b",
                            "revoked_at": datetime.now(UTC).isoformat(),
                            "agent_id": "agt_2",
                            "tenant_id": "t",
                        },
                    ],
                    "next_since": 12345,
                    "has_more": False,
                },
            )
        )
        await cache._poll_once()

    assert cache.is_revoked("jti_a")
    assert cache.is_revoked("jti_b")
    assert cache.is_revoked("jti_c") is False
    stats = cache.stats
    assert stats["size"] == 2
    assert stats["cursor"] == 12345
    assert stats["last_poll_at"] is not None
    assert stats["last_poll_error"] is None
    await cache.stop()


@pytest.mark.asyncio
async def test_poll_once_advances_cursor_across_polls():
    cache = RevocationCache(identity_url=IDENTITY_URL, poll_interval=60)
    with respx.mock(assert_all_called=False) as mock:
        route = mock.get(f"{IDENTITY_URL}/v1/revocations")
        route.side_effect = [
            Response(
                200,
                json={
                    "revocations": [
                        {
                            "jti": "jti_first",
                            "revoked_at": datetime.now(UTC).isoformat(),
                            "agent_id": "a",
                            "tenant_id": "t",
                        },
                    ],
                    "next_since": 100,
                    "has_more": False,
                },
            ),
            Response(
                200,
                json={
                    "revocations": [
                        {
                            "jti": "jti_second",
                            "revoked_at": datetime.now(UTC).isoformat(),
                            "agent_id": "a",
                            "tenant_id": "t",
                        },
                    ],
                    "next_since": 200,
                    "has_more": False,
                },
            ),
        ]
        await cache._poll_once()
        assert cache.stats["cursor"] == 100
        await cache._poll_once()
        assert cache.stats["cursor"] == 200
        assert cache.is_revoked("jti_first")
        assert cache.is_revoked("jti_second")
    await cache.stop()


@pytest.mark.asyncio
async def test_poll_once_failure_preserves_cache_and_records_error():
    cache = RevocationCache(identity_url=IDENTITY_URL, poll_interval=60)
    cache._force_revoke("jti_existing")
    with respx.mock(assert_all_called=True) as mock:
        mock.get(f"{IDENTITY_URL}/v1/revocations").mock(
            side_effect=httpx.ConnectError("boom"),
        )
        await cache._poll_once()
    assert cache.is_revoked("jti_existing")
    assert cache.stats["size"] == 1
    assert cache.stats["cursor"] == 0
    assert cache.stats["last_poll_error"] is not None
    assert "boom" in cache.stats["last_poll_error"]
    await cache.stop()


@pytest.mark.asyncio
async def test_poll_once_with_empty_url_records_error():
    cache = RevocationCache(identity_url="", poll_interval=60)
    await cache._poll_once()
    assert cache.stats["last_poll_error"] is not None
    assert "identity_url" in cache.stats["last_poll_error"]
    assert cache.stats["size"] == 0


@pytest.mark.asyncio
async def test_start_is_idempotent():
    cache = RevocationCache(identity_url=IDENTITY_URL, poll_interval=60)
    with respx.mock(assert_all_called=False) as mock:
        mock.get(f"{IDENTITY_URL}/v1/revocations").mock(
            return_value=Response(
                200,
                json={"revocations": [], "next_since": 0, "has_more": False},
            )
        )
        await cache.start()
        first_task = cache._task
        await cache.start()
        assert cache._task is first_task
    await cache.stop()


@pytest.mark.asyncio
async def test_stop_releases_task_cleanly():
    cache = RevocationCache(identity_url=IDENTITY_URL, poll_interval=60)
    with respx.mock(assert_all_called=False) as mock:
        mock.get(f"{IDENTITY_URL}/v1/revocations").mock(
            return_value=Response(
                200,
                json={"revocations": [], "next_since": 0, "has_more": False},
            )
        )
        await cache.start()
        await cache.stop()
    assert cache._task is None
    assert cache.stats["running"] is False


# ---------------------------------------------------------------------------
# Auth integration via the FastAPI app


@pytest.fixture()
def secured_settings(tmp_path: Path) -> Settings:
    return Settings(
        data_dir=tmp_path / "gw-data",
        gateway_host="127.0.0.1",
        gateway_port=7422,
        log_level="WARNING",
        log_format="console",
        backend_timeout_seconds=5.0,
        inbound_auth_required=True,
        auth_mode="verify_local",
        identity_jwt_secret=TEST_SECRET,
        identity_url=IDENTITY_URL,
        jwt_audience="plinth",
        revocation_poll_url="",
    )


@pytest_asyncio.fixture()
async def secured_app_and_client(
    secured_settings: Settings,
) -> AsyncIterator[tuple]:
    secured_settings.ensure_data_dir()
    app = create_app(secured_settings)
    transport = ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://test"
    ) as client:
        async with app.router.lifespan_context(app):
            yield app, client


@pytest.mark.asyncio
async def test_revoked_jti_returns_401_token_revoked(secured_app_and_client):
    app, client = secured_app_and_client
    token = _mint_token(jti="jti_revoked_one", scopes=["*"])
    app.state.revocation_cache._force_revoke("jti_revoked_one")

    r = await client.get(
        "/v1/tools", headers={"Authorization": f"Bearer {token}"}
    )
    assert r.status_code == 401
    body = r.json()
    assert body["error"]["code"] == "TOKEN_REVOKED"
    assert body["error"]["details"]["jti"] == "jti_revoked_one"


@pytest.mark.asyncio
async def test_unrevoked_jti_succeeds(secured_app_and_client):
    app, client = secured_app_and_client
    token = _mint_token(jti="jti_live_one", scopes=["*"])
    assert not app.state.revocation_cache.is_revoked("jti_live_one")

    r = await client.get(
        "/v1/tools", headers={"Authorization": f"Bearer {token}"}
    )
    assert r.status_code == 200


@pytest.mark.asyncio
async def test_disabled_polling_keeps_cache_empty(secured_app_and_client):
    app, _ = secured_app_and_client
    cache = app.state.revocation_cache
    assert cache.stats["size"] == 0
    assert cache.stats["last_poll_at"] is None
    assert cache.stats["last_poll_error"] is None
    assert cache.stats["running"] is False


@pytest.mark.asyncio
async def test_admin_stats_endpoint(secured_app_and_client):
    app, client = secured_app_and_client
    app.state.revocation_cache._force_revoke("jti_x")

    # No token → 401 because inbound_auth_required + verify_local.
    r = await client.get("/v1/admin/revocations/cache/stats")
    assert r.status_code == 401

    # Admin scope unlocks the endpoint.
    admin_token = _mint_token(jti="jti_admin_g", scopes=["*"])
    r = await client.get(
        "/v1/admin/revocations/cache/stats",
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["size"] == 1
    assert "cursor" in body
    assert "last_poll_at" in body
    assert "last_poll_error" in body
