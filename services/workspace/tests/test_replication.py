# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 The Plinth Authors
"""Tests for the v1.0 replication primitive + replica middleware."""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

import httpx
import pytest
import pytest_asyncio
from httpx import ASGITransport

from plinth_workspace.api import create_app
from plinth_workspace.replication import ReplicationLog
from plinth_workspace.settings import Settings


AUTH_HEADER = {"Authorization": "Bearer test-token"}


# ---------------------------------------------------------------------------
# ReplicationLog unit tests


@pytest.mark.asyncio
async def test_replication_log_append_and_fetch(tmp_path: Path) -> None:
    log = ReplicationLog(tmp_path / "wl.db", region_id="eu")
    await log.init()
    seq1 = await log.append("kv.set", {"key": "x", "value": 1}, workspace_id="ws_1")
    seq2 = await log.append("file.write", {"path": "/a"}, workspace_id="ws_1")
    assert seq1 == 1
    assert seq2 == 2

    entries = await log.fetch(since=0, limit=10)
    assert [e.seq for e in entries] == [1, 2]
    assert entries[0].kind == "kv.set"
    assert entries[0].payload == {"key": "x", "value": 1}
    assert entries[0].region_id == "eu"


@pytest.mark.asyncio
async def test_replication_log_fetch_since(tmp_path: Path) -> None:
    log = ReplicationLog(tmp_path / "wl.db", region_id="eu")
    await log.init()
    for i in range(5):
        await log.append(f"k{i}", {"i": i}, workspace_id="ws_1")
    entries = await log.fetch(since=3, limit=10)
    assert [e.seq for e in entries] == [4, 5]


@pytest.mark.asyncio
async def test_replication_log_fetch_limit(tmp_path: Path) -> None:
    log = ReplicationLog(tmp_path / "wl.db", region_id="eu")
    await log.init()
    for i in range(5):
        await log.append(f"k{i}", {"i": i}, workspace_id="ws_1")
    entries = await log.fetch(since=0, limit=2)
    assert [e.seq for e in entries] == [1, 2]


@pytest.mark.asyncio
async def test_replication_log_apply_entries_dedupes(tmp_path: Path) -> None:
    """Re-applying the same entries skips dupes."""

    log = ReplicationLog(tmp_path / "wl.db", region_id="us")
    await log.init()
    payload = [
        {
            "seq": 1,
            "kind": "kv.set",
            "workspace_id": "ws_1",
            "payload": {"k": "v"},
            "occurred_at": "2026-01-01T00:00:00+00:00",
            "region_id": "eu",
        },
        {
            "seq": 2,
            "kind": "file.write",
            "workspace_id": "ws_1",
            "payload": {"path": "/a"},
            "occurred_at": "2026-01-01T00:00:01+00:00",
            "region_id": "eu",
        },
    ]
    applied, skipped = await log.apply_entries(payload)
    assert applied == 2
    assert skipped == 0

    # Replay — all entries should be skipped.
    applied2, skipped2 = await log.apply_entries(payload)
    assert applied2 == 0
    assert skipped2 == 2


@pytest.mark.asyncio
async def test_replication_log_current_seq(tmp_path: Path) -> None:
    log = ReplicationLog(tmp_path / "wl.db", region_id="eu")
    await log.init()
    assert await log.current_seq() == 0
    await log.append("kv.set", {"k": "v"}, workspace_id="ws_1")
    await log.append("kv.set", {"k": "v2"}, workspace_id="ws_1")
    assert await log.current_seq() == 2


# ---------------------------------------------------------------------------
# /v1/admin/replication/* endpoints


@pytest_asyncio.fixture()
async def primary_client(tmp_path: Path) -> AsyncIterator[httpx.AsyncClient]:
    settings = Settings(
        data_dir=tmp_path,
        log_level="WARNING",
        region_id="eu-west-1",
        replication_mode="primary",
    )
    from plinth_workspace.db import init_db

    settings.data_dir.mkdir(parents=True, exist_ok=True)
    settings.blobs_dir.mkdir(parents=True, exist_ok=True)
    await init_db(settings.db_path)
    app = create_app(settings)
    # Initialise the replication log (lifespan would do this).
    await app.state.replication_log.init()
    transport = ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://test", headers=AUTH_HEADER
    ) as client:
        yield client


@pytest_asyncio.fixture()
async def replica_client(tmp_path: Path) -> AsyncIterator[httpx.AsyncClient]:
    settings = Settings(
        data_dir=tmp_path,
        log_level="WARNING",
        region_id="us-east-1",
        region_peers=["eu-west-1"],
        region_peer_urls={"eu-west-1": "http://eu.example"},
        replication_mode="replica",
        region_primary_url="http://eu.example",
    )
    from plinth_workspace.db import init_db

    settings.data_dir.mkdir(parents=True, exist_ok=True)
    settings.blobs_dir.mkdir(parents=True, exist_ok=True)
    await init_db(settings.db_path)
    app = create_app(settings)
    await app.state.replication_log.init()
    transport = ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://test", headers=AUTH_HEADER
    ) as client:
        yield client


@pytest_asyncio.fixture()
async def standalone_client(tmp_path: Path) -> AsyncIterator[httpx.AsyncClient]:
    settings = Settings(
        data_dir=tmp_path,
        log_level="WARNING",
    )
    from plinth_workspace.db import init_db

    settings.data_dir.mkdir(parents=True, exist_ok=True)
    settings.blobs_dir.mkdir(parents=True, exist_ok=True)
    await init_db(settings.db_path)
    app = create_app(settings)
    await app.state.replication_log.init()
    transport = ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://test", headers=AUTH_HEADER
    ) as client:
        yield client


@pytest.mark.asyncio
async def test_replication_status_endpoint(primary_client: httpx.AsyncClient) -> None:
    resp = await primary_client.get("/v1/admin/replication/status")
    assert resp.status_code == 200
    data = resp.json()
    assert data["mode"] == "primary"
    assert data["region"] == "eu-west-1"
    assert data["current_seq"] == 0
    assert data["peers_lag"] == {}


@pytest.mark.asyncio
async def test_replication_log_appended_on_primary_writes(
    primary_client: httpx.AsyncClient,
) -> None:
    """A successful write on a primary lands an entry in the log."""

    resp = await primary_client.post("/v1/workspaces", json={"name": "ws1"})
    assert resp.status_code == 201

    log_resp = await primary_client.get("/v1/admin/replication/log")
    assert log_resp.status_code == 200
    entries = log_resp.json()["entries"]
    assert len(entries) >= 1
    assert any(e["kind"].startswith("workspace.post") for e in entries)


@pytest.mark.asyncio
async def test_replication_log_not_appended_on_standalone(
    standalone_client: httpx.AsyncClient,
) -> None:
    """Standalone deployments don't write to the log."""

    resp = await standalone_client.post("/v1/workspaces", json={"name": "ws1"})
    assert resp.status_code == 201

    log_resp = await standalone_client.get("/v1/admin/replication/log")
    assert log_resp.status_code == 200
    assert log_resp.json()["entries"] == []


@pytest.mark.asyncio
async def test_replication_apply_endpoint(primary_client: httpx.AsyncClient) -> None:
    payload = [
        {
            "seq": 100,
            "kind": "kv.set",
            "workspace_id": "ws_1",
            "payload": {"k": "v"},
            "occurred_at": "2026-01-01T00:00:00+00:00",
            "region_id": "eu",
        },
    ]
    resp = await primary_client.post("/v1/admin/replication/apply", json=payload)
    assert resp.status_code == 201
    body = resp.json()
    assert body["applied"] == 1
    assert body["skipped"] == 0


# ---------------------------------------------------------------------------
# Replica mode


@pytest.mark.asyncio
async def test_replica_get_works(replica_client: httpx.AsyncClient) -> None:
    """Replicas serve GETs normally."""

    resp = await replica_client.get("/v1/workspaces")
    assert resp.status_code == 200


@pytest.mark.asyncio
async def test_replica_post_returns_421(replica_client: httpx.AsyncClient) -> None:
    """Replicas reject POST/PUT/DELETE/PATCH with 421 + redirect headers."""

    resp = await replica_client.post("/v1/workspaces", json={"name": "ws1"})
    assert resp.status_code == 421
    assert resp.headers["X-Plinth-Primary-Region"] == "eu-west-1"
    assert resp.headers["X-Plinth-Primary-URL"] == "http://eu.example"
    assert resp.headers.get("Location", "").startswith("http://eu.example")
    error = resp.json()["error"]
    assert error["code"] == "REPLICA_READ_ONLY"


@pytest.mark.asyncio
async def test_replica_put_returns_421(replica_client: httpx.AsyncClient) -> None:
    resp = await replica_client.put(
        "/v1/workspaces/ws_1/kv/foo",
        json={"value": "bar"},
    )
    assert resp.status_code == 421


@pytest.mark.asyncio
async def test_replica_delete_returns_421(replica_client: httpx.AsyncClient) -> None:
    resp = await replica_client.delete("/v1/workspaces/ws_1")
    assert resp.status_code == 421


@pytest.mark.asyncio
async def test_replica_health_passes(replica_client: httpx.AsyncClient) -> None:
    """``/healthz`` is allowlisted on replicas."""

    resp = await replica_client.get("/healthz")
    assert resp.status_code == 200


@pytest.mark.asyncio
async def test_replica_replication_apply_passes(replica_client: httpx.AsyncClient) -> None:
    """Replicas accept replication-apply POSTs (otherwise replication can't work)."""

    resp = await replica_client.post(
        "/v1/admin/replication/apply",
        json=[{
            "seq": 1,
            "kind": "kv.set",
            "workspace_id": "ws_1",
            "payload": {"k": "v"},
            "occurred_at": "2026-01-01T00:00:00+00:00",
            "region_id": "eu",
        }],
    )
    assert resp.status_code == 201
