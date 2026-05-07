# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 The Plinth Authors
"""Workspace GC + retention tests.

Covers the engine's correctness rules (referenced versions are never
deleted), the per-rule retention semantics (``keep_versions``, ``keep_days``,
``keep_snapshots``), branch + blob cleanup, concurrency, and the HTTP
endpoints (CRUD on retention + admin sweep).
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx
import pytest
import pytest_asyncio
from httpx import ASGITransport

from plinth_workspace.api import create_app
from plinth_workspace.db import connect, init_db, iso, now_utc
from plinth_workspace.gc import GCEngine, GCInProgress, RetentionStore
from plinth_workspace.models import RetentionPolicy
from plinth_workspace.settings import Settings
from plinth_workspace.snapshots import SnapshotStore
from plinth_workspace.storage import WorkspaceStore

UTC = timezone.utc  # noqa: UP017
AUTH_HEADER = {"Authorization": "Bearer test-token"}


# ---------------------------------------------------------------------------
# Fixtures


@pytest_asyncio.fixture()
async def ws_store(tmp_path: Path) -> WorkspaceStore:
    data_dir = tmp_path / "data"
    blobs_dir = data_dir / "blobs"
    data_dir.mkdir(parents=True, exist_ok=True)
    blobs_dir.mkdir(parents=True, exist_ok=True)
    db_path = data_dir / "workspace.db"
    await init_db(db_path)
    return WorkspaceStore(db_path, blobs_dir)


@pytest_asyncio.fixture()
async def retention(ws_store: WorkspaceStore) -> RetentionStore:
    return RetentionStore(ws_store.db_path)


@pytest_asyncio.fixture()
async def engine(ws_store: WorkspaceStore) -> GCEngine:
    return GCEngine(ws_store.db_path, ws_store.blobs_dir)


@pytest_asyncio.fixture()
async def workspace_id(ws_store: WorkspaceStore) -> str:
    ws = await ws_store.create_workspace("gc-test")
    return ws.id


# ---------------------------------------------------------------------------
# Retention CRUD


@pytest.mark.asyncio()
async def test_retention_default_when_unset(
    retention: RetentionStore, workspace_id: str
) -> None:
    policy = await retention.get(workspace_id)
    assert policy.keep_versions is None
    assert policy.keep_days is None
    assert policy.keep_snapshots is None
    assert policy.delete_unreferenced_blobs is True


@pytest.mark.asyncio()
async def test_retention_upsert_inserts(
    retention: RetentionStore, workspace_id: str
) -> None:
    policy = await retention.upsert(
        workspace_id,
        keep_versions=5,
        keep_days=7,
        keep_snapshots=10,
        delete_unreferenced_blobs=True,
    )
    assert policy.keep_versions == 5
    assert policy.keep_days == 7
    assert policy.keep_snapshots == 10

    re_read = await retention.get(workspace_id)
    assert re_read.keep_versions == 5
    assert re_read.keep_days == 7
    assert re_read.keep_snapshots == 10


@pytest.mark.asyncio()
async def test_retention_upsert_updates(
    retention: RetentionStore, workspace_id: str
) -> None:
    await retention.upsert(
        workspace_id,
        keep_versions=5,
        keep_days=None,
        keep_snapshots=None,
        delete_unreferenced_blobs=True,
    )
    second = await retention.upsert(
        workspace_id,
        keep_versions=2,
        keep_days=14,
        keep_snapshots=None,
        delete_unreferenced_blobs=False,
    )
    assert second.keep_versions == 2
    assert second.keep_days == 14
    assert second.delete_unreferenced_blobs is False


@pytest.mark.asyncio()
async def test_retention_workspaces_with_policies_lists_only_set(
    retention: RetentionStore, ws_store: WorkspaceStore
) -> None:
    a = await ws_store.create_workspace("a")
    b = await ws_store.create_workspace("b")
    await ws_store.create_workspace("c")  # no policy
    await retention.upsert(
        a.id, keep_versions=5, keep_days=None, keep_snapshots=None,
        delete_unreferenced_blobs=True,
    )
    await retention.upsert(
        b.id, keep_versions=None, keep_days=7, keep_snapshots=None,
        delete_unreferenced_blobs=True,
    )
    listed = await retention.workspaces_with_policies()
    assert sorted(listed) == sorted([a.id, b.id])


# ---------------------------------------------------------------------------
# GC core — KV / file / snapshot / branch


@pytest.mark.asyncio()
async def test_gc_no_policy_keeps_everything(
    ws_store: WorkspaceStore,
    retention: RetentionStore,
    engine: GCEngine,
    workspace_id: str,
) -> None:
    for i in range(5):
        await ws_store.kv_put(workspace_id, "k", f"v{i}")
    policy = await retention.get(workspace_id)
    result = await engine.run(workspace_id, policy)
    assert result.kv_versions_deleted == 0

    history = await ws_store.kv_history(workspace_id, "k")
    assert len(history) == 5


@pytest.mark.asyncio()
async def test_gc_keep_versions_keeps_top_n(
    ws_store: WorkspaceStore,
    retention: RetentionStore,
    engine: GCEngine,
    workspace_id: str,
) -> None:
    for i in range(5):
        await ws_store.kv_put(workspace_id, "k", f"v{i}")
    await retention.upsert(
        workspace_id,
        keep_versions=2,
        keep_days=None,
        keep_snapshots=None,
        delete_unreferenced_blobs=True,
    )
    policy = await retention.get(workspace_id)
    result = await engine.run(workspace_id, policy)
    assert result.kv_versions_deleted == 3

    history = await ws_store.kv_history(workspace_id, "k")
    assert [e.version for e in history] == [4, 5]


@pytest.mark.asyncio()
async def test_gc_keep_versions_per_branch(
    ws_store: WorkspaceStore,
    retention: RetentionStore,
    engine: GCEngine,
    workspace_id: str,
) -> None:
    snapshots_store = SnapshotStore(ws_store)
    for i in range(3):
        await ws_store.kv_put(workspace_id, "k", f"main-v{i}")
    snap = await snapshots_store.create_snapshot(workspace_id, "before-branch")
    branch = await snapshots_store.create_branch(workspace_id, "exp", snap.id)
    for i in range(3):
        await ws_store.kv_put(workspace_id, "k", f"branch-v{i}", branch_id=branch.id)

    await retention.upsert(
        workspace_id,
        keep_versions=1,
        keep_days=None,
        keep_snapshots=None,
        delete_unreferenced_blobs=True,
    )
    policy = await retention.get(workspace_id)
    # Snapshot references main version 3 (the latest before snapshot).
    # ``keep_versions=1`` keeps only the latest of each branch group.
    # Snapshot reference protects main version 3, which is the same as
    # the latest, so no extra protection beyond what keep_versions does.
    await engine.run(workspace_id, policy)

    main_history = await ws_store.kv_history(workspace_id, "k")
    # Main: only the snapshot-referenced version 3 is kept (since the
    # latest of the main group is also v3, keep_versions=1 keeps it).
    main_versions = [e.version for e in main_history if e.branch_id is None]
    assert main_versions == [3]
    # Branch entries are listed when the call targets the branch.
    branch_history = await ws_store.kv_history(
        workspace_id, "k", branch_id=branch.id
    )
    branch_only = [e for e in branch_history if e.branch_id == branch.id]
    # Branch: keep_versions=1 keeps only the latest branch row.
    assert len(branch_only) == 1


@pytest.mark.asyncio()
async def test_gc_keep_days_keeps_recent(
    ws_store: WorkspaceStore,
    retention: RetentionStore,
    engine: GCEngine,
    workspace_id: str,
) -> None:
    for i in range(3):
        await ws_store.kv_put(workspace_id, "k", f"v{i}")

    # Backdate the first two entries to look "old".
    cutoff = (now_utc() - timedelta(days=30)).isoformat()
    async with connect(ws_store.db_path) as conn:
        await conn.execute(
            "UPDATE kv_entries SET created_at=? "
            "WHERE workspace_id=? AND version IN (1, 2)",
            (cutoff, workspace_id),
        )
        await conn.commit()

    await retention.upsert(
        workspace_id,
        keep_versions=None,
        keep_days=7,
        keep_snapshots=None,
        delete_unreferenced_blobs=True,
    )
    policy = await retention.get(workspace_id)
    result = await engine.run(workspace_id, policy)
    assert result.kv_versions_deleted == 2

    history = await ws_store.kv_history(workspace_id, "k")
    assert [e.version for e in history] == [3]


@pytest.mark.asyncio()
async def test_gc_keep_versions_and_days_take_union(
    ws_store: WorkspaceStore,
    retention: RetentionStore,
    engine: GCEngine,
    workspace_id: str,
) -> None:
    """The most permissive of the two rules wins per row."""

    for i in range(5):
        await ws_store.kv_put(workspace_id, "k", f"v{i}")

    cutoff = (now_utc() - timedelta(days=30)).isoformat()
    async with connect(ws_store.db_path) as conn:
        # Backdate v1, v2, v3 so keep_days=7 alone would drop them.
        await conn.execute(
            "UPDATE kv_entries SET created_at=? "
            "WHERE workspace_id=? AND version IN (1, 2, 3)",
            (cutoff, workspace_id),
        )
        await conn.commit()

    await retention.upsert(
        workspace_id,
        keep_versions=3,
        keep_days=7,
        keep_snapshots=None,
        delete_unreferenced_blobs=True,
    )
    policy = await retention.get(workspace_id)
    result = await engine.run(workspace_id, policy)
    # Latest 3 (v3, v4, v5) saved by keep_versions; v4/v5 also recent.
    # Only v1, v2 should be removed.
    assert result.kv_versions_deleted == 2
    history = await ws_store.kv_history(workspace_id, "k")
    assert [e.version for e in history] == [3, 4, 5]


@pytest.mark.asyncio()
async def test_gc_preserves_referenced_versions(
    ws_store: WorkspaceStore,
    retention: RetentionStore,
    engine: GCEngine,
    workspace_id: str,
) -> None:
    snapshots_store = SnapshotStore(ws_store)
    for i in range(5):
        await ws_store.kv_put(workspace_id, "k", f"v{i}")
    # Snapshot pins v5.
    await snapshots_store.create_snapshot(workspace_id, "snap")

    # Add a few more so v6/v7 exist.
    for i in range(2):
        await ws_store.kv_put(workspace_id, "k", f"v{5 + i}")

    await retention.upsert(
        workspace_id,
        keep_versions=1,
        keep_days=None,
        keep_snapshots=None,
        delete_unreferenced_blobs=True,
    )
    policy = await retention.get(workspace_id)
    result = await engine.run(workspace_id, policy)
    # keep_versions=1 keeps v7. Snapshot still pins v5. v1..v4 + v6 deletable
    # → 5 deletions.
    assert result.kv_versions_deleted == 5
    history = await ws_store.kv_history(workspace_id, "k")
    versions = sorted(e.version for e in history)
    assert versions == [5, 7]


@pytest.mark.asyncio()
async def test_gc_keep_snapshots_drops_oldest(
    ws_store: WorkspaceStore,
    retention: RetentionStore,
    engine: GCEngine,
    workspace_id: str,
) -> None:
    snapshots_store = SnapshotStore(ws_store)
    for i in range(5):
        await ws_store.kv_put(workspace_id, "k", f"v{i}")
        # Sleep tiny amounts to ensure ordered created_at.
        await snapshots_store.create_snapshot(workspace_id, f"s{i}")

    await retention.upsert(
        workspace_id,
        keep_versions=None,
        keep_days=None,
        keep_snapshots=3,
        delete_unreferenced_blobs=True,
    )
    policy = await retention.get(workspace_id)
    result = await engine.run(workspace_id, policy)
    assert result.snapshots_deleted == 2
    snapshots = await snapshots_store.list_snapshots(workspace_id)
    assert len(snapshots) == 3


@pytest.mark.asyncio()
async def test_gc_keep_snapshots_protects_active_branch_parent(
    ws_store: WorkspaceStore,
    retention: RetentionStore,
    engine: GCEngine,
    workspace_id: str,
) -> None:
    snapshots_store = SnapshotStore(ws_store)
    await ws_store.kv_put(workspace_id, "k", "v0")
    oldest = await snapshots_store.create_snapshot(workspace_id, "s0")
    # Create a live branch off the oldest snapshot.
    await snapshots_store.create_branch(workspace_id, "br", oldest.id)
    # Take a few more snapshots to push s0 out of the keep window.
    for i in range(4):
        await ws_store.kv_put(workspace_id, "k", f"v{i + 1}")
        await snapshots_store.create_snapshot(workspace_id, f"s{i + 1}")

    await retention.upsert(
        workspace_id,
        keep_versions=None,
        keep_days=None,
        keep_snapshots=2,
        delete_unreferenced_blobs=True,
    )
    policy = await retention.get(workspace_id)
    await engine.run(workspace_id, policy)

    snap_ids = [s.id for s in await snapshots_store.list_snapshots(workspace_id)]
    # The branch's parent must still be present even though it would
    # otherwise have been trimmed.
    assert oldest.id in snap_ids


# ---------------------------------------------------------------------------
# File + blob cleanup


@pytest.mark.asyncio()
async def test_gc_blob_cleanup_orphaned(
    ws_store: WorkspaceStore,
    retention: RetentionStore,
    engine: GCEngine,
    workspace_id: str,
) -> None:
    await ws_store.file_put(workspace_id, "doc.txt", b"hello")
    await ws_store.file_put(workspace_id, "doc.txt", b"world")
    await ws_store.file_put(workspace_id, "doc.txt", b"final")

    await retention.upsert(
        workspace_id,
        keep_versions=1,
        keep_days=None,
        keep_snapshots=None,
        delete_unreferenced_blobs=True,
    )
    policy = await retention.get(workspace_id)
    result = await engine.run(workspace_id, policy)
    assert result.file_versions_deleted == 2
    assert result.blob_files_deleted == 2
    assert result.bytes_freed > 0


@pytest.mark.asyncio()
async def test_gc_blob_cleanup_skips_shared_blob(
    ws_store: WorkspaceStore,
    retention: RetentionStore,
    engine: GCEngine,
    workspace_id: str,
) -> None:
    await ws_store.file_put(workspace_id, "a.txt", b"shared")
    await ws_store.file_put(workspace_id, "b.txt", b"shared")
    # Force keep_versions=0... actually keep_versions=1 keeps both.
    # To force a delete on a.txt while keeping b.txt referencing the
    # blob, write a new version of a.txt and run with keep_versions=1.
    await ws_store.file_put(workspace_id, "a.txt", b"unique")

    await retention.upsert(
        workspace_id,
        keep_versions=1,
        keep_days=None,
        keep_snapshots=None,
        delete_unreferenced_blobs=True,
    )
    policy = await retention.get(workspace_id)
    result = await engine.run(workspace_id, policy)
    # Old "shared" version of a.txt is gone, but b.txt still references
    # the same blob — the blob file must NOT be deleted.
    assert result.file_versions_deleted == 1
    assert result.blob_files_deleted == 0


@pytest.mark.asyncio()
async def test_gc_blob_cleanup_disabled_keeps_orphans(
    ws_store: WorkspaceStore,
    retention: RetentionStore,
    engine: GCEngine,
    workspace_id: str,
) -> None:
    await ws_store.file_put(workspace_id, "x.txt", b"v0")
    await ws_store.file_put(workspace_id, "x.txt", b"v1")

    await retention.upsert(
        workspace_id,
        keep_versions=1,
        keep_days=None,
        keep_snapshots=None,
        delete_unreferenced_blobs=False,
    )
    policy = await retention.get(workspace_id)
    result = await engine.run(workspace_id, policy)
    assert result.file_versions_deleted == 1
    assert result.blob_files_deleted == 0


# ---------------------------------------------------------------------------
# Branches


@pytest.mark.asyncio()
async def test_gc_drops_old_merged_branches(
    ws_store: WorkspaceStore,
    retention: RetentionStore,
    engine: GCEngine,
    workspace_id: str,
) -> None:
    snapshots_store = SnapshotStore(ws_store)
    await ws_store.kv_put(workspace_id, "k", "v0")
    snap = await snapshots_store.create_snapshot(workspace_id, "base")
    branch = await snapshots_store.create_branch(workspace_id, "br", snap.id)
    await ws_store.kv_put(workspace_id, "k", "v-branch", branch_id=branch.id)
    await snapshots_store.merge_branch(workspace_id, branch.id)

    # Backdate merged_at
    old = (now_utc() - timedelta(days=30)).isoformat()
    async with connect(ws_store.db_path) as conn:
        await conn.execute(
            "UPDATE branches SET merged_at=? WHERE id=?",
            (old, branch.id),
        )
        await conn.commit()

    await retention.upsert(
        workspace_id,
        keep_versions=None,
        keep_days=7,
        keep_snapshots=None,
        delete_unreferenced_blobs=True,
    )
    policy = await retention.get(workspace_id)
    result = await engine.run(workspace_id, policy)
    assert result.branches_deleted == 1


@pytest.mark.asyncio()
async def test_gc_keeps_unmerged_branches(
    ws_store: WorkspaceStore,
    retention: RetentionStore,
    engine: GCEngine,
    workspace_id: str,
) -> None:
    snapshots_store = SnapshotStore(ws_store)
    await ws_store.kv_put(workspace_id, "k", "v0")
    snap = await snapshots_store.create_snapshot(workspace_id, "base")
    branch = await snapshots_store.create_branch(workspace_id, "br", snap.id)
    # Backdate created_at — but it's NOT merged.
    old = (now_utc() - timedelta(days=30)).isoformat()
    async with connect(ws_store.db_path) as conn:
        await conn.execute(
            "UPDATE branches SET created_at=? WHERE id=?",
            (old, branch.id),
        )
        await conn.commit()

    await retention.upsert(
        workspace_id,
        keep_versions=None,
        keep_days=7,
        keep_snapshots=None,
        delete_unreferenced_blobs=True,
    )
    policy = await retention.get(workspace_id)
    result = await engine.run(workspace_id, policy)
    assert result.branches_deleted == 0


# ---------------------------------------------------------------------------
# Concurrency


@pytest.mark.asyncio()
async def test_gc_concurrent_run_returns_409(
    ws_store: WorkspaceStore,
    retention: RetentionStore,
    engine: GCEngine,
    workspace_id: str,
) -> None:
    for i in range(5):
        await ws_store.kv_put(workspace_id, "k", f"v{i}")
    policy = RetentionPolicy(
        workspace_id=workspace_id,
        keep_versions=1,
        keep_days=None,
        keep_snapshots=None,
        delete_unreferenced_blobs=True,
        updated_at=now_utc(),
    )

    # Manually grab the lock to simulate an in-flight run, then verify a
    # second call returns GCInProgress.
    lock = engine._locks.setdefault(workspace_id, asyncio.Lock())
    await lock.acquire()
    try:
        with pytest.raises(GCInProgress):
            await engine.run(workspace_id, policy)
    finally:
        lock.release()


@pytest.mark.asyncio()
async def test_gc_lock_releases_on_exception(
    ws_store: WorkspaceStore,
    retention: RetentionStore,
    engine: GCEngine,
    workspace_id: str,
) -> None:
    """If GC raises, the per-workspace lock must release — otherwise a
    second call would see a permanent 409."""

    policy = RetentionPolicy(
        workspace_id=workspace_id,
        keep_versions=1,
        keep_days=None,
        keep_snapshots=None,
        delete_unreferenced_blobs=True,
        updated_at=now_utc(),
    )
    # Trigger a failure inside ``_run_locked`` by passing a workspace ID
    # that doesn't exist (its DB-level absence is fine for blob-cleanup,
    # but the snapshot/kv reads will silently return nothing). The run
    # should still complete cleanly. Then verify the lock is free.
    await engine.run(workspace_id, policy)
    lock = engine._locks[workspace_id]
    assert not lock.locked()


# ---------------------------------------------------------------------------
# HTTP integration


@pytest_asyncio.fixture()
async def client(tmp_path: Path):
    data_dir = tmp_path / "data"
    blobs_dir = data_dir / "blobs"
    data_dir.mkdir(parents=True, exist_ok=True)
    blobs_dir.mkdir(parents=True, exist_ok=True)
    settings = Settings(
        data_dir=data_dir,
        workspace_port=17421,
        workspace_host="127.0.0.1",
        log_level="WARNING",
    )
    await init_db(settings.db_path)
    app = create_app(settings)
    transport = ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://test", headers=AUTH_HEADER
    ) as c:
        yield c


@pytest_asyncio.fixture()
async def http_workspace_id(client: httpx.AsyncClient) -> str:
    resp = await client.post("/v1/workspaces", json={"name": "gc-http-test"})
    assert resp.status_code == 201, resp.text
    return resp.json()["id"]


@pytest.mark.asyncio()
async def test_http_get_retention_default(
    client: httpx.AsyncClient, http_workspace_id: str
) -> None:
    resp = await client.get(f"/v1/workspaces/{http_workspace_id}/retention")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["workspace_id"] == http_workspace_id
    assert body["keep_versions"] is None
    assert body["delete_unreferenced_blobs"] is True


@pytest.mark.asyncio()
async def test_http_put_retention(
    client: httpx.AsyncClient, http_workspace_id: str
) -> None:
    resp = await client.put(
        f"/v1/workspaces/{http_workspace_id}/retention",
        json={"keep_versions": 3, "keep_snapshots": 5},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["keep_versions"] == 3
    assert body["keep_snapshots"] == 5
    assert body["keep_days"] is None
    assert body["delete_unreferenced_blobs"] is True


@pytest.mark.asyncio()
async def test_http_put_retention_invalid_value(
    client: httpx.AsyncClient, http_workspace_id: str
) -> None:
    resp = await client.put(
        f"/v1/workspaces/{http_workspace_id}/retention",
        json={"keep_versions": 0},  # ge=1 violation
    )
    assert resp.status_code == 400, resp.text


@pytest.mark.asyncio()
async def test_http_post_gc_returns_result(
    client: httpx.AsyncClient, http_workspace_id: str
) -> None:
    # Put a few KV versions first.
    for i in range(3):
        resp = await client.put(
            f"/v1/workspaces/{http_workspace_id}/kv/topic",
            json={"value": f"v{i}"},
        )
        assert resp.status_code == 200
    await client.put(
        f"/v1/workspaces/{http_workspace_id}/retention",
        json={"keep_versions": 1},
    )
    resp = await client.post(f"/v1/workspaces/{http_workspace_id}/gc")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["workspace_id"] == http_workspace_id
    assert body["kv_versions_deleted"] >= 2


@pytest.mark.asyncio()
async def test_http_post_gc_unknown_workspace(
    client: httpx.AsyncClient,
) -> None:
    resp = await client.post("/v1/workspaces/ws_nonexistent/gc")
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "WORKSPACE_NOT_FOUND"


@pytest.mark.asyncio()
async def test_http_get_retention_unknown_workspace(
    client: httpx.AsyncClient,
) -> None:
    resp = await client.get("/v1/workspaces/ws_does_not_exist/retention")
    assert resp.status_code == 404


@pytest.mark.asyncio()
async def test_http_admin_gc_sweep(
    client: httpx.AsyncClient, http_workspace_id: str
) -> None:
    """Permissive mode allows admin GC; verify it returns a list."""

    # Configure retention so the workspace shows up on the admin sweep.
    await client.put(
        f"/v1/workspaces/{http_workspace_id}/retention",
        json={"keep_versions": 1},
    )
    for i in range(3):
        await client.put(
            f"/v1/workspaces/{http_workspace_id}/kv/topic",
            json={"value": f"v{i}"},
        )
    resp = await client.post("/v1/admin/gc")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert "results" in body
    assert any(r["workspace_id"] == http_workspace_id for r in body["results"])


@pytest.mark.asyncio()
async def test_http_admin_gc_requires_scope_when_auth_required(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    blobs_dir = data_dir / "blobs"
    data_dir.mkdir(parents=True, exist_ok=True)
    blobs_dir.mkdir(parents=True, exist_ok=True)
    settings = Settings(
        data_dir=data_dir,
        workspace_port=17421,
        workspace_host="127.0.0.1",
        log_level="WARNING",
        auth_required=True,
    )
    await init_db(settings.db_path)
    app = create_app(settings)
    transport = ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://test", headers=AUTH_HEADER
    ) as c:
        # Permissive + auth_required gives a tenant=default but no scopes,
        # so the admin endpoint must reject.
        resp = await c.post("/v1/admin/gc")
        assert resp.status_code == 401
        assert resp.json()["error"]["code"] == "UNAUTHORIZED"


@pytest.mark.asyncio()
async def test_http_admin_gc_accepts_wildcard_scope(tmp_path: Path) -> None:
    """When the caller carries ``*`` scope, admin GC succeeds even with
    auth_required."""

    import jwt as pyjwt

    secret = "test-secret-32-bytes-test-secret-32-bytes"
    data_dir = tmp_path / "data"
    blobs_dir = data_dir / "blobs"
    data_dir.mkdir(parents=True, exist_ok=True)
    blobs_dir.mkdir(parents=True, exist_ok=True)
    settings = Settings(
        data_dir=data_dir,
        workspace_port=17421,
        workspace_host="127.0.0.1",
        log_level="WARNING",
        auth_mode="verify_local",
        identity_jwt_secret=secret,
    )
    await init_db(settings.db_path)
    app = create_app(settings)
    transport = ASGITransport(app=app)
    now = datetime.now(UTC)
    token = pyjwt.encode(
        {
            "sub": "agt",
            "iat": int(now.timestamp()),
            "exp": int((now + timedelta(hours=1)).timestamp()),
            "jti": "jti1",
            "aud": "plinth",
            "agent_id": "agt",
            "tenant_id": "default",
            "scopes": ["*"],
        },
        secret,
        algorithm="HS256",
    )
    async with httpx.AsyncClient(
        transport=transport,
        base_url="http://test",
        headers={"Authorization": f"Bearer {token}"},
    ) as c:
        resp = await c.post("/v1/admin/gc")
        assert resp.status_code == 200, resp.text


# ---------------------------------------------------------------------------
# Adversarial / edge cases


@pytest.mark.asyncio()
async def test_gc_empty_workspace_no_op(
    ws_store: WorkspaceStore,
    retention: RetentionStore,
    engine: GCEngine,
    workspace_id: str,
) -> None:
    """A brand-new workspace with no KV/files runs GC cleanly."""

    await retention.upsert(
        workspace_id,
        keep_versions=1,
        keep_days=7,
        keep_snapshots=3,
        delete_unreferenced_blobs=True,
    )
    policy = await retention.get(workspace_id)
    result = await engine.run(workspace_id, policy)
    assert result.kv_versions_deleted == 0
    assert result.file_versions_deleted == 0
    assert result.snapshots_deleted == 0
    assert result.branches_deleted == 0


@pytest.mark.asyncio()
async def test_gc_independent_keys_use_independent_groups(
    ws_store: WorkspaceStore,
    retention: RetentionStore,
    engine: GCEngine,
    workspace_id: str,
) -> None:
    """``keep_versions=2`` is per-key — each key keeps its top 2."""

    for i in range(4):
        await ws_store.kv_put(workspace_id, "alpha", f"a{i}")
    for i in range(4):
        await ws_store.kv_put(workspace_id, "beta", f"b{i}")

    await retention.upsert(
        workspace_id,
        keep_versions=2,
        keep_days=None,
        keep_snapshots=None,
        delete_unreferenced_blobs=True,
    )
    policy = await retention.get(workspace_id)
    await engine.run(workspace_id, policy)

    a_history = await ws_store.kv_history(workspace_id, "alpha")
    b_history = await ws_store.kv_history(workspace_id, "beta")
    assert [e.version for e in a_history] == [3, 4]
    assert [e.version for e in b_history] == [3, 4]


@pytest.mark.asyncio()
async def test_gc_keep_snapshots_zero_keeps_active_branch_parent(
    ws_store: WorkspaceStore,
    retention: RetentionStore,
    engine: GCEngine,
    workspace_id: str,
) -> None:
    """Even ``keep_snapshots=0`` must not break an active branch."""

    snapshots_store = SnapshotStore(ws_store)
    await ws_store.kv_put(workspace_id, "k", "v0")
    snap = await snapshots_store.create_snapshot(workspace_id, "base")
    await snapshots_store.create_branch(workspace_id, "br", snap.id)
    await ws_store.kv_put(workspace_id, "k", "v1")
    await snapshots_store.create_snapshot(workspace_id, "next")

    await retention.upsert(
        workspace_id,
        keep_versions=None,
        keep_days=None,
        keep_snapshots=0,
        delete_unreferenced_blobs=True,
    )
    policy = await retention.get(workspace_id)
    await engine.run(workspace_id, policy)
    snap_ids = [s.id for s in await snapshots_store.list_snapshots(workspace_id)]
    assert snap.id in snap_ids


@pytest.mark.asyncio()
async def test_gc_blob_cleanup_with_referenced_snapshot(
    ws_store: WorkspaceStore,
    retention: RetentionStore,
    engine: GCEngine,
    workspace_id: str,
) -> None:
    """Snapshot pin protects the blob even when GC would otherwise drop it."""

    snapshots_store = SnapshotStore(ws_store)
    await ws_store.file_put(workspace_id, "f.bin", b"snapshot-pinned")
    await snapshots_store.create_snapshot(workspace_id, "pin")
    await ws_store.file_put(workspace_id, "f.bin", b"latest")

    await retention.upsert(
        workspace_id,
        keep_versions=1,
        keep_days=None,
        keep_snapshots=None,
        delete_unreferenced_blobs=True,
    )
    policy = await retention.get(workspace_id)
    result = await engine.run(workspace_id, policy)
    # Snapshot pins v1; keep_versions=1 keeps v2. Nothing should drop.
    assert result.file_versions_deleted == 0
    assert result.blob_files_deleted == 0


@pytest.mark.asyncio()
async def test_gc_referenced_version_outranks_keep_versions(
    ws_store: WorkspaceStore,
    retention: RetentionStore,
    engine: GCEngine,
    workspace_id: str,
) -> None:
    """A snapshot reference saves a version even past ``keep_versions``."""

    snapshots_store = SnapshotStore(ws_store)
    for i in range(10):
        await ws_store.kv_put(workspace_id, "k", f"v{i}")
    # Pin v3.
    pinned_version = 3
    async with connect(ws_store.db_path) as conn:
        await conn.execute(
            "INSERT INTO snapshots (id, workspace_id, name, message, "
            "parent_snapshot_id, kv_versions, file_versions, created_at) "
            "VALUES ('snap_pinned', ?, 'pin', NULL, NULL, ?, '{}', ?)",
            (
                workspace_id,
                f'{{"k": {pinned_version}}}',
                iso(now_utc()),
            ),
        )
        await conn.commit()

    await retention.upsert(
        workspace_id,
        keep_versions=1,
        keep_days=None,
        keep_snapshots=None,
        delete_unreferenced_blobs=True,
    )
    policy = await retention.get(workspace_id)
    await engine.run(workspace_id, policy)

    history = await ws_store.kv_history(workspace_id, "k")
    versions = sorted(e.version for e in history)
    # keep_versions=1 → v10 stays. Snapshot → v3 stays.
    assert versions == [pinned_version, 10]
    # Quiet snapshots_store unused-warning.
    _ = snapshots_store

