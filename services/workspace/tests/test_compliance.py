# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 The Plinth Authors
"""Tests for the v1.0 GDPR admin endpoints on the workspace service."""

from __future__ import annotations

import json

import httpx
import pytest

from plinth_workspace.compliance import WorkspaceComplianceStore


@pytest.mark.asyncio
async def test_compliance_store_export_emits_workspace_jsonl(
    settings,
    store,
) -> None:
    ws = await store.create_workspace(
        "alpha-ws",
        metadata={"k": "v"},
        tenant_id="alpha",
    )
    await store.kv_put(ws.id, "k1", {"foo": "bar"})

    compliance = WorkspaceComplianceStore(settings.db_path, settings.blobs_dir)
    lines = []
    async for line in compliance.export_jsonl("alpha"):
        lines.append(line)
    types = {json.loads(line)["type"] for line in lines}
    assert "workspace" in types
    assert "kv_entry" in types
    # All lines should reference the alpha tenant's workspace.
    for line in lines:
        payload = json.loads(line)
        if payload["type"] == "workspace":
            assert payload["tenant_id"] == "alpha"
        elif payload["type"] == "kv_entry":
            assert payload["workspace_id"] == ws.id


@pytest.mark.asyncio
async def test_compliance_store_export_isolates_tenants(
    settings,
    store,
) -> None:
    """Tenant A's export must not include tenant B's data."""

    ws_a = await store.create_workspace("a-ws", tenant_id="alpha")
    ws_b = await store.create_workspace("b-ws", tenant_id="beta")
    await store.kv_put(ws_a.id, "ak", {"x": 1})
    await store.kv_put(ws_b.id, "bk", {"y": 2})

    compliance = WorkspaceComplianceStore(settings.db_path, settings.blobs_dir)
    alpha_lines = []
    async for line in compliance.export_jsonl("alpha"):
        alpha_lines.append(json.loads(line))
    workspace_ids = {
        line["workspace_id"]
        for line in alpha_lines
        if line["type"] == "kv_entry"
    }
    assert workspace_ids == {ws_a.id}


@pytest.mark.asyncio
async def test_compliance_store_delete_cascade(settings, store) -> None:
    ws = await store.create_workspace("alpha-ws", tenant_id="alpha")
    await store.kv_put(ws.id, "k1", {"foo": "bar"})

    compliance = WorkspaceComplianceStore(settings.db_path, settings.blobs_dir)
    counts = await compliance.delete_tenant_data("alpha")
    assert counts["workspaces"] == 1
    assert counts.get("kv_entries", 0) >= 1

    # Workspace is gone.
    workspaces = await store.list_workspaces(tenant_id="alpha")
    assert workspaces == []


@pytest.mark.asyncio
async def test_admin_export_endpoint(client: httpx.AsyncClient) -> None:
    """``GET /v1/admin/tenant/{id}/export-data`` returns JSONL body."""

    # Create a workspace under tenant 'default' (the test default).
    resp = await client.post("/v1/workspaces", json={"name": "test-export-ws"})
    assert resp.status_code == 201

    resp = await client.get("/v1/admin/tenant/default/export-data")
    assert resp.status_code == 200, resp.text
    assert resp.headers["content-type"].startswith("application/jsonl")
    lines = [line for line in resp.text.split("\n") if line]
    types = {json.loads(line)["type"] for line in lines}
    assert "workspace" in types


@pytest.mark.asyncio
async def test_admin_delete_endpoint(client: httpx.AsyncClient) -> None:
    """``DELETE /v1/admin/tenant/{id}/data`` cascades + returns counts."""

    # Seed two workspaces under default tenant.
    await client.post("/v1/workspaces", json={"name": "ws-1"})
    await client.post("/v1/workspaces", json={"name": "ws-2"})

    resp = await client.delete("/v1/admin/tenant/default/data")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert "deleted" in body
    assert body["deleted"]["workspaces"] >= 2

    # Subsequent list returns nothing tenant-scoped.
    resp = await client.get("/v1/workspaces")
    assert resp.status_code == 200
    assert resp.json()["workspaces"] == []


@pytest.mark.asyncio
async def test_admin_export_then_delete_then_list(
    client: httpx.AsyncClient,
) -> None:
    """Spot-check: export → delete → list returns nothing tenant-scoped."""

    await client.post("/v1/workspaces", json={"name": "ws-roundtrip"})

    pre = await client.get("/v1/admin/tenant/default/export-data")
    assert pre.status_code == 200
    pre_lines = [line for line in pre.text.split("\n") if line]
    assert any('"type": "workspace"' in line or '"type":"workspace"' in line for line in pre_lines)

    delete = await client.delete("/v1/admin/tenant/default/data")
    assert delete.status_code == 200

    post = await client.get("/v1/admin/tenant/default/export-data")
    assert post.status_code == 200
    post_lines = [line for line in post.text.split("\n") if line]
    workspace_lines = [
        line for line in post_lines if '"type":"workspace"' in line or '"type": "workspace"' in line
    ]
    assert workspace_lines == []
