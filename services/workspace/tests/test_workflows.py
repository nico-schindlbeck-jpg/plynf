# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 The Plinth Authors
"""End-to-end tests for the v0.2 Workflows API."""

from __future__ import annotations

import httpx


# ---------------------------------------------------------------------------
# Create + list + get


async def test_create_workflow(
    client: httpx.AsyncClient, workspace_id: str
) -> None:
    resp = await client.post(
        f"/v1/workspaces/{workspace_id}/workflows",
        json={
            "name": "research",
            "steps": ["search", "fetch", "synthesize"],
            "metadata": {"owner": "alice"},
        },
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["id"].startswith("wf_")
    assert body["name"] == "research"
    assert body["status"] == "pending"
    assert body["steps_manifest"] == ["search", "fetch", "synthesize"]
    assert body["steps"] == []
    assert body["metadata"] == {"owner": "alice"}
    assert body["started_at"] is None
    assert body["finished_at"] is None


async def test_create_workflow_validation(
    client: httpx.AsyncClient, workspace_id: str
) -> None:
    # Empty steps list → 400.
    resp = await client.post(
        f"/v1/workspaces/{workspace_id}/workflows",
        json={"name": "x", "steps": []},
    )
    assert resp.status_code == 400

    # Duplicate steps → 400.
    resp = await client.post(
        f"/v1/workspaces/{workspace_id}/workflows",
        json={"name": "x", "steps": ["a", "a"]},
    )
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "INVALID_ARGUMENTS"

    # Empty step name → 400.
    resp = await client.post(
        f"/v1/workspaces/{workspace_id}/workflows",
        json={"name": "x", "steps": ["a", ""]},
    )
    assert resp.status_code == 400


async def test_create_workflow_workspace_404(client: httpx.AsyncClient) -> None:
    resp = await client.post(
        "/v1/workspaces/ws_nope/workflows",
        json={"name": "x", "steps": ["a"]},
    )
    assert resp.status_code == 404


async def test_list_workflows(
    client: httpx.AsyncClient, workspace_id: str
) -> None:
    for nm in ["one", "two", "three"]:
        await client.post(
            f"/v1/workspaces/{workspace_id}/workflows",
            json={"name": nm, "steps": ["a"]},
        )
    resp = await client.get(f"/v1/workspaces/{workspace_id}/workflows")
    assert resp.status_code == 200
    names = [w["name"] for w in resp.json()["workflows"]]
    assert names == ["one", "two", "three"]


async def test_get_workflow(
    client: httpx.AsyncClient, workspace_id: str
) -> None:
    create = await client.post(
        f"/v1/workspaces/{workspace_id}/workflows",
        json={"name": "wf", "steps": ["a", "b"]},
    )
    wf_id = create.json()["id"]
    resp = await client.get(f"/v1/workspaces/{workspace_id}/workflows/{wf_id}")
    assert resp.status_code == 200
    assert resp.json()["id"] == wf_id


async def test_get_workflow_404(
    client: httpx.AsyncClient, workspace_id: str
) -> None:
    resp = await client.get(f"/v1/workspaces/{workspace_id}/workflows/wf_nope")
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "WORKFLOW_NOT_FOUND"


# ---------------------------------------------------------------------------
# Step lifecycle


async def _new_workflow(
    client: httpx.AsyncClient, workspace_id: str, steps: list[str]
) -> str:
    create = await client.post(
        f"/v1/workspaces/{workspace_id}/workflows",
        json={"name": "wf", "steps": steps},
    )
    return create.json()["id"]


async def test_start_step(
    client: httpx.AsyncClient, workspace_id: str
) -> None:
    wf_id = await _new_workflow(client, workspace_id, ["a", "b", "c"])
    resp = await client.post(
        f"/v1/workspaces/{workspace_id}/workflows/{wf_id}/steps",
        json={"name": "a", "input": {"topic": "ai"}},
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["id"].startswith("step_")
    assert body["status"] == "running"
    assert body["attempt"] == 1
    assert body["started_at"]
    assert body["input"] == {"topic": "ai"}

    # Workflow status now running.
    wf = await client.get(f"/v1/workspaces/{workspace_id}/workflows/{wf_id}")
    assert wf.json()["status"] == "running"
    assert wf.json()["started_at"] is not None


async def test_complete_step(
    client: httpx.AsyncClient, workspace_id: str
) -> None:
    wf_id = await _new_workflow(client, workspace_id, ["a", "b"])
    step = await client.post(
        f"/v1/workspaces/{workspace_id}/workflows/{wf_id}/steps",
        json={"name": "a", "input": {}},
    )
    step_id = step.json()["id"]

    resp = await client.patch(
        f"/v1/workspaces/{workspace_id}/workflows/{wf_id}/steps/{step_id}",
        json={
            "status": "completed",
            "output": {"sources": [1, 2]},
            "snapshot_id": "snap_xyz",
        },
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "completed"
    assert body["output"] == {"sources": [1, 2]}
    assert body["snapshot_id"] == "snap_xyz"
    assert body["finished_at"] is not None

    # Workflow still running (b not done).
    wf = await client.get(f"/v1/workspaces/{workspace_id}/workflows/{wf_id}")
    assert wf.json()["status"] == "running"


async def test_workflow_completed_when_all_steps_done(
    client: httpx.AsyncClient, workspace_id: str
) -> None:
    wf_id = await _new_workflow(client, workspace_id, ["a", "b"])
    for nm in ["a", "b"]:
        s = await client.post(
            f"/v1/workspaces/{workspace_id}/workflows/{wf_id}/steps",
            json={"name": nm},
        )
        sid = s.json()["id"]
        await client.patch(
            f"/v1/workspaces/{workspace_id}/workflows/{wf_id}/steps/{sid}",
            json={"status": "completed", "snapshot_id": f"snap_{nm}"},
        )

    wf = await client.get(f"/v1/workspaces/{workspace_id}/workflows/{wf_id}")
    body = wf.json()
    assert body["status"] == "completed"
    assert body["finished_at"] is not None


async def test_failed_step_fails_workflow(
    client: httpx.AsyncClient, workspace_id: str
) -> None:
    wf_id = await _new_workflow(client, workspace_id, ["a", "b"])
    s = await client.post(
        f"/v1/workspaces/{workspace_id}/workflows/{wf_id}/steps",
        json={"name": "a"},
    )
    sid = s.json()["id"]
    await client.patch(
        f"/v1/workspaces/{workspace_id}/workflows/{wf_id}/steps/{sid}",
        json={"status": "failed", "error": "boom"},
    )

    wf = await client.get(f"/v1/workspaces/{workspace_id}/workflows/{wf_id}")
    body = wf.json()
    assert body["status"] == "failed"
    assert body["finished_at"] is not None


async def test_failed_step_does_not_auto_cancel(
    client: httpx.AsyncClient, workspace_id: str
) -> None:
    """Spec requirement: a failed step does not auto-cancel the workflow.

    The workflow may transition to ``failed``, but it must not transition to
    ``cancelled`` — that's reserved for the explicit ``/cancel`` endpoint.
    Operators decide what to do after a failure (retry, abandon, etc.).
    """

    wf_id = await _new_workflow(client, workspace_id, ["a", "b"])
    s = await client.post(
        f"/v1/workspaces/{workspace_id}/workflows/{wf_id}/steps",
        json={"name": "a"},
    )
    await client.patch(
        f"/v1/workspaces/{workspace_id}/workflows/{wf_id}/steps/{s.json()['id']}",
        json={"status": "failed", "error": "boom"},
    )
    wf = (await client.get(
        f"/v1/workspaces/{workspace_id}/workflows/{wf_id}"
    )).json()
    assert wf["status"] != "cancelled"
    # The remaining manifest entry "b" must still be re-startable — failure
    # is not terminal in the sense that operator can retry.
    retry = await client.post(
        f"/v1/workspaces/{workspace_id}/workflows/{wf_id}/steps",
        json={"name": "a"},
    )
    assert retry.status_code == 201
    assert retry.json()["attempt"] == 2


async def test_invalid_step_name_400(
    client: httpx.AsyncClient, workspace_id: str
) -> None:
    wf_id = await _new_workflow(client, workspace_id, ["a", "b"])
    resp = await client.post(
        f"/v1/workspaces/{workspace_id}/workflows/{wf_id}/steps",
        json={"name": "not-in-manifest"},
    )
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "INVALID_WORKFLOW_STEP"


async def test_step_workflow_404(
    client: httpx.AsyncClient, workspace_id: str
) -> None:
    resp = await client.post(
        f"/v1/workspaces/{workspace_id}/workflows/wf_nope/steps",
        json={"name": "a"},
    )
    assert resp.status_code == 404


async def test_update_step_404(
    client: httpx.AsyncClient, workspace_id: str
) -> None:
    wf_id = await _new_workflow(client, workspace_id, ["a"])
    resp = await client.patch(
        f"/v1/workspaces/{workspace_id}/workflows/{wf_id}/steps/step_nope",
        json={"status": "completed"},
    )
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "WORKFLOW_STEP_NOT_FOUND"


# ---------------------------------------------------------------------------
# Attempt counter


async def test_attempt_increments(
    client: httpx.AsyncClient, workspace_id: str
) -> None:
    wf_id = await _new_workflow(client, workspace_id, ["a", "b"])
    first = await client.post(
        f"/v1/workspaces/{workspace_id}/workflows/{wf_id}/steps",
        json={"name": "a"},
    )
    assert first.json()["attempt"] == 1

    second = await client.post(
        f"/v1/workspaces/{workspace_id}/workflows/{wf_id}/steps",
        json={"name": "a"},
    )
    assert second.json()["attempt"] == 2


async def test_failed_then_retry_completes_workflow(
    client: httpx.AsyncClient, workspace_id: str
) -> None:
    """A retry that completes should let the manifest become 'completed'."""

    wf_id = await _new_workflow(client, workspace_id, ["a"])
    s1 = await client.post(
        f"/v1/workspaces/{workspace_id}/workflows/{wf_id}/steps",
        json={"name": "a"},
    )
    await client.patch(
        f"/v1/workspaces/{workspace_id}/workflows/{wf_id}/steps/{s1.json()['id']}",
        json={"status": "failed", "error": "transient"},
    )
    # Workflow currently failed.
    wf = await client.get(f"/v1/workspaces/{workspace_id}/workflows/{wf_id}")
    assert wf.json()["status"] == "failed"

    # Retry succeeds — workflow flips to completed.
    s2 = await client.post(
        f"/v1/workspaces/{workspace_id}/workflows/{wf_id}/steps",
        json={"name": "a"},
    )
    assert s2.json()["attempt"] == 2
    await client.patch(
        f"/v1/workspaces/{workspace_id}/workflows/{wf_id}/steps/{s2.json()['id']}",
        json={"status": "completed", "snapshot_id": "snap_ok"},
    )
    wf = await client.get(f"/v1/workspaces/{workspace_id}/workflows/{wf_id}")
    assert wf.json()["status"] == "completed"


# ---------------------------------------------------------------------------
# Resume


async def test_resume_with_no_steps(
    client: httpx.AsyncClient, workspace_id: str
) -> None:
    wf_id = await _new_workflow(client, workspace_id, ["a", "b", "c"])
    resp = await client.get(
        f"/v1/workspaces/{workspace_id}/workflows/{wf_id}/resume"
    )
    body = resp.json()
    assert body["next_step"] == "a"
    assert body["last_completed"] is None
    assert body["snapshot_id"] is None
    assert body["workflow_status"] == "pending"


async def test_resume_after_one_completed(
    client: httpx.AsyncClient, workspace_id: str
) -> None:
    wf_id = await _new_workflow(client, workspace_id, ["a", "b", "c"])
    s = await client.post(
        f"/v1/workspaces/{workspace_id}/workflows/{wf_id}/steps",
        json={"name": "a"},
    )
    await client.patch(
        f"/v1/workspaces/{workspace_id}/workflows/{wf_id}/steps/{s.json()['id']}",
        json={"status": "completed", "snapshot_id": "snap_a"},
    )
    resp = await client.get(
        f"/v1/workspaces/{workspace_id}/workflows/{wf_id}/resume"
    )
    body = resp.json()
    assert body["next_step"] == "b"
    assert body["last_completed"]["name"] == "a"
    assert body["snapshot_id"] == "snap_a"
    assert body["workflow_status"] == "running"


async def test_resume_when_all_done(
    client: httpx.AsyncClient, workspace_id: str
) -> None:
    wf_id = await _new_workflow(client, workspace_id, ["a"])
    s = await client.post(
        f"/v1/workspaces/{workspace_id}/workflows/{wf_id}/steps",
        json={"name": "a"},
    )
    await client.patch(
        f"/v1/workspaces/{workspace_id}/workflows/{wf_id}/steps/{s.json()['id']}",
        json={"status": "completed", "snapshot_id": "snap_done"},
    )
    resp = await client.get(
        f"/v1/workspaces/{workspace_id}/workflows/{wf_id}/resume"
    )
    body = resp.json()
    assert body["next_step"] is None
    assert body["last_completed"]["name"] == "a"
    assert body["snapshot_id"] == "snap_done"
    assert body["workflow_status"] == "completed"


async def test_resume_404(
    client: httpx.AsyncClient, workspace_id: str
) -> None:
    resp = await client.get(
        f"/v1/workspaces/{workspace_id}/workflows/wf_nope/resume"
    )
    assert resp.status_code == 404


async def test_resume_after_two_of_five_completed(
    client: httpx.AsyncClient, workspace_id: str
) -> None:
    """Spec scenario: 5-step manifest, first 2 completed → resume → step #3."""

    wf_id = await _new_workflow(
        client, workspace_id, ["one", "two", "three", "four", "five"]
    )
    last_snap = None
    for i, name in enumerate(["one", "two"]):
        s = await client.post(
            f"/v1/workspaces/{workspace_id}/workflows/{wf_id}/steps",
            json={"name": name},
        )
        last_snap = f"snap_{i}"
        await client.patch(
            f"/v1/workspaces/{workspace_id}/workflows/{wf_id}/steps/{s.json()['id']}",
            json={"status": "completed", "snapshot_id": last_snap},
        )

    resp = await client.get(
        f"/v1/workspaces/{workspace_id}/workflows/{wf_id}/resume"
    )
    body = resp.json()
    assert body["next_step"] == "three"
    assert body["last_completed"]["name"] == "two"
    assert body["snapshot_id"] == last_snap
    assert body["workflow_status"] == "running"


async def test_resume_when_step_crashed_running(
    client: httpx.AsyncClient, workspace_id: str
) -> None:
    """Crash mid-step (status=running) → resume can re-run the step.

    A step that's left ``running`` (e.g. agent crashed before patching it
    to ``completed``) is NOT considered done. ``next_step`` therefore still
    points at that step's manifest entry, and a fresh attempt can be created.
    """

    wf_id = await _new_workflow(client, workspace_id, ["a", "b"])
    s1 = await client.post(
        f"/v1/workspaces/{workspace_id}/workflows/{wf_id}/steps",
        json={"name": "a"},
    )
    # Note: no PATCH — step stays in 'running' (simulating crash).
    assert s1.json()["status"] == "running"

    resp = await client.get(
        f"/v1/workspaces/{workspace_id}/workflows/{wf_id}/resume"
    )
    body = resp.json()
    assert body["next_step"] == "a"
    # No completed step yet → no snapshot to restore from.
    assert body["last_completed"] is None
    assert body["snapshot_id"] is None

    # And we can re-create the step (attempt #2).
    retry = await client.post(
        f"/v1/workspaces/{workspace_id}/workflows/{wf_id}/steps",
        json={"name": "a"},
    )
    assert retry.status_code == 201
    assert retry.json()["attempt"] == 2


# ---------------------------------------------------------------------------
# Cancel


async def test_cancel_workflow(
    client: httpx.AsyncClient, workspace_id: str
) -> None:
    wf_id = await _new_workflow(client, workspace_id, ["a", "b"])
    # start a, leave it running.
    s = await client.post(
        f"/v1/workspaces/{workspace_id}/workflows/{wf_id}/steps",
        json={"name": "a"},
    )

    resp = await client.post(
        f"/v1/workspaces/{workspace_id}/workflows/{wf_id}/cancel"
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "cancelled"
    assert body["finished_at"] is not None
    # The running step is now cancelled.
    [step] = body["steps"]
    assert step["id"] == s.json()["id"]
    assert step["status"] == "cancelled"
    assert step["finished_at"] is not None


async def test_cancel_keeps_completed_steps_intact(
    client: httpx.AsyncClient, workspace_id: str
) -> None:
    wf_id = await _new_workflow(client, workspace_id, ["a", "b"])
    s = await client.post(
        f"/v1/workspaces/{workspace_id}/workflows/{wf_id}/steps",
        json={"name": "a"},
    )
    await client.patch(
        f"/v1/workspaces/{workspace_id}/workflows/{wf_id}/steps/{s.json()['id']}",
        json={"status": "completed"},
    )

    resp = await client.post(
        f"/v1/workspaces/{workspace_id}/workflows/{wf_id}/cancel"
    )
    body = resp.json()
    [step] = body["steps"]
    assert step["status"] == "completed"
    assert body["status"] == "cancelled"


async def test_cancel_404(
    client: httpx.AsyncClient, workspace_id: str
) -> None:
    resp = await client.post(
        f"/v1/workspaces/{workspace_id}/workflows/wf_nope/cancel"
    )
    assert resp.status_code == 404


async def test_cancelled_workflow_status_is_sticky(
    client: httpx.AsyncClient, workspace_id: str
) -> None:
    """Updating a step on a cancelled workflow must not flip status back."""

    wf_id = await _new_workflow(client, workspace_id, ["a"])
    s = await client.post(
        f"/v1/workspaces/{workspace_id}/workflows/{wf_id}/steps",
        json={"name": "a"},
    )
    await client.post(
        f"/v1/workspaces/{workspace_id}/workflows/{wf_id}/cancel"
    )
    # Now patch the step to completed — workflow must STAY cancelled.
    await client.patch(
        f"/v1/workspaces/{workspace_id}/workflows/{wf_id}/steps/{s.json()['id']}",
        json={"status": "completed"},
    )
    wf = await client.get(f"/v1/workspaces/{workspace_id}/workflows/{wf_id}")
    assert wf.json()["status"] == "cancelled"


# ---------------------------------------------------------------------------
# Step output null preservation


async def test_complete_with_null_output(
    client: httpx.AsyncClient, workspace_id: str
) -> None:
    wf_id = await _new_workflow(client, workspace_id, ["a"])
    s = await client.post(
        f"/v1/workspaces/{workspace_id}/workflows/{wf_id}/steps",
        json={"name": "a"},
    )
    resp = await client.patch(
        f"/v1/workspaces/{workspace_id}/workflows/{wf_id}/steps/{s.json()['id']}",
        json={"status": "completed"},
    )
    assert resp.status_code == 200
    assert resp.json()["output"] is None


async def test_update_preserves_existing_snapshot_id(
    client: httpx.AsyncClient, workspace_id: str
) -> None:
    """If a step was started with a snapshot_id, completing without one keeps it."""

    wf_id = await _new_workflow(client, workspace_id, ["a"])
    s = await client.post(
        f"/v1/workspaces/{workspace_id}/workflows/{wf_id}/steps",
        json={"name": "a", "snapshot_id": "snap_initial"},
    )
    resp = await client.patch(
        f"/v1/workspaces/{workspace_id}/workflows/{wf_id}/steps/{s.json()['id']}",
        json={"status": "completed"},
    )
    assert resp.json()["snapshot_id"] == "snap_initial"


async def test_update_step_on_unknown_workflow_404(
    client: httpx.AsyncClient, workspace_id: str
) -> None:
    resp = await client.patch(
        f"/v1/workspaces/{workspace_id}/workflows/wf_nope/steps/step_nope",
        json={"status": "completed"},
    )
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "WORKFLOW_NOT_FOUND"


async def test_resume_picks_most_recent_completion(
    client: httpx.AsyncClient, workspace_id: str
) -> None:
    """If a step was completed twice (retry succeeds), resume sees the second one."""

    wf_id = await _new_workflow(client, workspace_id, ["a", "b"])
    # First attempt at "a" — completed with snap_old.
    s1 = await client.post(
        f"/v1/workspaces/{workspace_id}/workflows/{wf_id}/steps",
        json={"name": "a"},
    )
    await client.patch(
        f"/v1/workspaces/{workspace_id}/workflows/{wf_id}/steps/{s1.json()['id']}",
        json={"status": "completed", "snapshot_id": "snap_old"},
    )
    # Second attempt at "a" — completed with snap_new.
    s2 = await client.post(
        f"/v1/workspaces/{workspace_id}/workflows/{wf_id}/steps",
        json={"name": "a"},
    )
    await client.patch(
        f"/v1/workspaces/{workspace_id}/workflows/{wf_id}/steps/{s2.json()['id']}",
        json={"status": "completed", "snapshot_id": "snap_new"},
    )

    resp = await client.get(
        f"/v1/workspaces/{workspace_id}/workflows/{wf_id}/resume"
    )
    body = resp.json()
    # next_step is "b" because "a" has at least one completion.
    assert body["next_step"] == "b"
    # last_completed should be the most recent (snap_new).
    assert body["snapshot_id"] == "snap_new"
