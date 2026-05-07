# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 The Plinth Authors
"""Tests for the v0.3 multi-tenancy + JWT verification middleware on the gateway."""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx
import jwt as pyjwt
import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from plinth_gateway.api import create_app
from plinth_gateway.settings import Settings

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
        identity_jwt_secret=TEST_SECRET,
        identity_url=ISSUER,
        jwt_audience=AUDIENCE,
    )
    app = create_app(settings)
    transport = ASGITransport(app=app)
    async with AsyncClient(
        transport=transport,
        base_url="http://testserver",
    ) as c, app.router.lifespan_context(app):
        yield c, settings


def _tool_body(tool_id: str = "web.fetch") -> dict:
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
async def test_permissive_default_tenant(client: AsyncClient):
    r = await client.post("/v1/tools/register", json=_tool_body("a.b"))
    assert r.status_code == 201


@pytest.mark.asyncio
async def test_permissive_tenants_endpoint_returns_default(
    client: AsyncClient, make_tool
):
    await client.post("/v1/tools/register", json=make_tool(tool_id="t.1"))
    r = await client.get("/v1/tenants")
    assert r.status_code == 200
    body = r.json()
    # 'default' shows up because we registered a tool there
    ids = {t["id"] for t in body["tenants"]}
    assert "default" in ids


@pytest.mark.asyncio
async def test_verify_local_tools_filtered_by_tenant(verify_local_client):
    client, _ = verify_local_client
    a_token = _mint(tenant_id="tenant-a", jti="jti_a")
    b_token = _mint(tenant_id="tenant-b", jti="jti_b")

    r = await client.post(
        "/v1/tools/register",
        json=_tool_body("only.in.a"),
        headers={"Authorization": f"Bearer {a_token}"},
    )
    assert r.status_code == 201

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


@pytest.mark.asyncio
async def test_verify_local_get_other_tenant_tool_returns_404(verify_local_client):
    client, _ = verify_local_client
    a_token = _mint(tenant_id="tenant-a", jti="jti_a3")
    b_token = _mint(tenant_id="tenant-b", jti="jti_b3")

    await client.post(
        "/v1/tools/register",
        json=_tool_body("only.in.a"),
        headers={"Authorization": f"Bearer {a_token}"},
    )
    r = await client.get(
        "/v1/tools/only.in.a",
        headers={"Authorization": f"Bearer {b_token}"},
    )
    assert r.status_code == 404
    assert r.json()["error"]["code"] == "TOOL_NOT_FOUND"


@pytest.mark.asyncio
async def test_verify_local_tampered_token_returns_401(verify_local_client):
    client, _ = verify_local_client
    token = _mint(tenant_id="acme")
    parts = token.split(".")
    tampered = ".".join([*parts[:2], parts[2][:-1] + ("A" if parts[2][-1] != "A" else "B")])
    r = await client.get(
        "/v1/tools",
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
        "/v1/tools",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert r.status_code == 401
    assert r.json()["error"]["code"] == "TOKEN_EXPIRED"


@pytest.mark.asyncio
async def test_verify_local_missing_token_returns_401(verify_local_client):
    client, _ = verify_local_client
    r = await client.get("/v1/tools")
    assert r.status_code == 401


@pytest.mark.asyncio
async def test_verify_local_healthz_no_auth(verify_local_client):
    client, _ = verify_local_client
    r = await client.get("/healthz")
    assert r.status_code == 200


@pytest.mark.asyncio
async def test_audit_filters_by_tenant_in_strict_mode(verify_local_client):
    """Audit events recorded under tenant A are invisible to tenant B."""

    client, _ = verify_local_client
    a_token = _mint(tenant_id="t-a", agent_id="agent-a", jti="jti_aa")
    b_token = _mint(tenant_id="t-b", agent_id="agent-b", jti="jti_bb")

    # Register a single tool *per tenant* — invokes that fail still write
    # an audit event.
    await client.post(
        "/v1/tools/register",
        json=_tool_body("a.tool"),
        headers={"Authorization": f"Bearer {a_token}"},
    )
    await client.post(
        "/v1/tools/register",
        json=_tool_body("b.tool"),
        headers={"Authorization": f"Bearer {b_token}"},
    )

    # Trigger an audit row by invoking — backend will fail (no real http
    # backend), but the audit row carries the tenant_id of the caller.
    await client.post(
        "/v1/invoke",
        json={"tool_id": "a.tool", "arguments": {}, "agent_id": "agent-a"},
        headers={"Authorization": f"Bearer {a_token}"},
    )
    await client.post(
        "/v1/invoke",
        json={"tool_id": "b.tool", "arguments": {}, "agent_id": "agent-b"},
        headers={"Authorization": f"Bearer {b_token}"},
    )

    a_audit = await client.get(
        "/v1/audit",
        headers={"Authorization": f"Bearer {a_token}"},
    )
    b_audit = await client.get(
        "/v1/audit",
        headers={"Authorization": f"Bearer {b_token}"},
    )
    a_tools = {e["tool_id"] for e in a_audit.json()["events"]}
    b_tools = {e["tool_id"] for e in b_audit.json()["events"]}
    assert "a.tool" in a_tools
    assert "b.tool" not in a_tools
    assert "b.tool" in b_tools
    assert "a.tool" not in b_tools


def test_auth_context_has_scope_exact():
    from plinth_gateway.jwt_auth import AuthContext

    ctx = AuthContext(scopes=["tool:x:read"])
    assert ctx.has_scope("tool:x:read")
    assert not ctx.has_scope("tool:x:write")


def test_auth_context_has_scope_superuser():
    from plinth_gateway.jwt_auth import AuthContext

    assert AuthContext(scopes=["*"]).has_scope("tool:any:read")


def test_auth_context_has_scope_action_wildcard():
    from plinth_gateway.jwt_auth import AuthContext

    ctx = AuthContext(scopes=["tool:web.fetch"])
    assert ctx.has_scope("tool:web.fetch:read")
    assert ctx.has_scope("tool:web.fetch:write")
    assert not ctx.has_scope("tool:other:read")


def test_auth_context_has_scope_resource_wildcard():
    from plinth_gateway.jwt_auth import AuthContext

    ctx = AuthContext(scopes=["tool:*"])
    assert ctx.has_scope("tool:any:read")
    assert not ctx.has_scope("workspace:ws_x:read")


@pytest.mark.asyncio
async def test_legacy_db_adds_tenant_columns(tmp_path):
    """A v0.2 gateway DB picks up tenant_id columns on connect."""

    import aiosqlite

    db_path = tmp_path / "old.db"
    async with aiosqlite.connect(db_path) as conn:
        await conn.execute(
            "CREATE TABLE audit_events ("
            " id TEXT PRIMARY KEY, timestamp TIMESTAMP NOT NULL, "
            " tool_id TEXT NOT NULL, workspace_id TEXT, agent_id TEXT, "
            " arguments_hash TEXT NOT NULL, arguments_preview TEXT, "
            " result_hash TEXT, cached INTEGER, duration_ms INTEGER, "
            " cost_estimate_usd REAL, error TEXT)"
        )
        await conn.commit()

    from plinth_gateway.db import Database

    database = Database(db_path)
    await database.connect()
    try:
        async with aiosqlite.connect(db_path) as conn:
            cur = await conn.execute("PRAGMA table_info(audit_events)")
            cols = {r[1] for r in await cur.fetchall()}
        assert "tenant_id" in cols
    finally:
        await database.close()
