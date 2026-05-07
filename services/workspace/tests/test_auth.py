# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 The Plinth Authors
"""Tests for the v0.3 multi-tenancy + JWT verification middleware."""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx
import jwt as pyjwt
import pytest
import pytest_asyncio
from httpx import ASGITransport

from plinth_workspace.api import create_app
from plinth_workspace.db import init_db
from plinth_workspace.settings import Settings

UTC = timezone.utc

TEST_SECRET = "test-secret-44chars-for-hs256-cZbR3lKp9q8WxYzAAA="
ISSUER = "http://identity.test"
AUDIENCE = "plinth"


def _mint(
    *,
    agent_id: str = "agt_1",
    tenant_id: str = "default",
    workspace_id: str | None = None,
    scopes: list[str] | None = None,
    iat: datetime | None = None,
    exp: datetime | None = None,
    audience: str = AUDIENCE,
    issuer: str = ISSUER,
    secret: str = TEST_SECRET,
    jti: str = "jti_test",
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
        "workspace_id": workspace_id,
        "scopes": scopes or [],
    }
    token = pyjwt.encode(payload, secret, algorithm="HS256")
    if isinstance(token, bytes):
        token = token.decode("ascii")
    return token


@pytest_asyncio.fixture()
async def verify_local_client(
    tmp_path: Path,
) -> AsyncIterator[tuple[httpx.AsyncClient, Settings]]:
    """A workspace client running in ``verify_local`` mode."""

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
        identity_jwt_secret=TEST_SECRET,
        identity_url=ISSUER,
        jwt_audience=AUDIENCE,
    )
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    settings.blobs_dir.mkdir(parents=True, exist_ok=True)
    await init_db(settings.db_path)

    app = create_app(settings)
    transport = ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport,
        base_url="http://test",
    ) as c:
        yield c, settings


@pytest.mark.asyncio
async def test_permissive_mode_default_tenant(client: httpx.AsyncClient):
    """In permissive mode every workspace lands in tenant 'default'."""

    r = await client.post("/v1/workspaces", json={"name": "ws-1"})
    assert r.status_code == 201
    body = r.json()
    assert body["tenant_id"] == "default"


@pytest.mark.asyncio
async def test_permissive_mode_lists_default_tenant(client: httpx.AsyncClient):
    await client.post("/v1/workspaces", json={"name": "ws-1"})
    r = await client.get("/v1/workspaces")
    assert r.status_code == 200
    workspaces = r.json()["workspaces"]
    assert all(w["tenant_id"] == "default" for w in workspaces)


@pytest.mark.asyncio
async def test_tenants_endpoint_lists_default(client: httpx.AsyncClient):
    await client.post("/v1/workspaces", json={"name": "ws-1"})
    r = await client.get("/v1/tenants")
    assert r.status_code == 200
    body = r.json()
    assert any(t["id"] == "default" for t in body["tenants"])


@pytest.mark.asyncio
async def test_verify_local_workspace_carries_tenant_id(verify_local_client):
    client, _ = verify_local_client
    token = _mint(tenant_id="acme", agent_id="agt_42")
    r = await client.post(
        "/v1/workspaces",
        json={"name": "ws-1"},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert r.status_code == 201
    assert r.json()["tenant_id"] == "acme"


@pytest.mark.asyncio
async def test_verify_local_lists_only_own_tenant(verify_local_client):
    client, _ = verify_local_client
    a_token = _mint(tenant_id="tenant-a", jti="jti_a")
    b_token = _mint(tenant_id="tenant-b", jti="jti_b")

    await client.post(
        "/v1/workspaces",
        json={"name": "owned-by-a"},
        headers={"Authorization": f"Bearer {a_token}"},
    )
    await client.post(
        "/v1/workspaces",
        json={"name": "owned-by-b"},
        headers={"Authorization": f"Bearer {b_token}"},
    )

    a_list = await client.get(
        "/v1/workspaces",
        headers={"Authorization": f"Bearer {a_token}"},
    )
    b_list = await client.get(
        "/v1/workspaces",
        headers={"Authorization": f"Bearer {b_token}"},
    )
    a_names = [w["name"] for w in a_list.json()["workspaces"]]
    b_names = [w["name"] for w in b_list.json()["workspaces"]]
    assert a_names == ["owned-by-a"]
    assert b_names == ["owned-by-b"]


@pytest.mark.asyncio
async def test_verify_local_get_other_tenant_returns_404(verify_local_client):
    client, _ = verify_local_client
    a_token = _mint(tenant_id="tenant-a", jti="jti_a2")
    b_token = _mint(tenant_id="tenant-b", jti="jti_b2")

    create = await client.post(
        "/v1/workspaces",
        json={"name": "owned-by-a"},
        headers={"Authorization": f"Bearer {a_token}"},
    )
    ws_id = create.json()["id"]

    r = await client.get(
        f"/v1/workspaces/{ws_id}",
        headers={"Authorization": f"Bearer {b_token}"},
    )
    assert r.status_code == 404
    assert r.json()["error"]["code"] == "WORKSPACE_NOT_FOUND"


@pytest.mark.asyncio
async def test_verify_local_missing_token_returns_401(verify_local_client):
    client, _ = verify_local_client
    r = await client.get("/v1/workspaces")
    assert r.status_code == 401


@pytest.mark.asyncio
async def test_verify_local_tampered_token_returns_401(verify_local_client):
    client, _ = verify_local_client
    token = _mint(tenant_id="acme")
    parts = token.split(".")
    tampered = ".".join([*parts[:2], parts[2][:-1] + ("A" if parts[2][-1] != "A" else "B")])
    r = await client.get(
        "/v1/workspaces",
        headers={"Authorization": f"Bearer {tampered}"},
    )
    assert r.status_code == 401
    assert r.json()["error"]["code"] == "INVALID_TOKEN"


@pytest.mark.asyncio
async def test_verify_local_expired_token_returns_401(verify_local_client):
    client, _ = verify_local_client
    token = _mint(
        tenant_id="acme",
        iat=datetime.now(UTC) - timedelta(hours=2),
        exp=datetime.now(UTC) - timedelta(hours=1),
    )
    r = await client.get(
        "/v1/workspaces",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert r.status_code == 401
    assert r.json()["error"]["code"] == "TOKEN_EXPIRED"


@pytest.mark.asyncio
async def test_verify_local_wrong_audience_returns_401(verify_local_client):
    client, _ = verify_local_client
    token = _mint(tenant_id="acme", audience="someone-else")
    r = await client.get(
        "/v1/workspaces",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert r.status_code == 401


@pytest.mark.asyncio
async def test_verify_local_healthz_does_not_require_token(verify_local_client):
    client, _ = verify_local_client
    r = await client.get("/healthz")
    assert r.status_code == 200


@pytest.mark.asyncio
async def test_permissive_mode_decodes_jwt_when_secret_present(
    tmp_path: Path,
):
    """Permissive mode still respects ``tenant_id`` claim if a token is sent.

    This is the gentle on-ramp: a v0.2 demo can issue tokens and start using
    multi-tenancy without flipping the strict-verify env var.
    """

    data_dir = tmp_path / "plinth-data"
    data_dir.mkdir(parents=True, exist_ok=True)
    settings = Settings(
        data_dir=data_dir,
        workspace_port=17421,
        workspace_host="127.0.0.1",
        log_level="WARNING",
        log_format="console",
        auth_required=False,
        auth_mode="permissive",
        identity_jwt_secret=TEST_SECRET,
        identity_url=ISSUER,
        jwt_audience=AUDIENCE,
    )
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    settings.blobs_dir.mkdir(parents=True, exist_ok=True)
    await init_db(settings.db_path)

    token = _mint(tenant_id="opt-in")
    app = create_app(settings)
    transport = ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        r = await c.post(
            "/v1/workspaces",
            json={"name": "ws"},
            headers={"Authorization": f"Bearer {token}"},
        )
        assert r.status_code == 201
        assert r.json()["tenant_id"] == "opt-in"


@pytest.mark.asyncio
async def test_storage_list_tenants_includes_count(store):
    await store.create_workspace("ws1", tenant_id="acme")
    await store.create_workspace("ws2", tenant_id="acme")
    await store.create_workspace("ws3", tenant_id="other")
    rows = await store.list_tenants()
    counts = {row["id"]: row["workspace_count"] for row in rows}
    assert counts.get("acme") == 2
    assert counts.get("other") == 1


def test_auth_context_has_scope_exact_match():
    from plinth_workspace.auth import AuthContext

    ctx = AuthContext(scopes=["tool:web.fetch:read"])
    assert ctx.has_scope("tool:web.fetch:read") is True
    assert ctx.has_scope("tool:web.fetch:write") is False


def test_auth_context_has_scope_superuser_wildcard():
    from plinth_workspace.auth import AuthContext

    ctx = AuthContext(scopes=["*"])
    assert ctx.has_scope("tool:any.tool:read") is True
    assert ctx.has_scope("workspace:ws_x:admin") is True


def test_auth_context_has_scope_action_wildcard():
    """Holding ``tool:web.fetch`` matches any action on web.fetch."""

    from plinth_workspace.auth import AuthContext

    ctx = AuthContext(scopes=["tool:web.fetch"])
    assert ctx.has_scope("tool:web.fetch:read") is True
    assert ctx.has_scope("tool:web.fetch:write") is True
    assert ctx.has_scope("tool:other:read") is False


def test_auth_context_has_scope_resource_wildcard():
    """Holding ``tool:*`` implies any tool."""

    from plinth_workspace.auth import AuthContext

    ctx = AuthContext(scopes=["tool:*"])
    assert ctx.has_scope("tool:any:read") is True
    assert ctx.has_scope("workspace:ws_x:read") is False


def test_auth_context_has_scope_empty_scopes():
    from plinth_workspace.auth import AuthContext

    ctx = AuthContext(scopes=[])
    assert ctx.has_scope("anything") is False


@pytest.mark.asyncio
async def test_existing_db_with_no_tenant_column_migrates(tmp_path):
    """An older DB with no ``tenant_id`` column gains one on init.

    We re-use the workspace store so the schema migration runs over an
    already-populated DB rather than a virgin one.
    """

    import aiosqlite

    db_path = tmp_path / "old.db"
    # Simulate v0.2 schema: workspaces table without tenant_id.
    async with aiosqlite.connect(db_path) as conn:
        await conn.execute(
            "CREATE TABLE workspaces ("
            " id TEXT PRIMARY KEY, name TEXT NOT NULL, "
            " metadata TEXT NOT NULL DEFAULT '{}', "
            " created_at TIMESTAMP NOT NULL, updated_at TIMESTAMP NOT NULL)"
        )
        await conn.execute(
            "INSERT INTO workspaces VALUES (?, ?, ?, ?, ?)",
            ("ws_legacy", "legacy", "{}", "2026-01-01T00:00:00+00:00",
             "2026-01-01T00:00:00+00:00"),
        )
        await conn.commit()

    await init_db(db_path)

    async with aiosqlite.connect(db_path) as conn:
        cur = await conn.execute(
            "SELECT tenant_id FROM workspaces WHERE id='ws_legacy'"
        )
        row = await cur.fetchone()
    assert row is not None
    assert row[0] == "default"
