# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 The Plinth Authors
"""Tests for the v0.5 durable-workflow executor primitives.

Covers:

* Worker registration / heartbeat / drain
* Lease acquire / heartbeat / release semantics
* Lease conflict (409) when an active lease exists
* Lease reaper expiring stale leases + reverting steps
* Concurrent lease attempts: exactly one wins
* HTTP endpoints round-tripping through the FastAPI app
"""

from __future__ import annotations

import asyncio
from datetime import timedelta

import httpx
import pytest

from plinth_workspace.db import iso, now_utc
from plinth_workspace.leases import LeaseStore


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _new_workflow(
    client: httpx.AsyncClient, workspace_id: str, steps: list[str]
) -> str:
    resp = await client.post(
        f"/v1/workspaces/{workspace_id}/workflows",
        json={"name": "wf", "steps": steps},
    )
    assert resp.status_code == 201, resp.text
    return resp.json()["id"]


async def _new_pending_step(
    client: httpx.AsyncClient,
    workspace_id: str,
    workflow_id: str,
    name: str,
) -> str:
    """Create a step row in the v0.5 lifecycle (pending).

    The v0.2 ``POST /steps`` endpoint puts a step directly into ``running`` —
    that's the legacy in-process flow. For the durable executor we want the
    step in ``pending`` so a worker can acquire a lease on it. We do that by
    creating it then patching its status back via the workflows API.
    """

    create = await client.post(
        f"/v1/workspaces/{workspace_id}/workflows/{workflow_id}/steps",
        json={"name": name},
    )
    assert create.status_code == 201, create.text
    step_id = create.json()["id"]

    # Manually flip back to pending via the SQL store. The HTTP layer does
    # not (and should not) expose a "status=pending" transition because it
    # only makes sense for the lease subsystem.
    from plinth_workspace.db import connect

    settings = client.app.state.settings if hasattr(client, "app") else None
    db_path = settings.db_path if settings else None
    if db_path is None:
        # Best-effort fallback used when the fixture wires through ASGI.
        from plinth_workspace.api import _app  # noqa: F401

    # Use the running app's settings via ASGI.
    db_path = client._transport.app.state.settings.db_path  # type: ignore[attr-defined]
    async with connect(db_path) as conn:
        await conn.execute(
            "UPDATE workflow_steps SET status='pending', started_at=NULL "
            "WHERE id=?",
            (step_id,),
        )
        await conn.commit()
    return step_id


# ---------------------------------------------------------------------------
# Worker register / heartbeat / drain
# ---------------------------------------------------------------------------


async def test_register_worker(client: httpx.AsyncClient) -> None:
    resp = await client.post(
        "/v1/workers/register",
        json={"hostname": "agent-1", "pid": 4242},
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["id"].startswith("worker_")
    assert body["hostname"] == "agent-1"
    assert body["pid"] == 4242
    assert body["status"] == "active"
    assert body["started_at"]
    assert body["last_heartbeat_at"]


async def test_register_worker_minimal_body(client: httpx.AsyncClient) -> None:
    resp = await client.post("/v1/workers/register", json={})
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["hostname"] is None
    assert body["pid"] is None


async def test_worker_heartbeat(client: httpx.AsyncClient) -> None:
    reg = await client.post("/v1/workers/register", json={"hostname": "h"})
    worker_id = reg.json()["id"]
    resp = await client.post(f"/v1/workers/{worker_id}/heartbeat")
    assert resp.status_code == 200, resp.text
    assert resp.json()["id"] == worker_id


async def test_worker_heartbeat_404(client: httpx.AsyncClient) -> None:
    resp = await client.post("/v1/workers/worker_unknown/heartbeat")
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "WORKER_NOT_FOUND"


async def test_worker_drain(client: httpx.AsyncClient) -> None:
    reg = await client.post("/v1/workers/register", json={})
    worker_id = reg.json()["id"]
    resp = await client.post(f"/v1/workers/{worker_id}/drain")
    assert resp.status_code == 200
    assert resp.json()["status"] == "draining"


async def test_workers_list(client: httpx.AsyncClient) -> None:
    for hostname in ["a", "b", "c"]:
        await client.post("/v1/workers/register", json={"hostname": hostname})
    resp = await client.get("/v1/workers")
    assert resp.status_code == 200
    workers = resp.json()["workers"]
    assert len(workers) >= 3
    assert any(w["hostname"] == "a" for w in workers)


async def test_workers_list_status_filter(client: httpx.AsyncClient) -> None:
    a = (await client.post("/v1/workers/register", json={})).json()["id"]
    await client.post("/v1/workers/register", json={})
    await client.post(f"/v1/workers/{a}/drain")

    drained = (await client.get("/v1/workers", params={"status": "draining"})).json()
    assert all(w["status"] == "draining" for w in drained["workers"])
    active = (await client.get("/v1/workers", params={"status": "active"})).json()
    assert all(w["status"] == "active" for w in active["workers"])


# ---------------------------------------------------------------------------
# Lease acquire
# ---------------------------------------------------------------------------


async def test_acquire_lease_on_pending_step(
    client: httpx.AsyncClient, workspace_id: str
) -> None:
    wf_id = await _new_workflow(client, workspace_id, ["a"])
    step_id = await _new_pending_step(client, workspace_id, wf_id, "a")
    worker = (await client.post("/v1/workers/register", json={})).json()

    resp = await client.post(
        f"/v1/workspaces/{workspace_id}/workflows/{wf_id}/steps/{step_id}/lease",
        json={"worker_id": worker["id"], "ttl_seconds": 60},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["step_id"] == step_id
    assert body["worker_id"] == worker["id"]
    assert body["status"] == "running"
    assert body["expires_at"] > body["acquired_at"]

    # Step status flipped to running.
    step = (await client.get(
        f"/v1/workspaces/{workspace_id}/workflows/{wf_id}"
    )).json()["steps"][0]
    assert step["status"] == "running"


async def test_acquire_lease_fails_if_step_already_running(
    client: httpx.AsyncClient, workspace_id: str
) -> None:
    wf_id = await _new_workflow(client, workspace_id, ["a"])
    # Default v0.2 path leaves the step in ``running`` immediately.
    step = (await client.post(
        f"/v1/workspaces/{workspace_id}/workflows/{wf_id}/steps",
        json={"name": "a"},
    )).json()
    worker = (await client.post("/v1/workers/register", json={})).json()
    resp = await client.post(
        f"/v1/workspaces/{workspace_id}/workflows/{wf_id}/steps/{step['id']}/lease",
        json={"worker_id": worker["id"], "ttl_seconds": 60},
    )
    assert resp.status_code == 409
    assert resp.json()["error"]["code"] == "LEASE_CONFLICT"


async def test_acquire_lease_conflict_when_active_lease_exists(
    client: httpx.AsyncClient, workspace_id: str
) -> None:
    wf_id = await _new_workflow(client, workspace_id, ["a"])
    step_id = await _new_pending_step(client, workspace_id, wf_id, "a")
    a = (await client.post("/v1/workers/register", json={})).json()["id"]
    b = (await client.post("/v1/workers/register", json={})).json()["id"]

    first = await client.post(
        f"/v1/workspaces/{workspace_id}/workflows/{wf_id}/steps/{step_id}/lease",
        json={"worker_id": a, "ttl_seconds": 60},
    )
    assert first.status_code == 200

    second = await client.post(
        f"/v1/workspaces/{workspace_id}/workflows/{wf_id}/steps/{step_id}/lease",
        json={"worker_id": b, "ttl_seconds": 60},
    )
    assert second.status_code == 409
    assert second.json()["error"]["code"] == "LEASE_CONFLICT"


async def test_acquire_lease_succeeds_after_expiry(
    settings, client: httpx.AsyncClient, workspace_id: str
) -> None:
    """If the existing lease is expired, a new acquire wins."""

    wf_id = await _new_workflow(client, workspace_id, ["a"])
    step_id = await _new_pending_step(client, workspace_id, wf_id, "a")

    # Acquire directly via the store so we can fabricate an old expires_at.
    store = LeaseStore(settings.db_path)
    a = await store.register_worker()
    lease = await store.acquire_lease(
        workspace_id, wf_id, step_id, worker_id=a.id, ttl_seconds=60
    )
    # Backdate the lease to simulate expiry.
    from plinth_workspace.db import connect

    async with connect(settings.db_path) as conn:
        await conn.execute(
            "UPDATE workflow_step_leases SET expires_at=? WHERE step_id=?",
            (iso(now_utc() - timedelta(seconds=10)), step_id),
        )
        # Also revert step back to pending (simulating the reaper running).
        await conn.execute(
            "UPDATE workflow_steps SET status='pending' WHERE id=?",
            (step_id,),
        )
        await conn.commit()

    b = await store.register_worker()
    new_lease = await store.acquire_lease(
        workspace_id, wf_id, step_id, worker_id=b.id, ttl_seconds=60
    )
    assert new_lease.worker_id == b.id
    assert new_lease.status == "running"
    # Previous lease overwritten — not present as a separate row.
    assert new_lease.step_id == lease.step_id


# ---------------------------------------------------------------------------
# Heartbeat
# ---------------------------------------------------------------------------


async def test_heartbeat_extends_expires_at(
    client: httpx.AsyncClient, workspace_id: str
) -> None:
    wf_id = await _new_workflow(client, workspace_id, ["a"])
    step_id = await _new_pending_step(client, workspace_id, wf_id, "a")
    worker = (await client.post("/v1/workers/register", json={})).json()

    first = (await client.post(
        f"/v1/workspaces/{workspace_id}/workflows/{wf_id}/steps/{step_id}/lease",
        json={"worker_id": worker["id"], "ttl_seconds": 60},
    )).json()
    await asyncio.sleep(0.01)
    hb = await client.post(
        f"/v1/workspaces/{workspace_id}/workflows/{wf_id}/steps/{step_id}/heartbeat",
        json={"worker_id": worker["id"]},
    )
    assert hb.status_code == 200, hb.text
    body = hb.json()
    assert body["expires_at"] > first["expires_at"]
    assert body["heartbeat_at"] > first["heartbeat_at"]


async def test_heartbeat_with_explicit_ttl(
    client: httpx.AsyncClient, workspace_id: str
) -> None:
    wf_id = await _new_workflow(client, workspace_id, ["a"])
    step_id = await _new_pending_step(client, workspace_id, wf_id, "a")
    worker = (await client.post("/v1/workers/register", json={})).json()

    await client.post(
        f"/v1/workspaces/{workspace_id}/workflows/{wf_id}/steps/{step_id}/lease",
        json={"worker_id": worker["id"], "ttl_seconds": 30},
    )
    hb = await client.post(
        f"/v1/workspaces/{workspace_id}/workflows/{wf_id}/steps/{step_id}/heartbeat",
        json={"worker_id": worker["id"], "ttl_seconds": 120},
    )
    assert hb.status_code == 200
    # TTL bumped to 120s — expires_at should be ~120s after heartbeat_at.
    body = hb.json()
    from datetime import datetime

    # FastAPI/Pydantic serialises datetimes with ``Z`` rather than ``+00:00``;
    # ``fromisoformat`` only learned to accept ``Z`` in Python 3.11. Patch
    # the suffix so this test works on the workspace's 3.11+ baseline AND
    # the older runner the verify step uses.
    def _parse(value: str) -> datetime:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))

    hb_at = _parse(body["heartbeat_at"])
    exp_at = _parse(body["expires_at"])
    delta = (exp_at - hb_at).total_seconds()
    assert 100 < delta < 140  # tolerate clock skew


async def test_heartbeat_404_when_no_lease(
    client: httpx.AsyncClient, workspace_id: str
) -> None:
    wf_id = await _new_workflow(client, workspace_id, ["a"])
    step_id = await _new_pending_step(client, workspace_id, wf_id, "a")
    worker = (await client.post("/v1/workers/register", json={})).json()

    resp = await client.post(
        f"/v1/workspaces/{workspace_id}/workflows/{wf_id}/steps/{step_id}/heartbeat",
        json={"worker_id": worker["id"]},
    )
    assert resp.status_code == 409
    assert resp.json()["error"]["code"] == "LEASE_NOT_HELD"


async def test_heartbeat_wrong_worker_rejected(
    client: httpx.AsyncClient, workspace_id: str
) -> None:
    wf_id = await _new_workflow(client, workspace_id, ["a"])
    step_id = await _new_pending_step(client, workspace_id, wf_id, "a")
    a = (await client.post("/v1/workers/register", json={})).json()["id"]
    b = (await client.post("/v1/workers/register", json={})).json()["id"]

    await client.post(
        f"/v1/workspaces/{workspace_id}/workflows/{wf_id}/steps/{step_id}/lease",
        json={"worker_id": a, "ttl_seconds": 60},
    )
    resp = await client.post(
        f"/v1/workspaces/{workspace_id}/workflows/{wf_id}/steps/{step_id}/heartbeat",
        json={"worker_id": b},
    )
    assert resp.status_code == 409
    assert resp.json()["error"]["code"] == "LEASE_NOT_HELD"


# ---------------------------------------------------------------------------
# Release
# ---------------------------------------------------------------------------


async def test_release_marks_lease_released_and_step_completed(
    client: httpx.AsyncClient, workspace_id: str
) -> None:
    wf_id = await _new_workflow(client, workspace_id, ["a"])
    step_id = await _new_pending_step(client, workspace_id, wf_id, "a")
    worker = (await client.post("/v1/workers/register", json={})).json()

    await client.post(
        f"/v1/workspaces/{workspace_id}/workflows/{wf_id}/steps/{step_id}/lease",
        json={"worker_id": worker["id"], "ttl_seconds": 60},
    )
    resp = await client.post(
        f"/v1/workspaces/{workspace_id}/workflows/{wf_id}/steps/{step_id}/release",
        json={
            "worker_id": worker["id"],
            "status": "completed",
            "output": {"result": 42},
            "snapshot_id": "snap_x",
        },
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "released"

    wf = (await client.get(
        f"/v1/workspaces/{workspace_id}/workflows/{wf_id}"
    )).json()
    [step] = wf["steps"]
    assert step["status"] == "completed"
    assert step["output"] == {"result": 42}
    assert step["snapshot_id"] == "snap_x"
    assert wf["status"] == "completed"


async def test_release_with_failed_status(
    client: httpx.AsyncClient, workspace_id: str
) -> None:
    wf_id = await _new_workflow(client, workspace_id, ["a", "b"])
    step_id = await _new_pending_step(client, workspace_id, wf_id, "a")
    worker = (await client.post("/v1/workers/register", json={})).json()

    await client.post(
        f"/v1/workspaces/{workspace_id}/workflows/{wf_id}/steps/{step_id}/lease",
        json={"worker_id": worker["id"], "ttl_seconds": 60},
    )
    resp = await client.post(
        f"/v1/workspaces/{workspace_id}/workflows/{wf_id}/steps/{step_id}/release",
        json={"worker_id": worker["id"], "status": "failed", "error": "boom"},
    )
    assert resp.status_code == 200
    wf = (await client.get(
        f"/v1/workspaces/{workspace_id}/workflows/{wf_id}"
    )).json()
    [step] = wf["steps"]
    assert step["status"] == "failed"
    assert step["error"] == "boom"
    assert wf["status"] == "failed"


async def test_release_pending_requeues_step(
    client: httpx.AsyncClient, workspace_id: str
) -> None:
    """Releasing with status='pending' returns the step to the pool."""

    wf_id = await _new_workflow(client, workspace_id, ["a"])
    step_id = await _new_pending_step(client, workspace_id, wf_id, "a")
    worker = (await client.post("/v1/workers/register", json={})).json()

    await client.post(
        f"/v1/workspaces/{workspace_id}/workflows/{wf_id}/steps/{step_id}/lease",
        json={"worker_id": worker["id"], "ttl_seconds": 60},
    )
    resp = await client.post(
        f"/v1/workspaces/{workspace_id}/workflows/{wf_id}/steps/{step_id}/release",
        json={"worker_id": worker["id"], "status": "pending"},
    )
    assert resp.status_code == 200
    pending = (await client.get(
        f"/v1/workspaces/{workspace_id}/workflows/{wf_id}/pending"
    )).json()
    assert any(s["id"] == step_id for s in pending["steps"])


async def test_release_wrong_worker_rejected(
    client: httpx.AsyncClient, workspace_id: str
) -> None:
    wf_id = await _new_workflow(client, workspace_id, ["a"])
    step_id = await _new_pending_step(client, workspace_id, wf_id, "a")
    a = (await client.post("/v1/workers/register", json={})).json()["id"]
    b = (await client.post("/v1/workers/register", json={})).json()["id"]

    await client.post(
        f"/v1/workspaces/{workspace_id}/workflows/{wf_id}/steps/{step_id}/lease",
        json={"worker_id": a, "ttl_seconds": 60},
    )
    resp = await client.post(
        f"/v1/workspaces/{workspace_id}/workflows/{wf_id}/steps/{step_id}/release",
        json={"worker_id": b, "status": "completed"},
    )
    assert resp.status_code == 409


async def test_release_invalid_status(
    client: httpx.AsyncClient, workspace_id: str
) -> None:
    wf_id = await _new_workflow(client, workspace_id, ["a"])
    step_id = await _new_pending_step(client, workspace_id, wf_id, "a")
    worker = (await client.post("/v1/workers/register", json={})).json()

    await client.post(
        f"/v1/workspaces/{workspace_id}/workflows/{wf_id}/steps/{step_id}/lease",
        json={"worker_id": worker["id"], "ttl_seconds": 60},
    )
    resp = await client.post(
        f"/v1/workspaces/{workspace_id}/workflows/{wf_id}/steps/{step_id}/release",
        json={"worker_id": worker["id"], "status": "running"},
    )
    assert resp.status_code == 400


# ---------------------------------------------------------------------------
# Pending + expired listing
# ---------------------------------------------------------------------------


async def test_list_pending_steps(
    client: httpx.AsyncClient, workspace_id: str
) -> None:
    wf_id = await _new_workflow(client, workspace_id, ["a", "b"])
    a_id = await _new_pending_step(client, workspace_id, wf_id, "a")
    b_id = await _new_pending_step(client, workspace_id, wf_id, "b")
    pending = (await client.get(
        f"/v1/workspaces/{workspace_id}/workflows/{wf_id}/pending"
    )).json()["steps"]
    ids = {s["id"] for s in pending}
    assert {a_id, b_id} <= ids
    assert all(s["status"] == "pending" for s in pending)


async def test_list_expired_leases_empty_when_fresh(
    client: httpx.AsyncClient, workspace_id: str
) -> None:
    wf_id = await _new_workflow(client, workspace_id, ["a"])
    step_id = await _new_pending_step(client, workspace_id, wf_id, "a")
    worker = (await client.post("/v1/workers/register", json={})).json()
    await client.post(
        f"/v1/workspaces/{workspace_id}/workflows/{wf_id}/steps/{step_id}/lease",
        json={"worker_id": worker["id"], "ttl_seconds": 600},
    )
    expired = (await client.get(
        f"/v1/workspaces/{workspace_id}/workflows/{wf_id}/expired"
    )).json()
    assert expired["leases"] == []


# ---------------------------------------------------------------------------
# Reaper
# ---------------------------------------------------------------------------


async def test_reaper_expires_stale_leases(
    settings, store, snapshots
) -> None:
    """Backdate a lease and confirm the reaper flips it to expired + reverts step."""

    from plinth_workspace.db import connect
    from plinth_workspace.workflows import WorkflowStore

    workflow_store = WorkflowStore(settings.db_path)
    lease_store = LeaseStore(settings.db_path)

    ws = await store.create_workspace("reaper-test", {})
    wf = await workflow_store.create_workflow(ws.id, "wf", ["a"])
    step = await workflow_store.create_step(ws.id, wf.id, "a")

    # Manually flip step back to pending so we can lease it.
    async with connect(settings.db_path) as conn:
        await conn.execute(
            "UPDATE workflow_steps SET status='pending', started_at=NULL "
            "WHERE id=?",
            (step.id,),
        )
        await conn.commit()

    worker = await lease_store.register_worker()
    await lease_store.acquire_lease(
        ws.id, wf.id, step.id, worker_id=worker.id, ttl_seconds=60
    )

    # Backdate the lease so it's already past expiry.
    async with connect(settings.db_path) as conn:
        await conn.execute(
            "UPDATE workflow_step_leases SET expires_at=? WHERE step_id=?",
            (iso(now_utc() - timedelta(seconds=10)), step.id),
        )
        await conn.commit()

    expired = await lease_store.expire_stale_leases()
    assert expired == 1

    # Lease now expired AND step reverted to pending.
    async with connect(settings.db_path) as conn:
        cur = await conn.execute(
            "SELECT status FROM workflow_step_leases WHERE step_id=?",
            (step.id,),
        )
        row = await cur.fetchone()
        await cur.close()
        assert row["status"] == "expired"

        cur = await conn.execute(
            "SELECT status FROM workflow_steps WHERE id=?", (step.id,)
        )
        row = await cur.fetchone()
        await cur.close()
        assert row["status"] == "pending"


async def test_reaper_idempotent_when_no_stale_leases(
    settings, store
) -> None:
    lease_store = LeaseStore(settings.db_path)
    expired = await lease_store.expire_stale_leases()
    assert expired == 0


async def test_reaper_does_not_revert_completed_steps(
    settings, store
) -> None:
    """A race where the worker completed the step just before the reaper
    runs must not revert the step back to pending."""

    from plinth_workspace.db import connect
    from plinth_workspace.workflows import WorkflowStore

    workflow_store = WorkflowStore(settings.db_path)
    lease_store = LeaseStore(settings.db_path)

    ws = await store.create_workspace("reaper-race", {})
    wf = await workflow_store.create_workflow(ws.id, "wf", ["a"])
    step = await workflow_store.create_step(ws.id, wf.id, "a")
    async with connect(settings.db_path) as conn:
        await conn.execute(
            "UPDATE workflow_steps SET status='pending', started_at=NULL "
            "WHERE id=?",
            (step.id,),
        )
        await conn.commit()

    worker = await lease_store.register_worker()
    await lease_store.acquire_lease(
        ws.id, wf.id, step.id, worker_id=worker.id, ttl_seconds=60
    )
    # Backdate lease + simultaneously mark step completed.
    async with connect(settings.db_path) as conn:
        await conn.execute(
            "UPDATE workflow_step_leases SET expires_at=? WHERE step_id=?",
            (iso(now_utc() - timedelta(seconds=10)), step.id),
        )
        await conn.execute(
            "UPDATE workflow_steps SET status='completed' WHERE id=?",
            (step.id,),
        )
        await conn.commit()

    await lease_store.expire_stale_leases()

    async with connect(settings.db_path) as conn:
        cur = await conn.execute(
            "SELECT status FROM workflow_steps WHERE id=?", (step.id,)
        )
        row = await cur.fetchone()
        await cur.close()
        assert row["status"] == "completed"


async def test_reaper_marks_inactive_workers_gone(
    settings, store
) -> None:
    from plinth_workspace.db import connect

    lease_store = LeaseStore(settings.db_path)
    w = await lease_store.register_worker(hostname="zombie")
    # Backdate last_heartbeat_at so the worker is past the timeout.
    async with connect(settings.db_path) as conn:
        await conn.execute(
            "UPDATE workers SET last_heartbeat_at=? WHERE id=?",
            (iso(now_utc() - timedelta(seconds=600)), w.id),
        )
        await conn.commit()

    swept = await lease_store.mark_inactive_workers(timeout_seconds=300)
    assert swept == 1
    fetched = await lease_store.get_worker(w.id)
    assert fetched.status == "gone"


# ---------------------------------------------------------------------------
# Concurrent acquire — exactly one wins
# ---------------------------------------------------------------------------


async def test_concurrent_lease_attempts_exactly_one_wins(
    settings, store
) -> None:
    """Spec requirement: race-safe acquire.

    Spawning multiple ``acquire_lease`` coroutines on the same step
    concurrently — exactly one should succeed; the rest should get
    :class:`LeaseConflict`.
    """

    from plinth_workspace.db import connect
    from plinth_workspace.leases import LeaseConflict
    from plinth_workspace.workflows import WorkflowStore

    workflow_store = WorkflowStore(settings.db_path)
    lease_store = LeaseStore(settings.db_path)

    ws = await store.create_workspace("race", {})
    wf = await workflow_store.create_workflow(ws.id, "wf", ["a"])
    step = await workflow_store.create_step(ws.id, wf.id, "a")
    async with connect(settings.db_path) as conn:
        await conn.execute(
            "UPDATE workflow_steps SET status='pending', started_at=NULL "
            "WHERE id=?",
            (step.id,),
        )
        await conn.commit()

    workers = [await lease_store.register_worker() for _ in range(5)]

    async def attempt(worker_id: str):
        try:
            return await lease_store.acquire_lease(
                ws.id, wf.id, step.id, worker_id=worker_id, ttl_seconds=60
            )
        except LeaseConflict as exc:
            return exc

    results = await asyncio.gather(*(attempt(w.id) for w in workers))
    successes = [r for r in results if not isinstance(r, Exception)]
    failures = [r for r in results if isinstance(r, LeaseConflict)]
    assert len(successes) == 1
    assert len(failures) == len(workers) - 1


# ---------------------------------------------------------------------------
# Lease + workflow lifecycle integration
# ---------------------------------------------------------------------------


async def test_workflow_completes_after_all_leased_steps_release(
    client: httpx.AsyncClient, workspace_id: str
) -> None:
    wf_id = await _new_workflow(client, workspace_id, ["a", "b"])
    a_id = await _new_pending_step(client, workspace_id, wf_id, "a")
    b_id = await _new_pending_step(client, workspace_id, wf_id, "b")
    worker = (await client.post("/v1/workers/register", json={})).json()

    for step_id in (a_id, b_id):
        await client.post(
            f"/v1/workspaces/{workspace_id}/workflows/{wf_id}/steps/{step_id}/lease",
            json={"worker_id": worker["id"], "ttl_seconds": 60},
        )
        await client.post(
            f"/v1/workspaces/{workspace_id}/workflows/{wf_id}/steps/{step_id}/release",
            json={"worker_id": worker["id"], "status": "completed"},
        )

    wf = (await client.get(
        f"/v1/workspaces/{workspace_id}/workflows/{wf_id}"
    )).json()
    assert wf["status"] == "completed"


async def test_lease_404_for_unknown_workflow(
    client: httpx.AsyncClient, workspace_id: str
) -> None:
    worker = (await client.post("/v1/workers/register", json={})).json()
    resp = await client.post(
        f"/v1/workspaces/{workspace_id}/workflows/wf_nope/steps/step_nope/lease",
        json={"worker_id": worker["id"], "ttl_seconds": 60},
    )
    assert resp.status_code == 404


async def test_lease_404_for_unknown_step(
    client: httpx.AsyncClient, workspace_id: str
) -> None:
    wf_id = await _new_workflow(client, workspace_id, ["a"])
    worker = (await client.post("/v1/workers/register", json={})).json()
    resp = await client.post(
        f"/v1/workspaces/{workspace_id}/workflows/{wf_id}/steps/step_nope/lease",
        json={"worker_id": worker["id"], "ttl_seconds": 60},
    )
    assert resp.status_code == 404


async def test_lease_acquire_validates_ttl(
    client: httpx.AsyncClient, workspace_id: str
) -> None:
    wf_id = await _new_workflow(client, workspace_id, ["a"])
    step_id = await _new_pending_step(client, workspace_id, wf_id, "a")
    worker = (await client.post("/v1/workers/register", json={})).json()
    resp = await client.post(
        f"/v1/workspaces/{workspace_id}/workflows/{wf_id}/steps/{step_id}/lease",
        json={"worker_id": worker["id"], "ttl_seconds": 0},
    )
    assert resp.status_code == 400


# ---------------------------------------------------------------------------
# Direct LeaseStore usage (programmatic)
# ---------------------------------------------------------------------------


async def test_direct_acquire_release_via_store(settings, store) -> None:
    from plinth_workspace.db import connect
    from plinth_workspace.workflows import WorkflowStore

    workflow_store = WorkflowStore(settings.db_path)
    lease_store = LeaseStore(settings.db_path)
    ws = await store.create_workspace("direct", {})
    wf = await workflow_store.create_workflow(ws.id, "wf", ["a"])
    step = await workflow_store.create_step(ws.id, wf.id, "a")
    async with connect(settings.db_path) as conn:
        await conn.execute(
            "UPDATE workflow_steps SET status='pending', started_at=NULL "
            "WHERE id=?",
            (step.id,),
        )
        await conn.commit()

    w = await lease_store.register_worker()
    lease = await lease_store.acquire_lease(
        ws.id, wf.id, step.id, worker_id=w.id, ttl_seconds=60
    )
    assert lease.worker_id == w.id

    released = await lease_store.release_lease(
        ws.id, wf.id, step.id, worker_id=w.id, step_status="completed",
        output={"k": "v"},
    )
    assert released.status == "released"


async def test_lease_acquire_404_workflow(settings, store) -> None:
    from plinth_workspace.exceptions import WorkflowNotFound

    lease_store = LeaseStore(settings.db_path)
    ws = await store.create_workspace("x", {})
    w = await lease_store.register_worker()
    with pytest.raises(WorkflowNotFound):
        await lease_store.acquire_lease(
            ws.id, "wf_nope", "step_nope", worker_id=w.id, ttl_seconds=60
        )


# ---------------------------------------------------------------------------
# v1.3 — Cluster-coordinated leases
# ---------------------------------------------------------------------------


def _fake_redis_server():
    """Construct an in-memory ``fakeredis.FakeServer`` shared between
    multiple ``RedisBackend`` clients so they simulate distinct workspace
    replicas pointing at the same Redis cluster.
    """

    import fakeredis

    return fakeredis.FakeServer()


def _shared_redis_backend(server, *, key_prefix: str = "plinth-test"):
    import fakeredis.aioredis

    from plinth_workspace.coordination import RedisBackend

    client = fakeredis.aioredis.FakeRedis(server=server, decode_responses=True)
    return RedisBackend(
        "redis://x/0", key_prefix=key_prefix, client=client
    )


async def _create_cluster_workspace_with_pending_step(
    store, settings, ws_name: str
):
    """Create a workspace, workflow, and a step ready to be leased."""

    from plinth_workspace.db import connect
    from plinth_workspace.workflows import WorkflowStore

    workflow_store = WorkflowStore(settings.db_path)
    ws = await store.create_workspace(ws_name, {})
    wf = await workflow_store.create_workflow(ws.id, "wf", ["a"])
    step = await workflow_store.create_step(ws.id, wf.id, "a")
    async with connect(settings.db_path) as conn:
        await conn.execute(
            "UPDATE workflow_steps SET status='pending', started_at=NULL "
            "WHERE id=?",
            (step.id,),
        )
        await conn.commit()
    return ws.id, wf.id, step.id


class TestClusterCoordinatedLeases:
    """v1.3 — multiple LeaseStore replicas race-coordinate via Redis.

    The ``RedisBackend`` is fronted by ``fakeredis.FakeServer`` so each
    test simulates two replicas pointing at *separate* SQLite DBs but
    *one* shared Redis cluster. Without v1.3 these tests would let both
    replicas win.
    """

    async def test_two_workers_one_wins_via_cluster(
        self, settings, store, tmp_path
    ) -> None:
        """Two LeaseStore instances against separate SQLite DBs but a
        shared Redis cluster: exactly one acquire wins.

        We mirror replica A's workspace + workflow + step rows into
        replica B's separate SQLite DB so the cluster lock is what
        decides the winner — the local DB writes would otherwise both
        succeed against their own files.
        """

        from plinth_workspace.db import connect, init_db
        from plinth_workspace.leases import LeaseConflict
        from plinth_workspace.storage import WorkspaceStore
        from plinth_workspace.workflows import WorkflowStore

        # Replica A (the existing fixture's settings.db_path)
        ws_id_a, wf_id_a, step_id_a = (
            await _create_cluster_workspace_with_pending_step(
                store, settings, "alpha"
            )
        )

        # Replica B — separate SQLite DB. Use the same store APIs to
        # ensure schema columns (incl. timestamps) are populated, then
        # realign the IDs to match replica A so the cluster key collides.
        replica_b_dir = tmp_path / "replica-b"
        replica_b_dir.mkdir(parents=True, exist_ok=True)
        blobs_b = replica_b_dir / "blobs"
        blobs_b.mkdir(parents=True, exist_ok=True)
        replica_b_path = replica_b_dir / "workspace.db"
        await init_db(replica_b_path)
        store_b_ws = WorkspaceStore(replica_b_path, blobs_b)

        ws_b = await store_b_ws.create_workspace("alpha", {})
        wf_store_b = WorkflowStore(replica_b_path)
        wf_b = await wf_store_b.create_workflow(ws_b.id, "wf", ["a"])
        step_b = await wf_store_b.create_step(ws_b.id, wf_b.id, "a")

        # Realign IDs to match replica A so the cluster key
        # ``<prefix>:workspace:lease:<workspace_id>:<step_id>`` collides.
        # Disable FKs during the rename so we don't have to reorder
        # parent/child updates.
        async with connect(replica_b_path) as conn:
            await conn.execute("PRAGMA foreign_keys=OFF")
            await conn.execute(
                "UPDATE workspaces SET id=? WHERE id=?",
                (ws_id_a, ws_b.id),
            )
            await conn.execute(
                "UPDATE workflows SET id=?, workspace_id=? WHERE id=?",
                (wf_id_a, ws_id_a, wf_b.id),
            )
            await conn.execute(
                "UPDATE workflow_steps SET id=?, workflow_id=?, "
                "status='pending', started_at=NULL WHERE id=?",
                (step_id_a, wf_id_a, step_b.id),
            )
            await conn.commit()
            await conn.execute("PRAGMA foreign_keys=ON")

        # Shared Redis cluster.
        server = _fake_redis_server()
        store_a_lease = LeaseStore(
            settings.db_path,
            coordination=_shared_redis_backend(server),
        )
        store_b_lease = LeaseStore(
            replica_b_path,
            coordination=_shared_redis_backend(server),
        )

        worker_a = await store_a_lease.register_worker()
        # Replica B's worker registration writes to its own DB.
        store_b_pre = LeaseStore(replica_b_path)
        worker_b = await store_b_pre.register_worker()

        async def acquire_via(s, wid):
            try:
                return await s.acquire_lease(
                    ws_id_a, wf_id_a, step_id_a,
                    worker_id=wid, ttl_seconds=60,
                )
            except LeaseConflict as exc:
                return exc

        results = await asyncio.gather(
            acquire_via(store_a_lease, worker_a.id),
            acquire_via(store_b_lease, worker_b.id),
        )
        successes = [r for r in results if not isinstance(r, Exception)]
        failures = [r for r in results if isinstance(r, LeaseConflict)]
        assert len(successes) == 1, (
            "exactly one replica must win the cluster lock race"
        )
        assert len(failures) == 1

    async def test_release_releases_cluster_lock(
        self, settings, store
    ) -> None:
        """Acquire from store A, release; store B can then acquire."""

        from plinth_workspace.leases import LeaseConflict

        ws_id, wf_id, step_id = (
            await _create_cluster_workspace_with_pending_step(
                store, settings, "release-cluster"
            )
        )

        server = _fake_redis_server()
        store_a = LeaseStore(
            settings.db_path,
            coordination=_shared_redis_backend(server),
        )
        store_b = LeaseStore(
            settings.db_path,
            coordination=_shared_redis_backend(server),
        )

        worker_a = await store_a.register_worker()
        worker_b = await store_a.register_worker()

        await store_a.acquire_lease(
            ws_id, wf_id, step_id, worker_id=worker_a.id, ttl_seconds=60
        )

        # B can't acquire yet — both cluster lock is held AND step is running.
        with pytest.raises(LeaseConflict):
            await store_b.acquire_lease(
                ws_id, wf_id, step_id, worker_id=worker_b.id, ttl_seconds=60
            )

        # A releases (status=pending re-queues so B can re-lease).
        await store_a.release_lease(
            ws_id, wf_id, step_id,
            worker_id=worker_a.id, step_status="pending",
        )

        # Now B should be able to acquire.
        lease_b = await store_b.acquire_lease(
            ws_id, wf_id, step_id, worker_id=worker_b.id, ttl_seconds=60
        )
        assert lease_b.worker_id == worker_b.id
        assert lease_b.status == "running"

    async def test_heartbeat_refreshes_cluster_ttl(
        self, settings, store
    ) -> None:
        """Heartbeat extends the cluster lock TTL too.

        Verified by inspecting the underlying fakeredis after a heartbeat
        with an increased TTL — the cluster key should still be present
        and re-asserted by the same holder.
        """

        ws_id, wf_id, step_id = (
            await _create_cluster_workspace_with_pending_step(
                store, settings, "heartbeat-cluster"
            )
        )

        server = _fake_redis_server()
        backend = _shared_redis_backend(server)
        lease_store = LeaseStore(settings.db_path, coordination=backend)

        worker = await lease_store.register_worker()
        await lease_store.acquire_lease(
            ws_id, wf_id, step_id, worker_id=worker.id, ttl_seconds=30
        )

        cluster_key = (
            f"{lease_store.coordination_prefix}:{ws_id}:{step_id}"
        )

        # Cluster lock is currently held by the worker.
        held = await backend.get(f"lock:{cluster_key}")
        assert held == worker.id

        # Heartbeat with a larger TTL.
        await lease_store.heartbeat_lease(
            ws_id, wf_id, step_id, worker_id=worker.id, ttl_seconds=600
        )

        # Cluster lock still held by the same worker.
        held_after = await backend.get(f"lock:{cluster_key}")
        assert held_after == worker.id

    async def test_memory_backend_uses_local_path_only(
        self, settings, store
    ) -> None:
        """When coordination is MemoryBackend, no cluster keys are written."""

        from plinth_workspace.coordination import MemoryBackend

        ws_id, wf_id, step_id = (
            await _create_cluster_workspace_with_pending_step(
                store, settings, "memory-only"
            )
        )

        memory = MemoryBackend()
        lease_store = LeaseStore(settings.db_path, coordination=memory)
        assert lease_store._cluster_enabled() is False

        worker = await lease_store.register_worker()
        lease = await lease_store.acquire_lease(
            ws_id, wf_id, step_id, worker_id=worker.id, ttl_seconds=60
        )
        assert lease.worker_id == worker.id
        # MemoryBackend's lock store is unused — no entries for our key.
        # Implementation detail; check the private dict to confirm.
        assert all(
            "lease" not in k for k in memory._locks  # type: ignore[attr-defined]
        )

    async def test_no_coordination_legacy_behavior(
        self, settings, store
    ) -> None:
        """Without a coordination backend, behaviour matches v1.2."""

        ws_id, wf_id, step_id = (
            await _create_cluster_workspace_with_pending_step(
                store, settings, "legacy"
            )
        )

        lease_store = LeaseStore(settings.db_path)
        assert lease_store.coordination is None
        assert lease_store._cluster_enabled() is False

        worker = await lease_store.register_worker()
        lease = await lease_store.acquire_lease(
            ws_id, wf_id, step_id, worker_id=worker.id, ttl_seconds=60
        )
        assert lease.worker_id == worker.id

        released = await lease_store.release_lease(
            ws_id, wf_id, step_id, worker_id=worker.id, step_status="completed"
        )
        assert released.status == "released"

    async def test_local_failure_releases_cluster(
        self, settings, store
    ) -> None:
        """If the local DB write fails after cluster acquire, the
        cluster lock is released so the next caller can retry without
        waiting for the cluster TTL.
        """

        from plinth_workspace.exceptions import WorkflowStepNotFound

        # Create a workspace + workflow but NOT the step (so the local
        # DB write inside ``_acquire_lease_local`` raises
        # ``WorkflowStepNotFound``).
        from plinth_workspace.workflows import WorkflowStore

        workflow_store = WorkflowStore(settings.db_path)
        ws = await store.create_workspace("local-fail", {})
        wf = await workflow_store.create_workflow(ws.id, "wf", ["a"])
        # No step row created — acquire below will hit the cluster lock
        # then fail at the local DB layer.

        server = _fake_redis_server()
        backend = _shared_redis_backend(server)
        lease_store = LeaseStore(settings.db_path, coordination=backend)

        worker = await lease_store.register_worker()

        # First attempt: cluster gate succeeds, local DB write fails.
        with pytest.raises(WorkflowStepNotFound):
            await lease_store.acquire_lease(
                ws.id, wf.id, "step_missing",
                worker_id=worker.id, ttl_seconds=60,
            )

        # The cluster lock for that key must NOT still be held — the
        # next caller (from a different replica) should not see a
        # lingering orphan lock.
        cluster_key = (
            f"{lease_store.coordination_prefix}:{ws.id}:step_missing"
        )
        # Use a fresh holder to prove the lock is releasable.
        ok = await backend.acquire_lock(
            cluster_key, holder="fresh-holder", ttl_seconds=10
        )
        assert ok is True, (
            "expected the cluster lock to be released after local "
            "failure; instead found it orphaned"
        )

    async def test_cluster_release_on_concurrent_local_race(
        self, settings, store
    ) -> None:
        """The cluster gate is permissive — when both replicas pass the
        cluster gate sequentially and only the SECOND fails the local
        race (because the first already held the row), the second
        replica's cluster lock attempt should fail (since the first
        replica holds it) and surface as ``LeaseConflict`` cleanly.
        """

        from plinth_workspace.leases import LeaseConflict

        ws_id, wf_id, step_id = (
            await _create_cluster_workspace_with_pending_step(
                store, settings, "concurrent-race"
            )
        )

        server = _fake_redis_server()
        store_a = LeaseStore(
            settings.db_path,
            coordination=_shared_redis_backend(server),
        )
        store_b = LeaseStore(
            settings.db_path,
            coordination=_shared_redis_backend(server),
        )

        worker_a = await store_a.register_worker()
        worker_b = await store_a.register_worker()

        # A wins.
        await store_a.acquire_lease(
            ws_id, wf_id, step_id, worker_id=worker_a.id, ttl_seconds=60
        )
        # B loses on the cluster gate alone.
        with pytest.raises(LeaseConflict) as exc_info:
            await store_b.acquire_lease(
                ws_id, wf_id, step_id, worker_id=worker_b.id, ttl_seconds=60
            )
        # Error details should include the cluster key so operators can
        # trace cluster contention.
        assert exc_info.value.details.get("cluster_key", "").endswith(
            f":{ws_id}:{step_id}"
        )
