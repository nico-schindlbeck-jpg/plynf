# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 The Plinth Authors
"""Tests for the v1.5 dashboard endpoints (replay aggregator + studio import).

Covers:

* ``GET /api/workflows/{id}/replay?ws=...`` aggregates workflow + audit + snapshots
* ``POST /api/workspaces/{ws}/workflows/import`` proxies the body to the workspace
* ``/studio`` and ``/workflows/{id}/replay`` SPA shell routes
* The ``_build_workflow_timeline`` helper produces the expected event ordering
"""

from __future__ import annotations

import httpx
import respx
from httpx import AsyncClient

from plinth_dashboard.server import _build_workflow_timeline
from plinth_dashboard.settings import Settings


# ---------------------------------------------------------------------------
# SPA shell routes


async def test_studio_route_serves_spa_shell(client: AsyncClient):
    r = await client.get("/studio")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/html")
    assert "Plinth" in r.text


async def test_workflow_replay_route_serves_spa_shell(client: AsyncClient):
    r = await client.get("/workflows/wf_abc/replay")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/html")
    assert "Plinth" in r.text


# ---------------------------------------------------------------------------
# Replay aggregator


def _wf_payload(
    *,
    wf_id: str = "wf_x",
    ws_id: str = "ws_a",
    steps: list[dict] | None = None,
    status: str = "running",
    created_at: str = "2026-05-10T12:00:00+00:00",
    started_at: str | None = "2026-05-10T12:00:01+00:00",
    finished_at: str | None = None,
) -> dict:
    return {
        "id": wf_id,
        "workspace_id": ws_id,
        "name": "research",
        "steps_manifest": ["search", "extract"],
        "steps": steps or [],
        "status": status,
        "metadata": {},
        "created_at": created_at,
        "started_at": started_at,
        "finished_at": finished_at,
    }


def _step(
    *,
    name: str,
    status: str,
    attempt: int = 1,
    started_at: str | None = "2026-05-10T12:00:02+00:00",
    finished_at: str | None = "2026-05-10T12:00:05+00:00",
    error: str | None = None,
) -> dict:
    return {
        "id": f"step_{name}_{attempt}",
        "workflow_id": "wf_x",
        "name": name,
        "status": status,
        "attempt": attempt,
        "started_at": started_at,
        "finished_at": finished_at,
        "input": None,
        "output": None,
        "error": error,
        "snapshot_id": None,
        "created_at": "2026-05-10T12:00:01+00:00",
    }


async def test_replay_endpoint_aggregates_payload(
    client: AsyncClient, settings: Settings
):
    wf = _wf_payload(
        steps=[
            _step(
                name="search",
                status="completed",
                started_at="2026-05-10T12:00:02+00:00",
                finished_at="2026-05-10T12:00:04+00:00",
            ),
            _step(
                name="extract",
                status="failed",
                attempt=1,
                started_at="2026-05-10T12:00:05+00:00",
                finished_at="2026-05-10T12:00:07+00:00",
                error="model timeout",
            ),
            _step(
                name="extract",
                status="completed",
                attempt=2,
                started_at="2026-05-10T12:00:08+00:00",
                finished_at="2026-05-10T12:00:10+00:00",
            ),
        ],
        finished_at="2026-05-10T12:00:10+00:00",
        status="completed",
    )
    snaps = {
        "snapshots": [
            {
                "id": "snap_1",
                "workspace_id": "ws_a",
                "name": "checkpoint",
                "message": None,
                "created_at": "2026-05-10T12:00:04+00:00",
                "kv_versions": {},
                "file_versions": {},
                "parent_snapshot_id": None,
            }
        ]
    }
    audit = {
        "events": [
            {
                "id": "evt_1",
                "timestamp": "2026-05-10T12:00:03+00:00",
                "tool_id": "web.search",
                "workspace_id": "ws_a",
                "agent_id": None,
                "arguments_hash": "x",
                "result_hash": "y",
                "cached": False,
                "duration_ms": 50,
                "cost_estimate_usd": 0.0,
                "error": None,
            }
        ]
    }
    with respx.mock(assert_all_called=False) as router:
        router.get(f"{settings.workspace_url}/v1/workspaces/ws_a/workflows/wf_x").mock(
            return_value=httpx.Response(200, json=wf)
        )
        router.get(f"{settings.workspace_url}/v1/workspaces/ws_a/snapshots").mock(
            return_value=httpx.Response(200, json=snaps)
        )
        router.get(f"{settings.gateway_url}/v1/audit").mock(
            return_value=httpx.Response(200, json=audit)
        )
        r = await client.get("/api/workflows/wf_x/replay?ws=ws_a")

    assert r.status_code == 200, r.text
    body = r.json()
    assert body["workflow"]["id"] == "wf_x"
    assert len(body["snapshots"]) == 1
    assert body["snapshots"][0]["id"] == "snap_1"
    assert len(body["audit_events"]) == 1
    # Timeline includes every step state-change + workflow created/finished.
    kinds = [ev["kind"] for ev in body["timeline"]]
    assert "workflow.created" in kinds
    assert "workflow.finished" in kinds
    assert kinds.count("step.created") == 3
    assert kinds.count("step.finished") == 3


async def test_replay_endpoint_missing_ws_returns_400(client: AsyncClient):
    r = await client.get("/api/workflows/wf_x/replay")
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "INVALID_ARGUMENTS"


async def test_replay_endpoint_workflow_404(
    client: AsyncClient, settings: Settings
):
    err = {"error": {"code": "WORKFLOW_NOT_FOUND", "message": "no such wf"}}
    with respx.mock(assert_all_called=False) as router:
        router.get(
            f"{settings.workspace_url}/v1/workspaces/ws_a/workflows/wf_nope"
        ).mock(return_value=httpx.Response(404, json=err))
        r = await client.get("/api/workflows/wf_nope/replay?ws=ws_a")
    assert r.status_code == 404
    assert r.json()["error"]["code"] == "WORKFLOW_NOT_FOUND"


async def test_replay_empty_audit_case_renders(
    client: AsyncClient, settings: Settings
):
    """A workflow that hasn't started yet still produces a coherent payload.

    No steps, no audit, but the SPA must still get a workflow + an empty
    timeline + zero snapshots. This mirrors the spec's "empty audit case".
    """
    wf = _wf_payload(steps=[], status="pending", started_at=None)
    with respx.mock(assert_all_called=False) as router:
        router.get(
            f"{settings.workspace_url}/v1/workspaces/ws_a/workflows/wf_x"
        ).mock(return_value=httpx.Response(200, json=wf))
        router.get(f"{settings.workspace_url}/v1/workspaces/ws_a/snapshots").mock(
            return_value=httpx.Response(200, json={"snapshots": []})
        )
        router.get(f"{settings.gateway_url}/v1/audit").mock(
            return_value=httpx.Response(200, json={"events": []})
        )
        r = await client.get("/api/workflows/wf_x/replay?ws=ws_a")
    assert r.status_code == 200
    body = r.json()
    assert body["workflow"]["status"] == "pending"
    assert body["snapshots"] == []
    assert body["audit_events"] == []
    # Just the workflow.created event when no steps exist.
    kinds = [ev["kind"] for ev in body["timeline"]]
    assert kinds == ["workflow.created"]


async def test_replay_endpoint_degrades_when_audit_unreachable(
    client: AsyncClient, settings: Settings
):
    """Audit/snapshot fetch failures don't block the replay payload."""
    wf = _wf_payload(steps=[_step(name="search", status="completed")])
    with respx.mock(assert_all_called=False) as router:
        router.get(
            f"{settings.workspace_url}/v1/workspaces/ws_a/workflows/wf_x"
        ).mock(return_value=httpx.Response(200, json=wf))
        router.get(f"{settings.workspace_url}/v1/workspaces/ws_a/snapshots").mock(
            side_effect=httpx.ConnectError("snapshot upstream down")
        )
        router.get(f"{settings.gateway_url}/v1/audit").mock(
            side_effect=httpx.ConnectError("gateway down")
        )
        r = await client.get("/api/workflows/wf_x/replay?ws=ws_a")
    assert r.status_code == 200
    body = r.json()
    assert body["workflow"]["id"] == "wf_x"
    assert body["snapshots"] == []
    assert body["audit_events"] == []
    # Timeline still reconstructs from the workflow itself.
    assert any(ev["kind"] == "step.finished" for ev in body["timeline"])


# ---------------------------------------------------------------------------
# Studio import proxy


async def test_studio_import_proxies_to_workspace(
    client: AsyncClient, settings: Settings
):
    """``POST /api/workspaces/{ws}/workflows/import`` forwards the body."""
    definition = {
        "name": "lead-research",
        "steps": [
            {"name": "search", "type": "tool", "tool_id": "web.search"},
        ],
    }
    payload = {
        "id": "wf_studio_1",
        "workspace_id": "ws_a",
        "name": "lead-research",
        "steps_manifest": ["search"],
        "steps": [],
        "status": "pending",
        "metadata": {"definition": definition, "imported_via": "plinth-studio"},
        "created_at": "2026-05-10T12:00:00+00:00",
        "started_at": None,
        "finished_at": None,
    }
    with respx.mock(assert_all_called=False) as router:
        route = router.post(
            f"{settings.workspace_url}/v1/workspaces/ws_a/workflows/import"
        ).mock(return_value=httpx.Response(201, json=payload))
        r = await client.post(
            "/api/workspaces/ws_a/workflows/import",
            json=definition,
        )
    assert r.status_code == 201, r.text
    assert r.json()["id"] == "wf_studio_1"
    # Body forwarded verbatim.
    assert route.called
    sent = route.calls.last.request.read()
    import json

    assert json.loads(sent) == definition


async def test_studio_import_propagates_workspace_400(
    client: AsyncClient, settings: Settings
):
    err = {
        "error": {
            "code": "INVALID_ARGUMENTS",
            "message": "workflow definition requires a non-empty 'name'",
            "details": {},
        }
    }
    with respx.mock(assert_all_called=False) as router:
        router.post(
            f"{settings.workspace_url}/v1/workspaces/ws_a/workflows/import"
        ).mock(return_value=httpx.Response(400, json=err))
        r = await client.post(
            "/api/workspaces/ws_a/workflows/import",
            json={"steps": []},
        )
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "INVALID_ARGUMENTS"


async def test_studio_import_502_when_upstream_unreachable(
    client: AsyncClient, settings: Settings
):
    with respx.mock(assert_all_called=False) as router:
        router.post(
            f"{settings.workspace_url}/v1/workspaces/ws_a/workflows/import"
        ).mock(side_effect=httpx.ConnectError("workspace down"))
        r = await client.post(
            "/api/workspaces/ws_a/workflows/import",
            json={"name": "x", "steps": [{"name": "a", "type": "tool"}]},
        )
    assert r.status_code == 502
    assert r.json()["error"]["code"] == "UPSTREAM_UNREACHABLE"


# ---------------------------------------------------------------------------
# _build_workflow_timeline helper


def test_build_timeline_orders_events_chronologically():
    wf = {
        "id": "wf_x",
        "created_at": "2026-05-10T12:00:00+00:00",
        "started_at": "2026-05-10T12:00:01+00:00",
        "finished_at": "2026-05-10T12:00:10+00:00",
        "status": "completed",
        "steps": [
            {
                "id": "s1",
                "name": "a",
                "status": "completed",
                "attempt": 1,
                "created_at": "2026-05-10T12:00:01+00:00",
                "started_at": "2026-05-10T12:00:02+00:00",
                "finished_at": "2026-05-10T12:00:04+00:00",
            },
            {
                "id": "s2",
                "name": "b",
                "status": "failed",
                "attempt": 1,
                "created_at": "2026-05-10T12:00:04+00:00",
                "started_at": "2026-05-10T12:00:05+00:00",
                "finished_at": "2026-05-10T12:00:07+00:00",
                "error": "boom",
            },
        ],
    }
    events = _build_workflow_timeline(wf)
    timestamps = [e["ts"] for e in events]
    assert timestamps == sorted(timestamps)
    # Workflow bookends are present.
    assert events[0]["kind"] == "workflow.created"
    assert events[-1]["kind"] == "workflow.finished"
    # The failed step's finish event carries the error message.
    failed = [e for e in events if e["kind"] == "step.finished" and e.get("error")]
    assert len(failed) == 1
    assert failed[0]["error"] == "boom"


def test_build_timeline_with_no_steps_just_bookends():
    wf = {
        "id": "wf_y",
        "created_at": "2026-05-10T12:00:00+00:00",
        "started_at": None,
        "finished_at": None,
        "status": "pending",
        "steps": [],
    }
    events = _build_workflow_timeline(wf)
    assert [e["kind"] for e in events] == ["workflow.created"]


def test_build_timeline_handles_running_step_no_finish():
    wf = {
        "id": "wf_z",
        "created_at": "2026-05-10T12:00:00+00:00",
        "started_at": "2026-05-10T12:00:01+00:00",
        "finished_at": None,
        "status": "running",
        "steps": [
            {
                "id": "s1",
                "name": "a",
                "status": "running",
                "attempt": 1,
                "created_at": "2026-05-10T12:00:01+00:00",
                "started_at": "2026-05-10T12:00:02+00:00",
                "finished_at": None,
            },
        ],
    }
    events = _build_workflow_timeline(wf)
    kinds = [e["kind"] for e in events]
    assert "step.started" in kinds
    assert "step.finished" not in kinds
