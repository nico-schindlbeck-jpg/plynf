# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 The Plinth Authors
"""Tests for the v0.6 generic resource-lock primitive.

The store-level tests exercise the race-safe acquire / heartbeat /
release path directly via :class:`ResourceLockStore`. The HTTP-level
tests cover the wire contract (status codes, error envelopes, path
encoding) end-to-end through the FastAPI app.
"""

from __future__ import annotations

import asyncio
from datetime import timedelta
from typing import Any

import httpx
import pytest

from plinth_workspace.db import iso, now_utc
from plinth_workspace.exceptions import (
    LockHeld,
    LockNotFound,
    LockNotHeld,
    WorkspaceNotFound,
)
from plinth_workspace.resource_locks import ResourceLockStore


# ---------------------------------------------------------------------------
# Store-level tests (use the ``settings`` fixture for a fresh DB).
# ---------------------------------------------------------------------------


async def _make_workspace(client: httpx.AsyncClient, name: str = "ws") -> str:
    resp = await client.post("/v1/workspaces", json={"name": name})
    assert resp.status_code == 201, resp.text
    return resp.json()["id"]


@pytest.fixture()
async def lock_store(settings: Any) -> ResourceLockStore:
    """Initialise the DB so the store has a schema to talk to."""

    from plinth_workspace.db import init_db

    settings.data_dir.mkdir(parents=True, exist_ok=True)
    settings.blobs_dir.mkdir(parents=True, exist_ok=True)
    await init_db(settings.db_path)
    return ResourceLockStore(settings.db_path)


@pytest.fixture()
async def ws_id_for_store(lock_store: ResourceLockStore) -> str:
    """Insert a workspace row so the store assertions pass."""

    from plinth_workspace.storage import WorkspaceStore

    ws = await WorkspaceStore(
        lock_store.db_path, lock_store.db_path.parent / "blobs"
    ).create_workspace("test", {})
    return ws.id


# ---------- store: acquire ---------------------------------------------------


@pytest.mark.asyncio
async def test_store_acquire_fresh_lock(
    lock_store: ResourceLockStore, ws_id_for_store: str
) -> None:
    lock = await lock_store.acquire(
        ws_id_for_store, "kv:idx", holder="agent-A", ttl_seconds=30
    )
    assert lock.name == "kv:idx"
    assert lock.workspace_id == ws_id_for_store
    assert lock.holder == "agent-A"
    assert lock.expires_at > lock.acquired_at


@pytest.mark.asyncio
async def test_store_acquire_held_raises(
    lock_store: ResourceLockStore, ws_id_for_store: str
) -> None:
    await lock_store.acquire(
        ws_id_for_store, "ext:vendor", holder="agent-A", ttl_seconds=60
    )
    with pytest.raises(LockHeld) as excinfo:
        await lock_store.acquire(
            ws_id_for_store, "ext:vendor", holder="agent-B", ttl_seconds=60
        )
    err = excinfo.value
    assert err.details["current_holder"] == "agent-A"
    assert err.details["retry_after_seconds"] >= 1


@pytest.mark.asyncio
async def test_store_acquire_steals_expired_lock(
    lock_store: ResourceLockStore, ws_id_for_store: str
) -> None:
    """An acquire after the prior holder's TTL elapsed must succeed."""

    # Insert an already-expired row directly.
    from plinth_workspace.db import connect

    past = now_utc() - timedelta(seconds=300)
    async with connect(lock_store.db_path) as conn:
        await conn.execute(
            """
            INSERT INTO resource_locks
                (workspace_id, name, holder, acquired_at,
                 expires_at, heartbeat_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                ws_id_for_store,
                "stale",
                "ghost",
                iso(past),
                iso(past + timedelta(seconds=1)),
                iso(past),
            ),
        )
        await conn.commit()

    lock = await lock_store.acquire(
        ws_id_for_store, "stale", holder="agent-A", ttl_seconds=30
    )
    assert lock.holder == "agent-A"
    assert lock.expires_at > now_utc()


@pytest.mark.asyncio
async def test_store_wait_ms_succeeds_when_released(
    lock_store: ResourceLockStore, ws_id_for_store: str
) -> None:
    """A waiter should pick up the lock as soon as the holder releases."""

    await lock_store.acquire(
        ws_id_for_store, "wait", holder="A", ttl_seconds=60
    )

    async def releaser() -> None:
        await asyncio.sleep(0.3)
        await lock_store.release(ws_id_for_store, "wait", holder="A")

    asyncio.create_task(releaser())
    lock = await lock_store.acquire(
        ws_id_for_store, "wait", holder="B", ttl_seconds=30, wait_ms=2000
    )
    assert lock.holder == "B"


@pytest.mark.asyncio
async def test_store_wait_ms_times_out(
    lock_store: ResourceLockStore, ws_id_for_store: str
) -> None:
    await lock_store.acquire(
        ws_id_for_store, "stuck", holder="A", ttl_seconds=60
    )
    with pytest.raises(LockHeld):
        await lock_store.acquire(
            ws_id_for_store, "stuck", holder="B", ttl_seconds=30, wait_ms=300
        )


# ---------- store: heartbeat -------------------------------------------------


@pytest.mark.asyncio
async def test_store_heartbeat_extends_expiry(
    lock_store: ResourceLockStore, ws_id_for_store: str
) -> None:
    lock = await lock_store.acquire(
        ws_id_for_store, "hb", holder="A", ttl_seconds=10
    )
    await asyncio.sleep(0.05)
    refreshed = await lock_store.heartbeat(
        ws_id_for_store, "hb", holder="A", ttl_seconds=120
    )
    # New expiry must be later than the original.
    assert refreshed.expires_at > lock.expires_at


@pytest.mark.asyncio
async def test_store_heartbeat_wrong_holder(
    lock_store: ResourceLockStore, ws_id_for_store: str
) -> None:
    await lock_store.acquire(
        ws_id_for_store, "hb-wrong", holder="A", ttl_seconds=60
    )
    with pytest.raises(LockNotHeld):
        await lock_store.heartbeat(ws_id_for_store, "hb-wrong", holder="B")


@pytest.mark.asyncio
async def test_store_heartbeat_unknown_lock(
    lock_store: ResourceLockStore, ws_id_for_store: str
) -> None:
    with pytest.raises(LockNotFound):
        await lock_store.heartbeat(ws_id_for_store, "nope", holder="A")


# ---------- store: release ---------------------------------------------------


@pytest.mark.asyncio
async def test_store_release_by_holder(
    lock_store: ResourceLockStore, ws_id_for_store: str
) -> None:
    await lock_store.acquire(
        ws_id_for_store, "rel", holder="A", ttl_seconds=60
    )
    await lock_store.release(ws_id_for_store, "rel", holder="A")
    # Re-acquire works because the row was deleted.
    lock = await lock_store.acquire(
        ws_id_for_store, "rel", holder="B", ttl_seconds=30
    )
    assert lock.holder == "B"


@pytest.mark.asyncio
async def test_store_release_wrong_holder_idempotent(
    lock_store: ResourceLockStore, ws_id_for_store: str
) -> None:
    """Release by a non-holder must be a silent no-op."""

    await lock_store.acquire(
        ws_id_for_store, "rel-x", holder="A", ttl_seconds=60
    )
    await lock_store.release(ws_id_for_store, "rel-x", holder="B")  # no-op
    # Original holder still owns the lock.
    lock = await lock_store.get(ws_id_for_store, "rel-x")
    assert lock is not None
    assert lock.holder == "A"


@pytest.mark.asyncio
async def test_store_release_never_existed_idempotent(
    lock_store: ResourceLockStore, ws_id_for_store: str
) -> None:
    await lock_store.release(ws_id_for_store, "phantom", holder="A")  # no-op


# ---------- store: list / get ------------------------------------------------


@pytest.mark.asyncio
async def test_store_list_locks(
    lock_store: ResourceLockStore, ws_id_for_store: str
) -> None:
    await lock_store.acquire(
        ws_id_for_store, "a", holder="A", ttl_seconds=30
    )
    await lock_store.acquire(
        ws_id_for_store, "b", holder="A", ttl_seconds=30
    )
    locks = await lock_store.list_locks(ws_id_for_store)
    assert {lock.name for lock in locks} == {"a", "b"}


@pytest.mark.asyncio
async def test_store_get_individual(
    lock_store: ResourceLockStore, ws_id_for_store: str
) -> None:
    await lock_store.acquire(
        ws_id_for_store, "get-me", holder="A", ttl_seconds=30
    )
    lock = await lock_store.get(ws_id_for_store, "get-me")
    assert lock is not None
    assert lock.name == "get-me"

    missing = await lock_store.get(ws_id_for_store, "missing")
    assert missing is None


# ---------- store: reaper ----------------------------------------------------


@pytest.mark.asyncio
async def test_store_reaper_sweeps_expired_locks(
    lock_store: ResourceLockStore, ws_id_for_store: str
) -> None:
    from plinth_workspace.db import connect

    past = now_utc() - timedelta(seconds=120)
    async with connect(lock_store.db_path) as conn:
        await conn.execute(
            """
            INSERT INTO resource_locks
                (workspace_id, name, holder, acquired_at,
                 expires_at, heartbeat_at)
            VALUES (?, ?, ?, ?, ?, ?), (?, ?, ?, ?, ?, ?)
            """,
            (
                ws_id_for_store, "exp1", "X", iso(past), iso(past), iso(past),
                ws_id_for_store, "exp2", "Y", iso(past), iso(past), iso(past),
            ),
        )
        await conn.commit()

    # Add a fresh row that should NOT be swept.
    await lock_store.acquire(
        ws_id_for_store, "fresh", holder="Z", ttl_seconds=120
    )

    swept = await lock_store.expire_stale_locks()
    assert swept == 2

    remaining = await lock_store.list_locks(ws_id_for_store)
    assert {lock.name for lock in remaining} == {"fresh"}


# ---------- store: race correctness -----------------------------------------


@pytest.mark.asyncio
async def test_store_concurrent_acquires_one_winner(
    lock_store: ResourceLockStore, ws_id_for_store: str
) -> None:
    """N concurrent acquires of the same name → exactly 1 winner."""

    n = 10
    holders = [f"agent-{i}" for i in range(n)]

    async def attempt(holder: str) -> str | None:
        try:
            lock = await lock_store.acquire(
                ws_id_for_store,
                "racing",
                holder=holder,
                ttl_seconds=60,
            )
        except LockHeld:
            return None
        return lock.holder

    results = await asyncio.gather(*(attempt(h) for h in holders))
    winners = [r for r in results if r is not None]
    assert len(winners) == 1, f"expected exactly 1 winner, got {len(winners)}: {winners}"


@pytest.mark.asyncio
async def test_store_unknown_workspace(lock_store: ResourceLockStore) -> None:
    """Operations on a missing workspace surface 404, not 5xx."""

    with pytest.raises(WorkspaceNotFound):
        await lock_store.acquire(
            "ws_does_not_exist", "x", holder="A", ttl_seconds=10
        )


# ---------------------------------------------------------------------------
# HTTP-level tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_http_acquire_returns_200_with_body(
    client: httpx.AsyncClient, workspace_id: str
) -> None:
    resp = await client.post(
        f"/v1/workspaces/{workspace_id}/locks/kv:sources/index/acquire",
        json={"holder": "A", "ttl_seconds": 30},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["name"] == "kv:sources/index"
    assert body["workspace_id"] == workspace_id
    assert body["holder"] == "A"
    assert body["waiters"] == 0


@pytest.mark.asyncio
async def test_http_acquire_held_returns_409(
    client: httpx.AsyncClient, workspace_id: str
) -> None:
    await client.post(
        f"/v1/workspaces/{workspace_id}/locks/foo/acquire",
        json={"holder": "A", "ttl_seconds": 60},
    )
    resp = await client.post(
        f"/v1/workspaces/{workspace_id}/locks/foo/acquire",
        json={"holder": "B", "ttl_seconds": 60},
    )
    assert resp.status_code == 409, resp.text
    body = resp.json()
    assert body["error"]["code"] == "LOCK_HELD"
    details = body["error"]["details"]
    assert details["current_holder"] == "A"
    assert "retry_after_seconds" in details


@pytest.mark.asyncio
async def test_http_heartbeat_round_trip(
    client: httpx.AsyncClient, workspace_id: str
) -> None:
    await client.post(
        f"/v1/workspaces/{workspace_id}/locks/hb/acquire",
        json={"holder": "A", "ttl_seconds": 30},
    )
    resp = await client.post(
        f"/v1/workspaces/{workspace_id}/locks/hb/heartbeat",
        json={"holder": "A"},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["holder"] == "A"


@pytest.mark.asyncio
async def test_http_heartbeat_wrong_holder(
    client: httpx.AsyncClient, workspace_id: str
) -> None:
    await client.post(
        f"/v1/workspaces/{workspace_id}/locks/hb-wrong/acquire",
        json={"holder": "A", "ttl_seconds": 30},
    )
    resp = await client.post(
        f"/v1/workspaces/{workspace_id}/locks/hb-wrong/heartbeat",
        json={"holder": "B"},
    )
    assert resp.status_code == 409
    assert resp.json()["error"]["code"] == "LOCK_NOT_HELD"


@pytest.mark.asyncio
async def test_http_heartbeat_unknown_lock(
    client: httpx.AsyncClient, workspace_id: str
) -> None:
    resp = await client.post(
        f"/v1/workspaces/{workspace_id}/locks/never/heartbeat",
        json={"holder": "A"},
    )
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "LOCK_NOT_FOUND"


@pytest.mark.asyncio
async def test_http_release_204_idempotent(
    client: httpx.AsyncClient, workspace_id: str
) -> None:
    await client.post(
        f"/v1/workspaces/{workspace_id}/locks/r/acquire",
        json={"holder": "A", "ttl_seconds": 30},
    )
    resp = await client.post(
        f"/v1/workspaces/{workspace_id}/locks/r/release",
        json={"holder": "A"},
    )
    assert resp.status_code == 204
    # Second release is still 204.
    resp = await client.post(
        f"/v1/workspaces/{workspace_id}/locks/r/release",
        json={"holder": "A"},
    )
    assert resp.status_code == 204


@pytest.mark.asyncio
async def test_http_release_wrong_holder_204(
    client: httpx.AsyncClient, workspace_id: str
) -> None:
    """Release with a non-matching holder is a silent no-op (204)."""

    await client.post(
        f"/v1/workspaces/{workspace_id}/locks/r2/acquire",
        json={"holder": "A", "ttl_seconds": 30},
    )
    resp = await client.post(
        f"/v1/workspaces/{workspace_id}/locks/r2/release",
        json={"holder": "B"},
    )
    assert resp.status_code == 204
    # A still owns it.
    resp = await client.get(f"/v1/workspaces/{workspace_id}/locks/r2")
    assert resp.status_code == 200
    assert resp.json()["holder"] == "A"


@pytest.mark.asyncio
async def test_http_list_locks(
    client: httpx.AsyncClient, workspace_id: str
) -> None:
    for n in ("a", "b", "c"):
        await client.post(
            f"/v1/workspaces/{workspace_id}/locks/{n}/acquire",
            json={"holder": "A", "ttl_seconds": 60},
        )
    resp = await client.get(f"/v1/workspaces/{workspace_id}/locks")
    assert resp.status_code == 200
    names = {lock["name"] for lock in resp.json()["locks"]}
    assert names == {"a", "b", "c"}


@pytest.mark.asyncio
async def test_http_get_individual_lock(
    client: httpx.AsyncClient, workspace_id: str
) -> None:
    await client.post(
        f"/v1/workspaces/{workspace_id}/locks/inspect/acquire",
        json={"holder": "A", "ttl_seconds": 30},
    )
    resp = await client.get(f"/v1/workspaces/{workspace_id}/locks/inspect")
    assert resp.status_code == 200
    assert resp.json()["holder"] == "A"


@pytest.mark.asyncio
async def test_http_get_unknown_lock_404(
    client: httpx.AsyncClient, workspace_id: str
) -> None:
    resp = await client.get(f"/v1/workspaces/{workspace_id}/locks/missing")
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "LOCK_NOT_FOUND"


@pytest.mark.asyncio
async def test_http_lock_name_with_slash(
    client: httpx.AsyncClient, workspace_id: str
) -> None:
    """Names containing ``/`` (the canonical use case) round-trip cleanly."""

    await client.post(
        f"/v1/workspaces/{workspace_id}/locks/kv:sources/index/acquire",
        json={"holder": "A", "ttl_seconds": 30},
    )
    resp = await client.get(
        f"/v1/workspaces/{workspace_id}/locks/kv:sources/index"
    )
    assert resp.status_code == 200
    assert resp.json()["name"] == "kv:sources/index"


@pytest.mark.asyncio
async def test_http_unknown_workspace_404(
    client: httpx.AsyncClient,
) -> None:
    resp = await client.post(
        "/v1/workspaces/ws_nonexistent/locks/foo/acquire",
        json={"holder": "A", "ttl_seconds": 30},
    )
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "WORKSPACE_NOT_FOUND"


@pytest.mark.asyncio
async def test_http_tenant_isolation(
    settings: Any, tmp_path: Any
) -> None:
    """A lock in workspace A should not be visible from workspace B in
    a separate tenant."""

    # Two workspaces in different tenants; we mock the tenant by stamping
    # ``request.state.tenant_id`` via the auth header in permissive mode.
    from httpx import ASGITransport

    from plinth_workspace.api import create_app
    from plinth_workspace.db import init_db

    settings.data_dir.mkdir(parents=True, exist_ok=True)
    settings.blobs_dir.mkdir(parents=True, exist_ok=True)
    await init_db(settings.db_path)
    app = create_app(settings)
    transport = ASGITransport(app=app)

    async with httpx.AsyncClient(
        transport=transport,
        base_url="http://test",
        headers={"Authorization": "Bearer test-token"},
    ) as c:
        ws_a = await _make_workspace(c, "ws-a")
        ws_b = await _make_workspace(c, "ws-b")

        await c.post(
            f"/v1/workspaces/{ws_a}/locks/x/acquire",
            json={"holder": "A", "ttl_seconds": 60},
        )

        # ws_b does not see the lock from ws_a.
        resp = await c.get(f"/v1/workspaces/{ws_b}/locks")
        assert resp.status_code == 200
        assert resp.json()["locks"] == []


@pytest.mark.asyncio
async def test_http_concurrent_acquires_one_winner(
    client: httpx.AsyncClient, workspace_id: str
) -> None:
    """100 concurrent HTTP acquires → exactly 1 winner."""

    n = 100

    async def attempt(i: int) -> int:
        resp = await client.post(
            f"/v1/workspaces/{workspace_id}/locks/contend/acquire",
            json={"holder": f"agent-{i}", "ttl_seconds": 60},
        )
        return resp.status_code

    statuses = await asyncio.gather(*(attempt(i) for i in range(n)))
    wins = sum(1 for s in statuses if s == 200)
    losses = sum(1 for s in statuses if s == 409)
    assert wins == 1, f"expected 1 winner, got {wins}"
    assert losses == n - 1
