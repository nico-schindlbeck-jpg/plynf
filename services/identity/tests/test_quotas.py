# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 The Plinth Authors
"""Tests for the per-tenant resource quotas (v1.0)."""

from __future__ import annotations

import httpx
import pytest

from plinth_identity.quotas import (
    DEFAULT_MAX_ACTIVE_TOKENS,
    DEFAULT_MAX_CHANNELS_PER_WORKSPACE,
    DEFAULT_MAX_COST_USD_DAY,
    DEFAULT_MAX_COST_USD_MONTH,
    DEFAULT_MAX_INVOCATIONS_PER_MINUTE,
    DEFAULT_MAX_OAUTH_CONNECTIONS,
    DEFAULT_MAX_STORAGE_GB,
    DEFAULT_MAX_WORKFLOWS_PER_WORKSPACE,
    DEFAULT_MAX_WORKSPACES,
    QuotaStore,
    TenantQuotasUpdate,
)
from plinth_identity.store import init_db


# ---------------------------------------------------------------------------
# /v1/tenants/{tenant_id}/quotas — API


@pytest.mark.asyncio
async def test_get_quotas_unknown_tenant_returns_defaults(client: httpx.AsyncClient):
    # Per spec: no 404. Unknown tenants get the contract defaults so the
    # workspace/gateway quota fetch path stays branchless.
    r = await client.get("/v1/tenants/no-such/quotas")
    assert r.status_code == 200
    body = r.json()
    assert body["tenant_id"] == "no-such"
    assert body["max_workspaces"] == DEFAULT_MAX_WORKSPACES
    assert body["max_storage_gb"] == DEFAULT_MAX_STORAGE_GB
    assert body["max_channels_per_workspace"] == DEFAULT_MAX_CHANNELS_PER_WORKSPACE
    assert body["max_workflows_per_workspace"] == DEFAULT_MAX_WORKFLOWS_PER_WORKSPACE
    assert body["max_active_tokens"] == DEFAULT_MAX_ACTIVE_TOKENS
    assert body["max_oauth_connections"] == DEFAULT_MAX_OAUTH_CONNECTIONS
    assert body["max_cost_usd_day"] == DEFAULT_MAX_COST_USD_DAY
    assert body["max_cost_usd_month"] == DEFAULT_MAX_COST_USD_MONTH
    assert body["max_invocations_per_minute"] == DEFAULT_MAX_INVOCATIONS_PER_MINUTE


@pytest.mark.asyncio
async def test_set_quotas_persists_full_envelope(client: httpx.AsyncClient):
    body = {
        "max_workspaces": 5,
        "max_storage_gb": 1.5,
        "max_channels_per_workspace": 10,
        "max_workflows_per_workspace": 20,
        "max_active_tokens": 30,
        "max_oauth_connections": 4,
        "max_cost_usd_day": 5.0,
        "max_cost_usd_month": 50.0,
        "max_invocations_per_minute": 60,
    }
    r = await client.post("/v1/tenants/acme/quotas", json=body)
    assert r.status_code == 200, r.text
    out = r.json()
    for key, val in body.items():
        assert out[key] == val
    assert out["tenant_id"] == "acme"
    assert "updated_at" in out


@pytest.mark.asyncio
async def test_set_quotas_partial_keeps_existing(client: httpx.AsyncClient):
    await client.post(
        "/v1/tenants/acme2/quotas",
        json={"max_workspaces": 7, "max_storage_gb": 2.0},
    )
    # Patch only max_workspaces; storage_gb should remain at 2.0.
    r = await client.post(
        "/v1/tenants/acme2/quotas",
        json={"max_workspaces": 12},
    )
    assert r.status_code == 200
    out = r.json()
    assert out["max_workspaces"] == 12
    assert out["max_storage_gb"] == 2.0


@pytest.mark.asyncio
async def test_get_quotas_returns_persisted_values(client: httpx.AsyncClient):
    await client.post("/v1/tenants/acme3/quotas", json={"max_workspaces": 3})
    r = await client.get("/v1/tenants/acme3/quotas")
    assert r.status_code == 200
    assert r.json()["max_workspaces"] == 3


@pytest.mark.asyncio
async def test_delete_quotas_reverts_to_defaults(client: httpx.AsyncClient):
    await client.post("/v1/tenants/zeta/quotas", json={"max_workspaces": 1})
    r = await client.delete("/v1/tenants/zeta/quotas")
    assert r.status_code == 204
    follow = await client.get("/v1/tenants/zeta/quotas")
    assert follow.status_code == 200
    assert follow.json()["max_workspaces"] == DEFAULT_MAX_WORKSPACES


@pytest.mark.asyncio
async def test_delete_quotas_no_row_is_204(client: httpx.AsyncClient):
    r = await client.delete("/v1/tenants/never-existed/quotas")
    assert r.status_code == 204


@pytest.mark.asyncio
async def test_set_quotas_negative_value_rejected(client: httpx.AsyncClient):
    r = await client.post(
        "/v1/tenants/acme4/quotas",
        json={"max_workspaces": -1},
    )
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "INVALID_ARGUMENTS"


@pytest.mark.asyncio
async def test_set_quotas_extra_field_rejected(client: httpx.AsyncClient):
    r = await client.post(
        "/v1/tenants/acme5/quotas",
        json={"max_workspaces": 1, "bogus_field": 99},
    )
    assert r.status_code == 400


# ---------------------------------------------------------------------------
# /v1/tenants/{tenant_id}/usage — API


@pytest.mark.asyncio
async def test_usage_returns_expected_shape(client: httpx.AsyncClient):
    r = await client.get("/v1/tenants/default/usage")
    assert r.status_code == 200
    body = r.json()
    assert body["tenant_id"] == "default"
    # Identity owns ``active_tokens``; we have none yet.
    assert body["active_tokens"] == 0
    # Cross-service fields are zero with explanatory notes.
    assert body["workspaces"] == 0
    assert body["storage_gb"] == 0.0
    assert body["cost_usd_day"] == 0.0
    assert "notes" in body and "workspaces" in body["notes"]


@pytest.mark.asyncio
async def test_usage_counts_active_tokens(client: httpx.AsyncClient):
    # Issue two tokens for tenant "metrics" and verify usage reflects them.
    for i in range(2):
        r = await client.post(
            "/v1/tokens",
            json={"agent_id": f"a{i}", "tenant_id": "metrics", "scopes": []},
        )
        assert r.status_code == 201, r.text
    r = await client.get("/v1/tenants/metrics/usage")
    assert r.status_code == 200
    assert r.json()["active_tokens"] == 2


@pytest.mark.asyncio
async def test_usage_excludes_revoked_tokens(client: httpx.AsyncClient):
    r1 = await client.post(
        "/v1/tokens",
        json={"agent_id": "a", "tenant_id": "rev-test", "scopes": []},
    )
    r2 = await client.post(
        "/v1/tokens",
        json={"agent_id": "b", "tenant_id": "rev-test", "scopes": []},
    )
    j1 = r1.json()["jti"]
    await client.post(f"/v1/tokens/{j1}/revoke")
    r = await client.get("/v1/tenants/rev-test/usage")
    assert r.json()["active_tokens"] == 1
    # The non-revoked token is still counted.
    assert r2.status_code == 201


# ---------------------------------------------------------------------------
# QuotaStore — direct


@pytest.mark.asyncio
async def test_store_get_returns_defaults_for_unknown(settings):
    await init_db(settings.db_path)
    store = QuotaStore(settings.db_path)
    q = await store.get("nope")
    assert q.tenant_id == "nope"
    assert q.max_workspaces == DEFAULT_MAX_WORKSPACES


@pytest.mark.asyncio
async def test_store_set_then_get(settings):
    await init_db(settings.db_path)
    store = QuotaStore(settings.db_path)
    await store.set(
        "acme",
        TenantQuotasUpdate(max_workspaces=42, max_cost_usd_day=7.5),
    )
    q = await store.get("acme")
    assert q.max_workspaces == 42
    assert q.max_cost_usd_day == 7.5
    # Unspecified field falls back to default.
    assert q.max_storage_gb == DEFAULT_MAX_STORAGE_GB


@pytest.mark.asyncio
async def test_store_set_preserves_unrelated_fields(settings):
    await init_db(settings.db_path)
    store = QuotaStore(settings.db_path)
    await store.set("acme", TenantQuotasUpdate(max_workspaces=5))
    await store.set("acme", TenantQuotasUpdate(max_cost_usd_day=99.0))
    q = await store.get("acme")
    assert q.max_workspaces == 5
    assert q.max_cost_usd_day == 99.0


@pytest.mark.asyncio
async def test_store_delete_returns_true_when_row_existed(settings):
    await init_db(settings.db_path)
    store = QuotaStore(settings.db_path)
    await store.set("acme", TenantQuotasUpdate(max_workspaces=1))
    removed = await store.delete("acme")
    assert removed is True
    # Second delete is a no-op.
    assert await store.delete("acme") is False


@pytest.mark.asyncio
async def test_store_usage_independent_of_quotas(settings):
    await init_db(settings.db_path)
    store = QuotaStore(settings.db_path)
    usage = await store.usage("never-existed")
    assert usage.active_tokens == 0
    assert usage.workspaces == 0
    assert usage.storage_gb == 0.0
