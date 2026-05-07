# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 The Plinth Authors
"""Tests for the v0.5 WorkersClient + lease helpers on WorkflowHandle."""

from __future__ import annotations

from datetime import datetime, timezone

import httpx
import pytest
import respx

from plinth import Plinth
from plinth.exceptions import LeaseConflict, WorkerNotFound
from plinth.models import Lease, Worker

from tests.conftest import error_envelope, make_workflow, make_workflow_step


def _now_iso() -> str:
    return datetime.now(tz=timezone.utc).isoformat()  # noqa: UP017


def make_worker(
    *,
    worker_id: str = "worker_01TESTWORKER",
    hostname: str = "test-host",
    pid: int = 1234,
    status: str = "active",
) -> dict:
    return {
        "id": worker_id,
        "hostname": hostname,
        "pid": pid,
        "started_at": _now_iso(),
        "last_heartbeat_at": _now_iso(),
        "status": status,
    }


def make_lease(
    *,
    step_id: str = "step_01TESTSTEP",
    worker_id: str = "worker_01TESTWORKER",
    status: str = "running",
) -> dict:
    return {
        "step_id": step_id,
        "worker_id": worker_id,
        "acquired_at": _now_iso(),
        "expires_at": _now_iso(),
        "heartbeat_at": _now_iso(),
        "status": status,
    }


# ---------------------------------------------------------------------------
# WorkersClient
# ---------------------------------------------------------------------------


def test_workers_register(client: Plinth, workspace_mock: respx.MockRouter) -> None:
    workspace_mock.post("/v1/workers/register").mock(
        return_value=httpx.Response(201, json=make_worker())
    )
    w = client.workers.register(hostname="host-1", pid=999)
    assert isinstance(w, Worker)
    assert w.id == "worker_01TESTWORKER"
    assert w.status == "active"


def test_workers_register_uses_default_hostname_pid(
    client: Plinth, workspace_mock: respx.MockRouter
) -> None:
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["json"] = request.read()
        return httpx.Response(201, json=make_worker())

    workspace_mock.post("/v1/workers/register").mock(side_effect=handler)
    client.workers.register()
    # Ensure something was sent for hostname + pid (defaults from os/socket).
    assert b"hostname" in captured["json"]
    assert b"pid" in captured["json"]


def test_workers_heartbeat(client: Plinth, workspace_mock: respx.MockRouter) -> None:
    workspace_mock.post("/v1/workers/worker_x/heartbeat").mock(
        return_value=httpx.Response(200, json=make_worker(worker_id="worker_x"))
    )
    w = client.workers.heartbeat("worker_x")
    assert w.id == "worker_x"


def test_workers_heartbeat_404(client: Plinth, workspace_mock: respx.MockRouter) -> None:
    workspace_mock.post("/v1/workers/worker_x/heartbeat").mock(
        return_value=httpx.Response(
            404,
            json=error_envelope("WORKER_NOT_FOUND", "no"),
        )
    )
    with pytest.raises(WorkerNotFound):
        client.workers.heartbeat("worker_x")


def test_workers_drain(client: Plinth, workspace_mock: respx.MockRouter) -> None:
    workspace_mock.post("/v1/workers/worker_x/drain").mock(
        return_value=httpx.Response(
            200, json=make_worker(worker_id="worker_x", status="draining")
        )
    )
    w = client.workers.drain("worker_x")
    assert w.status == "draining"


def test_workers_list(client: Plinth, workspace_mock: respx.MockRouter) -> None:
    workspace_mock.get("/v1/workers").mock(
        return_value=httpx.Response(
            200,
            json={"workers": [make_worker(worker_id="w_1"), make_worker(worker_id="w_2")]},
        )
    )
    workers = client.workers.list()
    assert len(workers) == 2
    assert {w.id for w in workers} == {"w_1", "w_2"}


def test_workers_list_with_status_filter(
    client: Plinth, workspace_mock: respx.MockRouter
) -> None:
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        return httpx.Response(200, json={"workers": []})

    workspace_mock.get("/v1/workers").mock(side_effect=handler)
    client.workers.list(status="draining")
    assert "status=draining" in captured["url"]


# ---------------------------------------------------------------------------
# WorkflowHandle.pending_steps + lease helpers
# ---------------------------------------------------------------------------


def _wf_handle(client: Plinth, workspace_mock: respx.MockRouter):
    """Fixture-like helper: build a workflow handle wired to mocked HTTP."""
    workspace_mock.get("/v1/workspaces").mock(
        return_value=httpx.Response(200, json={"workspaces": []})
    )
    workspace_mock.post("/v1/workspaces").mock(
        return_value=httpx.Response(
            201,
            json={
                "id": "ws_test",
                "name": "wf-tests",
                "created_at": _now_iso(),
                "updated_at": _now_iso(),
                "metadata": {},
            },
        )
    )
    workspace_mock.post("/v1/workspaces/ws_test/workflows").mock(
        return_value=httpx.Response(201, json=make_workflow(workspace_id="ws_test"))
    )
    ws = client.workspace("wf-tests")
    return ws.workflows.create("research-pipeline", steps=["search", "fetch"])


def test_pending_steps(client: Plinth, workspace_mock: respx.MockRouter) -> None:
    wf = _wf_handle(client, workspace_mock)
    workspace_mock.get(
        f"/v1/workspaces/ws_test/workflows/{wf.id}/pending"
    ).mock(
        return_value=httpx.Response(
            200,
            json={
                "steps": [
                    make_workflow_step(
                        step_id="step_a",
                        workflow_id=wf.id,
                        name="search",
                        status="pending",
                    ),
                ]
            },
        )
    )
    steps = wf.pending_steps()
    assert len(steps) == 1
    assert steps[0].status == "pending"


def test_lease_step_success(
    client: Plinth, workspace_mock: respx.MockRouter
) -> None:
    wf = _wf_handle(client, workspace_mock)
    workspace_mock.post(
        f"/v1/workspaces/ws_test/workflows/{wf.id}/steps/step_a/lease"
    ).mock(
        return_value=httpx.Response(200, json=make_lease(step_id="step_a"))
    )
    lease = wf.lease_step("step_a", "worker_x", ttl=120)
    assert isinstance(lease, Lease)
    assert lease.step_id == "step_a"


def test_lease_step_conflict_returns_none(
    client: Plinth, workspace_mock: respx.MockRouter
) -> None:
    wf = _wf_handle(client, workspace_mock)
    workspace_mock.post(
        f"/v1/workspaces/ws_test/workflows/{wf.id}/steps/step_a/lease"
    ).mock(
        return_value=httpx.Response(
            409, json=error_envelope("LEASE_CONFLICT", "already leased")
        )
    )
    lease = wf.lease_step("step_a", "worker_x", ttl=120)
    assert lease is None


def test_lease_step_other_error_propagates(
    client: Plinth, workspace_mock: respx.MockRouter
) -> None:
    wf = _wf_handle(client, workspace_mock)
    workspace_mock.post(
        f"/v1/workspaces/ws_test/workflows/{wf.id}/steps/step_a/lease"
    ).mock(
        return_value=httpx.Response(
            500, json=error_envelope("INTERNAL_ERROR", "boom")
        )
    )
    with pytest.raises(Exception):
        wf.lease_step("step_a", "worker_x")


def test_heartbeat_step(
    client: Plinth, workspace_mock: respx.MockRouter
) -> None:
    wf = _wf_handle(client, workspace_mock)
    workspace_mock.post(
        f"/v1/workspaces/ws_test/workflows/{wf.id}/steps/step_a/heartbeat"
    ).mock(
        return_value=httpx.Response(200, json=make_lease(step_id="step_a"))
    )
    lease = wf.heartbeat_step("step_a", "worker_x", ttl=60)
    assert lease.step_id == "step_a"


def test_release_step(
    client: Plinth, workspace_mock: respx.MockRouter
) -> None:
    wf = _wf_handle(client, workspace_mock)
    workspace_mock.post(
        f"/v1/workspaces/ws_test/workflows/{wf.id}/steps/step_a/release"
    ).mock(
        return_value=httpx.Response(
            200,
            json=make_lease(step_id="step_a", status="released"),
        )
    )
    workspace_mock.get(f"/v1/workspaces/ws_test/workflows/{wf.id}").mock(
        return_value=httpx.Response(200, json=make_workflow(wf_id=wf.id))
    )
    lease = wf.release_step(
        "step_a", "worker_x", status="completed", output={"k": "v"}
    )
    assert lease.status == "released"


def test_start_step_with_pending_initial_status(
    client: Plinth, workspace_mock: respx.MockRouter
) -> None:
    """A step started with ``initial_status='pending'`` must propagate to
    the body so the workspace creates a leasable step rather than an
    in-process running one."""

    wf = _wf_handle(client, workspace_mock)
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        import json

        captured["body"] = json.loads(request.read())
        return httpx.Response(
            201,
            json=make_workflow_step(
                step_id="step_p",
                workflow_id=wf.id,
                name="search",
                status="pending",
            ),
        )

    workspace_mock.post(
        f"/v1/workspaces/ws_test/workflows/{wf.id}/steps"
    ).mock(side_effect=handler)
    step = wf.start_step("search", input={"topic": "x"}, initial_status="pending")
    assert step.status == "pending"
    assert captured["body"]["initial_status"] == "pending"


def test_lease_conflict_exception_class(
    client: Plinth, workspace_mock: respx.MockRouter
) -> None:
    """When the lease helper isn't used (raw HTTP), a 409 still produces LeaseConflict."""

    workspace_mock.post(
        "/v1/workspaces/ws_test/workflows/wf_x/steps/step_x/lease"
    ).mock(
        return_value=httpx.Response(
            409, json=error_envelope("LEASE_CONFLICT", "active lease")
        )
    )
    # Use the raw _http transport on the client to ensure the error code
    # flows through the central mapping table.
    with pytest.raises(LeaseConflict):
        client._workspace_http.post(
            "/v1/workspaces/ws_test/workflows/wf_x/steps/step_x/lease",
            json={"worker_id": "w", "ttl_seconds": 60},
        )
