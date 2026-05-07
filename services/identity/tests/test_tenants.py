# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 The Plinth Authors
"""Tests for the tenants CRUD + default tenant seeding."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import httpx
import pytest

from plinth_identity.store import (
    DEFAULT_TENANT_ID,
    DEFAULT_TENANT_NAME,
    TenantStore,
    init_db,
)

UTC = timezone.utc


# ---------------------------------------------------------------------------
# /v1/tenants — API


@pytest.mark.asyncio
async def test_default_tenant_seeded_on_startup(client: httpx.AsyncClient):
    r = await client.get("/v1/tenants")
    assert r.status_code == 200
    body = r.json()
    ids = {t["id"] for t in body["tenants"]}
    assert DEFAULT_TENANT_ID in ids


@pytest.mark.asyncio
async def test_default_tenant_has_expected_name(client: httpx.AsyncClient):
    r = await client.get(f"/v1/tenants/{DEFAULT_TENANT_ID}")
    assert r.status_code == 200
    body = r.json()
    assert body["id"] == DEFAULT_TENANT_ID
    assert body["name"] == DEFAULT_TENANT_NAME


@pytest.mark.asyncio
async def test_create_tenant(client: httpx.AsyncClient):
    r = await client.post(
        "/v1/tenants",
        json={"id": "acme", "name": "Acme Inc.", "metadata": {"plan": "free"}},
    )
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["id"] == "acme"
    assert body["name"] == "Acme Inc."
    assert body["metadata"] == {"plan": "free"}


@pytest.mark.asyncio
async def test_create_tenant_conflict(client: httpx.AsyncClient):
    await client.post("/v1/tenants", json={"id": "acme2", "name": "n"})
    r = await client.post("/v1/tenants", json={"id": "acme2", "name": "n"})
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "TENANT_ALREADY_EXISTS"


@pytest.mark.asyncio
async def test_get_tenant_unknown_returns_404(client: httpx.AsyncClient):
    r = await client.get("/v1/tenants/no-such-tenant")
    assert r.status_code == 404
    assert r.json()["error"]["code"] == "TENANT_NOT_FOUND"


@pytest.mark.asyncio
async def test_create_tenant_rejects_funny_id(client: httpx.AsyncClient):
    r = await client.post(
        "/v1/tenants",
        json={"id": "has spaces", "name": "n"},
    )
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "INVALID_ARGUMENTS"


@pytest.mark.asyncio
async def test_list_tenants_includes_created(client: httpx.AsyncClient):
    await client.post("/v1/tenants", json={"id": "alpha", "name": "Alpha"})
    await client.post("/v1/tenants", json={"id": "beta", "name": "Beta"})
    r = await client.get("/v1/tenants")
    ids = {t["id"] for t in r.json()["tenants"]}
    assert {"alpha", "beta", DEFAULT_TENANT_ID} <= ids


# ---------------------------------------------------------------------------
# TenantStore — direct


@pytest.mark.asyncio
async def test_tenant_store_create_and_get(settings):
    await init_db(settings.db_path)
    store = TenantStore(settings.db_path)
    t = await store.create(
        tenant_id="t-1",
        name="Tenant 1",
        metadata={"k": "v"},
    )
    assert t.id == "t-1"
    fetched = await store.get("t-1")
    assert fetched.metadata == {"k": "v"}


@pytest.mark.asyncio
async def test_tenant_store_get_unknown_raises(settings):
    from plinth_identity.exceptions import TenantNotFound

    await init_db(settings.db_path)
    store = TenantStore(settings.db_path)
    with pytest.raises(TenantNotFound):
        await store.get("missing")


@pytest.mark.asyncio
async def test_tenant_store_create_duplicate_raises(settings):
    from plinth_identity.exceptions import TenantAlreadyExists

    await init_db(settings.db_path)
    store = TenantStore(settings.db_path)
    await store.create(tenant_id="dup", name="x")
    with pytest.raises(TenantAlreadyExists):
        await store.create(tenant_id="dup", name="y")


@pytest.mark.asyncio
async def test_tenant_store_lists_default_after_init(settings):
    """init_db idempotently seeds 'default' even if called many times."""

    await init_db(settings.db_path)
    await init_db(settings.db_path)  # second call shouldn't blow up
    store = TenantStore(settings.db_path)
    tenants = await store.list()
    ids = {t.id for t in tenants}
    assert DEFAULT_TENANT_ID in ids


# ---------------------------------------------------------------------------
# /v1/tokens listing — used by workspace + gateway for revocation polling


@pytest.mark.asyncio
async def test_list_tokens_returns_all_issued(client: httpx.AsyncClient):
    issued: list[str] = []
    for i in range(3):
        r = await client.post(
            "/v1/tokens",
            json={"agent_id": f"a{i}", "scopes": []},
        )
        issued.append(r.json()["jti"])

    r = await client.get("/v1/tokens?revoked=false")
    assert r.status_code == 200
    body = r.json()
    jtis = {t["jti"] for t in body["tokens"]}
    # All issued tokens are returned (issued_at granularity is seconds, so
    # in-second ordering is non-deterministic; just check membership).
    assert set(issued) <= jtis


@pytest.mark.asyncio
async def test_list_tokens_filter_by_revoked(client: httpx.AsyncClient):
    r1 = await client.post("/v1/tokens", json={"agent_id": "a", "scopes": []})
    r2 = await client.post("/v1/tokens", json={"agent_id": "b", "scopes": []})

    j1, j2 = r1.json()["jti"], r2.json()["jti"]
    await client.post(f"/v1/tokens/{j1}/revoke")

    revoked = (await client.get("/v1/tokens?revoked=true")).json()["tokens"]
    revoked_ids = {t["jti"] for t in revoked}
    assert j1 in revoked_ids
    assert j2 not in revoked_ids


@pytest.mark.asyncio
async def test_list_tokens_filter_by_since(client: httpx.AsyncClient):
    r1 = await client.post("/v1/tokens", json={"agent_id": "a", "scopes": []})
    j1 = r1.json()["jti"]
    await client.post(f"/v1/tokens/{j1}/revoke")

    cutoff = (datetime.now(UTC) - timedelta(hours=1)).isoformat()
    # httpx encodes ``+`` in the query string as ``%2B``; pass via params=
    # to take the encoding decision out of our hands.
    r = await client.get("/v1/tokens", params={"revoked": "true", "since": cutoff})
    assert r.status_code == 200, r.text
    revoked_ids = {t["jti"] for t in r.json()["tokens"]}
    assert j1 in revoked_ids


@pytest.mark.asyncio
async def test_list_tokens_invalid_since_returns_400(client: httpx.AsyncClient):
    r = await client.get("/v1/tokens?since=not-a-date")
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "INVALID_ARGUMENTS"


@pytest.mark.asyncio
async def test_list_tokens_filter_by_tenant(client: httpx.AsyncClient):
    r1 = await client.post(
        "/v1/tokens",
        json={"agent_id": "a", "tenant_id": "alpha", "scopes": []},
    )
    r2 = await client.post(
        "/v1/tokens",
        json={"agent_id": "b", "tenant_id": "beta", "scopes": []},
    )
    only_alpha = (await client.get("/v1/tokens?tenant_id=alpha")).json()["tokens"]
    ids = {t["jti"] for t in only_alpha}
    assert r1.json()["jti"] in ids
    assert r2.json()["jti"] not in ids


@pytest.mark.asyncio
async def test_list_tokens_filter_by_agent(client: httpx.AsyncClient):
    r1 = await client.post("/v1/tokens", json={"agent_id": "alpha-agt", "scopes": []})
    r2 = await client.post("/v1/tokens", json={"agent_id": "beta-agt", "scopes": []})
    only_alpha = (await client.get("/v1/tokens?agent_id=alpha-agt")).json()["tokens"]
    ids = {t["jti"] for t in only_alpha}
    assert r1.json()["jti"] in ids
    assert r2.json()["jti"] not in ids
