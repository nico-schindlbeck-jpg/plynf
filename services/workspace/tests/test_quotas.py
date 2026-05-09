# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 The Plinth Authors
"""Tests for v1.0 per-tenant quota enforcement in the workspace service."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from pathlib import Path

import httpx
import pytest
import pytest_asyncio
from httpx import ASGITransport

from plinth_workspace.api import create_app
from plinth_workspace.db import init_db
from plinth_workspace.quotas import (
    QuotaCache,
    QuotaEnforcer,
    TenantQuotas,
    tenant_quotas_from_dict,
    tenant_storage_bytes,
    tenant_workspace_count,
    workspace_channel_count,
    workspace_workflow_count,
)
from plinth_workspace.settings import Settings


AUTH_HEADER = {"Authorization": "Bearer test-token"}


# ---------------------------------------------------------------------------
# Fixtures: a custom client that runs with quotas enabled and a stub identity.


class _StubIdentity:
    """Minimal in-memory stand-in for the identity service.

    Returns ``TenantQuotas`` JSON for ``GET /v1/tenants/{id}/quotas``.
    Tests mutate ``self.payload`` to vary the quota envelope. Counts every
    request so we can assert cache TTL behaviour.
    """

    def __init__(self, payload: dict | None = None) -> None:
        self.payload: dict = payload or {}
        self.calls: int = 0

    async def handle(self, request: httpx.Request) -> httpx.Response:
        self.calls += 1
        # Identity URL pattern: /v1/tenants/<id>/quotas
        return httpx.Response(200, json=self.payload)


def _make_quota_client(
    settings: Settings,
    *,
    identity_payload: dict | None = None,
    cache_ttl: int = 0,
) -> tuple[httpx.AsyncClient, _StubIdentity]:
    """Build a workspace client wired with quota enforcement enabled.

    The QuotaCache is replaced with one whose httpx.AsyncClient routes
    every quota fetch to ``_StubIdentity`` so tests stay hermetic.
    """

    settings.quotas_enabled = True
    settings.quotas_cache_ttl_seconds = cache_ttl

    stub = _StubIdentity(identity_payload)
    transport_to_identity = httpx.MockTransport(stub.handle)
    identity_client = httpx.AsyncClient(transport=transport_to_identity)
    cache = QuotaCache(
        identity_url="http://identity.test",
        ttl_seconds=cache_ttl,
        client=identity_client,
    )
    return cache, stub  # type: ignore[return-value]


@pytest_asyncio.fixture()
async def quota_client(settings: Settings) -> AsyncIterator[tuple[httpx.AsyncClient, _StubIdentity]]:
    """A workspace client + identity stub.

    The identity stub starts with ``max_workspaces=2``, ``max_channels=2``,
    ``max_workflows=2``, ``max_storage_gb=0.0001`` (~104KB) so each test
    can pick a tight ceiling without being verbose.
    """

    settings.data_dir.mkdir(parents=True, exist_ok=True)
    settings.blobs_dir.mkdir(parents=True, exist_ok=True)
    await init_db(settings.db_path)
    settings.quotas_enabled = True

    stub = _StubIdentity({
        "tenant_id": "default",
        "max_workspaces": 2,
        "max_channels_per_workspace": 2,
        "max_workflows_per_workspace": 2,
        "max_storage_gb": 0.0001,  # ~104.857 KB
        "max_storage_bytes": 100,
    })
    transport_to_identity = httpx.MockTransport(stub.handle)
    identity_client = httpx.AsyncClient(transport=transport_to_identity)
    cache = QuotaCache(
        identity_url="http://identity.test",
        ttl_seconds=0,
        client=identity_client,
    )

    app = create_app(settings)
    # Replace the in-app enforcer with one wired to the stub identity.
    app.state.quota_enforcer = QuotaEnforcer(
        cache,
        db_path=settings.db_path,
        enabled=True,
    )

    transport = ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport,
        base_url="http://test",
        headers=AUTH_HEADER,
    ) as c:
        try:
            yield c, stub
        finally:
            await identity_client.aclose()


@pytest_asyncio.fixture()
async def quota_client_ttl(settings: Settings) -> AsyncIterator[tuple[httpx.AsyncClient, _StubIdentity]]:
    """Like ``quota_client`` but with a 60s cache TTL so cache-hit tests can run."""

    settings.data_dir.mkdir(parents=True, exist_ok=True)
    settings.blobs_dir.mkdir(parents=True, exist_ok=True)
    await init_db(settings.db_path)
    settings.quotas_enabled = True

    stub = _StubIdentity({
        "tenant_id": "default",
        "max_workspaces": 100,
        "max_channels_per_workspace": 100,
        "max_workflows_per_workspace": 100,
        "max_storage_gb": 100.0,
    })
    transport_to_identity = httpx.MockTransport(stub.handle)
    identity_client = httpx.AsyncClient(transport=transport_to_identity)
    cache = QuotaCache(
        identity_url="http://identity.test",
        ttl_seconds=60,
        client=identity_client,
    )

    app = create_app(settings)
    app.state.quota_enforcer = QuotaEnforcer(
        cache,
        db_path=settings.db_path,
        enabled=True,
    )

    transport = ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport,
        base_url="http://test",
        headers=AUTH_HEADER,
    ) as c:
        try:
            yield c, stub
        finally:
            await identity_client.aclose()


# ---------------------------------------------------------------------------
# WorkspaceCreate enforcement


@pytest.mark.asyncio
async def test_workspace_create_under_quota_succeeds(quota_client):
    client, _ = quota_client
    r = await client.post("/v1/workspaces", json={"name": "first"})
    assert r.status_code == 201
    r = await client.post("/v1/workspaces", json={"name": "second"})
    assert r.status_code == 201


@pytest.mark.asyncio
async def test_workspace_create_blocks_at_max_workspaces(quota_client):
    client, _ = quota_client
    await client.post("/v1/workspaces", json={"name": "a"})
    await client.post("/v1/workspaces", json={"name": "b"})
    r = await client.post("/v1/workspaces", json={"name": "c"})
    assert r.status_code == 429
    body = r.json()
    assert body["error"]["code"] == "QUOTA_EXCEEDED"
    assert body["error"]["details"]["quota"] == "max_workspaces"
    assert body["error"]["details"]["limit"] == 2
    assert body["error"]["details"]["current"] == 2
    # No Retry-After: this is a long-term quota, not a rate-limit.
    assert "retry-after" not in {k.lower() for k in r.headers.keys()}


# ---------------------------------------------------------------------------
# Channel auto-create enforcement


@pytest.mark.asyncio
async def test_channel_autocreate_under_quota_succeeds(quota_client):
    client, _ = quota_client
    r = await client.post("/v1/workspaces", json={"name": "ws"})
    ws_id = r.json()["id"]
    r = await client.post(
        f"/v1/workspaces/{ws_id}/channels/c1/send",
        json={"payload": "hi"},
    )
    assert r.status_code == 201
    r = await client.post(
        f"/v1/workspaces/{ws_id}/channels/c2/send",
        json={"payload": "hi"},
    )
    assert r.status_code == 201


@pytest.mark.asyncio
async def test_channel_autocreate_blocks_at_max_channels(quota_client):
    client, _ = quota_client
    r = await client.post("/v1/workspaces", json={"name": "ws"})
    ws_id = r.json()["id"]
    await client.post(
        f"/v1/workspaces/{ws_id}/channels/c1/send",
        json={"payload": "hi"},
    )
    await client.post(
        f"/v1/workspaces/{ws_id}/channels/c2/send",
        json={"payload": "hi"},
    )
    r = await client.post(
        f"/v1/workspaces/{ws_id}/channels/c3/send",
        json={"payload": "hi"},
    )
    assert r.status_code == 429
    assert r.json()["error"]["details"]["quota"] == "max_channels_per_workspace"


@pytest.mark.asyncio
async def test_channel_subsequent_send_does_not_count(quota_client):
    """Sending a second message to an EXISTING channel never trips the cap."""

    client, _ = quota_client
    r = await client.post("/v1/workspaces", json={"name": "ws"})
    ws_id = r.json()["id"]
    await client.post(
        f"/v1/workspaces/{ws_id}/channels/c1/send",
        json={"payload": "hi"},
    )
    await client.post(
        f"/v1/workspaces/{ws_id}/channels/c2/send",
        json={"payload": "hi"},
    )
    # Send 5 more messages to existing channels.
    for _ in range(5):
        r = await client.post(
            f"/v1/workspaces/{ws_id}/channels/c1/send",
            json={"payload": "again"},
        )
        assert r.status_code == 201


# ---------------------------------------------------------------------------
# Workflow create enforcement


@pytest.mark.asyncio
async def test_workflow_create_blocks_at_max_workflows(quota_client):
    client, _ = quota_client
    r = await client.post("/v1/workspaces", json={"name": "ws"})
    ws_id = r.json()["id"]
    body = {"name": "wf", "steps": ["s1"]}
    r = await client.post(f"/v1/workspaces/{ws_id}/workflows", json=body)
    assert r.status_code == 201
    r = await client.post(f"/v1/workspaces/{ws_id}/workflows", json=body)
    assert r.status_code == 201
    r = await client.post(f"/v1/workspaces/{ws_id}/workflows", json=body)
    assert r.status_code == 429
    assert r.json()["error"]["details"]["quota"] == "max_workflows_per_workspace"


# ---------------------------------------------------------------------------
# File storage enforcement


@pytest.mark.asyncio
async def test_file_put_blocks_at_max_storage_gb(quota_client):
    client, stub = quota_client
    # quota_client sets max_storage_gb to ~100KB. Push past.
    r = await client.post("/v1/workspaces", json={"name": "ws"})
    ws_id = r.json()["id"]
    # Put a 50KB file — should succeed.
    r = await client.put(
        f"/v1/workspaces/{ws_id}/files/big.bin",
        content=b"\x00" * 50_000,
        headers={"content-type": "application/octet-stream"},
    )
    assert r.status_code in (200, 201)
    # Put another 80KB file — would push us over the 100KB-equivalent
    # max_storage_gb=0.0001 ceiling.
    r = await client.put(
        f"/v1/workspaces/{ws_id}/files/big2.bin",
        content=b"\x00" * 80_000,
        headers={"content-type": "application/octet-stream"},
    )
    assert r.status_code == 429
    assert r.json()["error"]["details"]["quota"] == "max_storage_gb"


# ---------------------------------------------------------------------------
# Identity unreachable → degraded mode


@pytest.mark.asyncio
async def test_identity_unreachable_allows_operation(settings: Settings):
    """When identity returns 5xx the workspace allows the op (logs warning)."""

    settings.data_dir.mkdir(parents=True, exist_ok=True)
    settings.blobs_dir.mkdir(parents=True, exist_ok=True)
    await init_db(settings.db_path)
    settings.quotas_enabled = True

    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, json={"error": "boom"})

    transport_to_identity = httpx.MockTransport(handler)
    identity_client = httpx.AsyncClient(transport=transport_to_identity)
    cache = QuotaCache(
        identity_url="http://identity.test",
        ttl_seconds=0,
        client=identity_client,
    )

    app = create_app(settings)
    app.state.quota_enforcer = QuotaEnforcer(
        cache,
        db_path=settings.db_path,
        enabled=True,
    )

    transport = ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport,
        base_url="http://test",
        headers=AUTH_HEADER,
    ) as c:
        # Even though identity is broken, the workspace falls back to
        # defaults — and defaults are large enough that we sail past.
        r = await c.post("/v1/workspaces", json={"name": "x"})
        assert r.status_code == 201
    await identity_client.aclose()


# ---------------------------------------------------------------------------
# Cache behaviour


@pytest.mark.asyncio
async def test_quota_cache_reuses_within_ttl(quota_client_ttl):
    client, stub = quota_client_ttl
    # The first create triggers a fetch; the second should reuse.
    r = await client.post("/v1/workspaces", json={"name": "ws1"})
    assert r.status_code == 201
    calls_after_first = stub.calls
    r = await client.post("/v1/workspaces", json={"name": "ws2"})
    assert r.status_code == 201
    # Second call should NOT hit identity (cached).
    assert stub.calls == calls_after_first


# ---------------------------------------------------------------------------
# Counters


@pytest.mark.asyncio
async def test_tenant_workspace_count(client: httpx.AsyncClient, settings: Settings):
    # Use the default (quotas-disabled) client to populate state, then
    # query the counter directly.
    await client.post("/v1/workspaces", json={"name": "a"})
    await client.post("/v1/workspaces", json={"name": "b"})
    n = await tenant_workspace_count(settings.db_path, "default")
    assert n == 2


@pytest.mark.asyncio
async def test_workspace_channel_count(
    client: httpx.AsyncClient,
    settings: Settings,
    workspace_id: str,
):
    await client.post(
        f"/v1/workspaces/{workspace_id}/channels/x/send",
        json={"payload": 1},
    )
    await client.post(
        f"/v1/workspaces/{workspace_id}/channels/y/send",
        json={"payload": 2},
    )
    n = await workspace_channel_count(settings.db_path, workspace_id)
    assert n == 2


@pytest.mark.asyncio
async def test_workspace_workflow_count(
    client: httpx.AsyncClient,
    settings: Settings,
    workspace_id: str,
):
    await client.post(
        f"/v1/workspaces/{workspace_id}/workflows",
        json={"name": "wf", "steps": ["s"]},
    )
    n = await workspace_workflow_count(settings.db_path, workspace_id)
    assert n == 1


@pytest.mark.asyncio
async def test_tenant_storage_bytes_sums_files(
    client: httpx.AsyncClient,
    settings: Settings,
    workspace_id: str,
):
    await client.put(
        f"/v1/workspaces/{workspace_id}/files/foo.txt",
        content=b"hello world",
        headers={"content-type": "text/plain"},
    )
    n = await tenant_storage_bytes(settings.db_path, "default")
    assert n == len(b"hello world")


# ---------------------------------------------------------------------------
# Quotas disabled → no-op


@pytest.mark.asyncio
async def test_quotas_disabled_does_not_block(client: httpx.AsyncClient):
    # The default fixture has quotas_enabled=False, so even hammering
    # workspace creation succeeds.
    for i in range(5):
        r = await client.post("/v1/workspaces", json={"name": f"ws{i}"})
        assert r.status_code == 201


# ---------------------------------------------------------------------------
# Model parsing


def test_tenant_quotas_from_dict_uses_defaults_for_missing_fields():
    q = tenant_quotas_from_dict("acme", {"max_workspaces": 7})
    assert q.tenant_id == "acme"
    assert q.max_workspaces == 7
    # Unspecified field falls through to default.
    assert q.max_storage_gb == 10.0
