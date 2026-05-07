# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 The Plinth Authors
"""Tests for ``plinth.workflows`` -- the v0.2 workflows SDK surface."""

from __future__ import annotations

import json

import httpx
import pytest
import respx

from plinth import (
    InvalidWorkflowStep,
    Plinth,
    ResumeInfo,
    Workflow,
    WorkflowHandle,
    WorkflowNotFound,
    WorkflowStep,
    WorkflowStepNotFound,
    Workspace,
)

from .conftest import (
    error_envelope,
    make_resume_info,
    make_workflow,
    make_workflow_step,
    make_workspace,
)

# ---------------------------------------------------------------------------
# Helper -- a ready-to-use workspace handle.
# ---------------------------------------------------------------------------


@pytest.fixture
def ws(client: Plinth, workspace_mock: respx.MockRouter) -> Workspace:
    """Return a Workspace bound to ws_01TESTWORKSPACE."""
    workspace_mock.get("/v1/workspaces").mock(
        return_value=httpx.Response(200, json={"workspaces": [make_workspace()]})
    )
    return client.workspace("research-task-1")


# ---------------------------------------------------------------------------
# create / get / list
# ---------------------------------------------------------------------------


def test_create_returns_workflow_handle(
    ws: Workspace, workspace_mock: respx.MockRouter
):
    payload = make_workflow(
        wf_id="wf_01ABC",
        name="research-pipeline",
        steps_manifest=["search", "fetch", "extract", "synthesize"],
        metadata={"topic": "renewable energy"},
    )
    route = workspace_mock.post(f"/v1/workspaces/{ws.id}/workflows").mock(
        return_value=httpx.Response(201, json=payload)
    )

    wf = ws.workflows.create(
        name="research-pipeline",
        steps=["search", "fetch", "extract", "synthesize"],
        metadata={"topic": "renewable energy"},
    )

    assert isinstance(wf, WorkflowHandle)
    assert wf.id == "wf_01ABC"
    assert wf.name == "research-pipeline"
    assert wf.steps_manifest == ["search", "fetch", "extract", "synthesize"]
    assert wf.metadata == {"topic": "renewable energy"}

    body = json.loads(route.calls.last.request.read())
    assert body["name"] == "research-pipeline"
    assert body["steps"] == ["search", "fetch", "extract", "synthesize"]
    assert body["metadata"] == {"topic": "renewable energy"}


def test_create_omits_metadata_when_none(
    ws: Workspace, workspace_mock: respx.MockRouter
):
    payload = make_workflow(wf_id="wf_meta_none", metadata={})
    route = workspace_mock.post(f"/v1/workspaces/{ws.id}/workflows").mock(
        return_value=httpx.Response(201, json=payload)
    )

    ws.workflows.create("p", steps=["a", "b"])

    body = json.loads(route.calls.last.request.read())
    assert "metadata" not in body


def test_get_returns_workflow_handle(
    ws: Workspace, workspace_mock: respx.MockRouter
):
    workspace_mock.get(f"/v1/workspaces/{ws.id}/workflows/wf_01ABC").mock(
        return_value=httpx.Response(200, json=make_workflow(wf_id="wf_01ABC"))
    )

    wf = ws.workflows.get("wf_01ABC")

    assert isinstance(wf, WorkflowHandle)
    assert wf.id == "wf_01ABC"


def test_get_404_raises_workflow_not_found(
    ws: Workspace, workspace_mock: respx.MockRouter
):
    workspace_mock.get(f"/v1/workspaces/{ws.id}/workflows/nope").mock(
        return_value=httpx.Response(
            404, json=error_envelope("WORKFLOW_NOT_FOUND", "nope")
        )
    )

    with pytest.raises(WorkflowNotFound):
        ws.workflows.get("nope")


def test_list_returns_workflows(ws: Workspace, workspace_mock: respx.MockRouter):
    workspace_mock.get(f"/v1/workspaces/{ws.id}/workflows").mock(
        return_value=httpx.Response(
            200,
            json={
                "workflows": [
                    make_workflow(wf_id="wf_A", name="a"),
                    make_workflow(wf_id="wf_B", name="b"),
                ]
            },
        )
    )

    out = ws.workflows.list()

    assert all(isinstance(w, Workflow) for w in out)
    assert {w.id for w in out} == {"wf_A", "wf_B"}


# ---------------------------------------------------------------------------
# get_or_create
# ---------------------------------------------------------------------------


def test_get_or_create_returns_existing(
    ws: Workspace, workspace_mock: respx.MockRouter
):
    existing = make_workflow(wf_id="wf_existing", name="research-pipeline")
    workspace_mock.get(f"/v1/workspaces/{ws.id}/workflows").mock(
        return_value=httpx.Response(200, json={"workflows": [existing]})
    )
    workspace_mock.get(f"/v1/workspaces/{ws.id}/workflows/wf_existing").mock(
        return_value=httpx.Response(200, json=existing)
    )

    wf = ws.workflows.get_or_create("research-pipeline", steps=["search", "fetch"])

    assert isinstance(wf, WorkflowHandle)
    assert wf.id == "wf_existing"


def test_get_or_create_creates_when_missing(
    ws: Workspace, workspace_mock: respx.MockRouter
):
    workspace_mock.get(f"/v1/workspaces/{ws.id}/workflows").mock(
        return_value=httpx.Response(200, json={"workflows": []})
    )
    create_payload = make_workflow(wf_id="wf_new", name="research-pipeline")
    create_route = workspace_mock.post(f"/v1/workspaces/{ws.id}/workflows").mock(
        return_value=httpx.Response(201, json=create_payload)
    )

    wf = ws.workflows.get_or_create(
        "research-pipeline",
        steps=["search", "fetch"],
        metadata={"topic": "x"},
    )

    assert isinstance(wf, WorkflowHandle)
    assert wf.id == "wf_new"
    assert create_route.called


def test_get_or_create_is_idempotent_across_calls(
    ws: Workspace, workspace_mock: respx.MockRouter
):
    """Two ``get_or_create`` calls return the same workflow ID."""
    existing = make_workflow(wf_id="wf_same", name="pipeline")

    workspace_mock.get(f"/v1/workspaces/{ws.id}/workflows").mock(
        return_value=httpx.Response(200, json={"workflows": [existing]})
    )
    workspace_mock.get(f"/v1/workspaces/{ws.id}/workflows/wf_same").mock(
        return_value=httpx.Response(200, json=existing)
    )
    # No create endpoint should fire -- if it does, the test fails because
    # respx will route to this and tests aren't stubbed for a 201 response.
    create_route = workspace_mock.post(f"/v1/workspaces/{ws.id}/workflows").mock(
        return_value=httpx.Response(500)
    )

    a = ws.workflows.get_or_create("pipeline", steps=["x"])
    b = ws.workflows.get_or_create("pipeline", steps=["x"])

    assert a.id == b.id == "wf_same"
    assert not create_route.called


# ---------------------------------------------------------------------------
# WorkflowHandle.start_step
# ---------------------------------------------------------------------------


def _create_handle(
    ws: Workspace,
    workspace_mock: respx.MockRouter,
    *,
    wf_id: str = "wf_01ABC",
    steps_manifest: list[str] | None = None,
    steps: list[dict] | None = None,
    status: str = "pending",
) -> WorkflowHandle:
    """Convenience: stand up a WorkflowHandle without going through create."""
    payload = make_workflow(
        wf_id=wf_id,
        steps_manifest=steps_manifest or ["search", "fetch"],
        steps=steps,
        status=status,
    )
    workspace_mock.post(f"/v1/workspaces/{ws.id}/workflows").mock(
        return_value=httpx.Response(201, json=payload)
    )
    return ws.workflows.create(
        name=payload["name"],
        steps=payload["steps_manifest"],
    )


def test_start_step_creates_running_step(
    ws: Workspace, workspace_mock: respx.MockRouter
):
    handle = _create_handle(ws, workspace_mock)

    step_payload = make_workflow_step(
        step_id="step_01",
        workflow_id=handle.id,
        name="search",
        status="running",
        input={"topic": "renewable"},
    )
    route = workspace_mock.post(
        f"/v1/workspaces/{ws.id}/workflows/{handle.id}/steps"
    ).mock(return_value=httpx.Response(201, json=step_payload))

    step = handle.start_step("search", input={"topic": "renewable"})

    assert isinstance(step, WorkflowStep)
    assert step.id == "step_01"
    assert step.status == "running"
    assert step.input == {"topic": "renewable"}

    body = json.loads(route.calls.last.request.read())
    assert body["name"] == "search"
    assert body["input"] == {"topic": "renewable"}


def test_start_step_records_step_locally(
    ws: Workspace, workspace_mock: respx.MockRouter
):
    handle = _create_handle(ws, workspace_mock)
    step_payload = make_workflow_step(
        step_id="step_01", workflow_id=handle.id, name="search"
    )
    workspace_mock.post(
        f"/v1/workspaces/{ws.id}/workflows/{handle.id}/steps"
    ).mock(return_value=httpx.Response(201, json=step_payload))

    handle.start_step("search")

    # The cached steps list now contains the new step.
    assert len(handle.steps) == 1
    assert handle.steps[0].id == "step_01"


def test_start_step_with_off_manifest_name_raises(
    ws: Workspace, workspace_mock: respx.MockRouter
):
    handle = _create_handle(
        ws, workspace_mock, steps_manifest=["search", "fetch"]
    )

    with pytest.raises(InvalidWorkflowStep):
        handle.start_step("not-in-manifest")


def test_start_step_workflow_not_found_maps_correctly(
    ws: Workspace, workspace_mock: respx.MockRouter
):
    handle = _create_handle(ws, workspace_mock)
    workspace_mock.post(
        f"/v1/workspaces/{ws.id}/workflows/{handle.id}/steps"
    ).mock(
        return_value=httpx.Response(
            404, json=error_envelope("WORKFLOW_NOT_FOUND", "no")
        )
    )

    with pytest.raises(WorkflowNotFound):
        handle.start_step("search")


def test_server_side_invalid_workflow_step_maps(
    ws: Workspace, workspace_mock: respx.MockRouter
):
    """A 400 with INVALID_WORKFLOW_STEP from the server is mapped client-side."""
    handle = _create_handle(ws, workspace_mock)
    workspace_mock.post(
        f"/v1/workspaces/{ws.id}/workflows/{handle.id}/steps"
    ).mock(
        return_value=httpx.Response(
            400,
            json=error_envelope(
                "INVALID_WORKFLOW_STEP", "step name not in manifest"
            ),
        )
    )

    # ``search`` is in our local manifest so we get past the client-side
    # check; the server still rejects, and we should get an
    # InvalidWorkflowStep back.
    with pytest.raises(InvalidWorkflowStep):
        handle.start_step("search")


# ---------------------------------------------------------------------------
# complete_step / fail_step / cancel_step
# ---------------------------------------------------------------------------


def test_complete_step_patches_status(
    ws: Workspace, workspace_mock: respx.MockRouter
):
    handle = _create_handle(ws, workspace_mock)
    completed = make_workflow_step(
        step_id="step_01",
        workflow_id=handle.id,
        name="search",
        status="completed",
        output={"found": 5},
        snapshot_id="snap_AFTER",
    )
    captured: dict = {}

    def handler(request):
        captured["body"] = request.read()
        return httpx.Response(200, json=completed)

    workspace_mock.patch(
        f"/v1/workspaces/{ws.id}/workflows/{handle.id}/steps/step_01"
    ).mock(side_effect=handler)

    step = handle.complete_step(
        "step_01", output={"found": 5}, snapshot_id="snap_AFTER"
    )

    assert step.status == "completed"
    assert step.output == {"found": 5}
    assert step.snapshot_id == "snap_AFTER"

    body = json.loads(captured["body"])
    assert body["status"] == "completed"
    assert body["output"] == {"found": 5}
    assert body["snapshot_id"] == "snap_AFTER"


def test_complete_step_omits_optional_fields(
    ws: Workspace, workspace_mock: respx.MockRouter
):
    handle = _create_handle(ws, workspace_mock)
    completed = make_workflow_step(
        step_id="step_01",
        workflow_id=handle.id,
        name="search",
        status="completed",
    )
    captured: dict = {}

    def handler(request):
        captured["body"] = request.read()
        return httpx.Response(200, json=completed)

    workspace_mock.patch(
        f"/v1/workspaces/{ws.id}/workflows/{handle.id}/steps/step_01"
    ).mock(side_effect=handler)

    handle.complete_step("step_01")

    body = json.loads(captured["body"])
    assert body == {"status": "completed"}


def test_fail_step_records_error(ws: Workspace, workspace_mock: respx.MockRouter):
    handle = _create_handle(ws, workspace_mock)
    failed = make_workflow_step(
        step_id="step_01",
        workflow_id=handle.id,
        name="search",
        status="failed",
        error="connection refused",
    )
    captured: dict = {}

    def handler(request):
        captured["body"] = request.read()
        return httpx.Response(200, json=failed)

    workspace_mock.patch(
        f"/v1/workspaces/{ws.id}/workflows/{handle.id}/steps/step_01"
    ).mock(side_effect=handler)

    step = handle.fail_step("step_01", error="connection refused")

    assert step.status == "failed"
    assert step.error == "connection refused"
    body = json.loads(captured["body"])
    assert body["status"] == "failed"
    assert body["error"] == "connection refused"


def test_cancel_step_marks_cancelled(
    ws: Workspace, workspace_mock: respx.MockRouter
):
    handle = _create_handle(ws, workspace_mock)
    cancelled = make_workflow_step(
        step_id="step_01",
        workflow_id=handle.id,
        name="search",
        status="cancelled",
    )
    workspace_mock.patch(
        f"/v1/workspaces/{ws.id}/workflows/{handle.id}/steps/step_01"
    ).mock(return_value=httpx.Response(200, json=cancelled))

    step = handle.cancel_step("step_01")

    assert step.status == "cancelled"


def test_patch_step_404_maps_to_workflow_step_not_found(
    ws: Workspace, workspace_mock: respx.MockRouter
):
    handle = _create_handle(ws, workspace_mock)
    workspace_mock.patch(
        f"/v1/workspaces/{ws.id}/workflows/{handle.id}/steps/step_X"
    ).mock(
        return_value=httpx.Response(
            404, json=error_envelope("WORKFLOW_STEP_NOT_FOUND", "no such step")
        )
    )

    with pytest.raises(WorkflowStepNotFound):
        handle.complete_step("step_X")


def test_start_then_complete_step_roundtrip(
    ws: Workspace, workspace_mock: respx.MockRouter
):
    """End-to-end: start a step, complete it, observe the cached state."""
    handle = _create_handle(ws, workspace_mock)

    started = make_workflow_step(
        step_id="step_01",
        workflow_id=handle.id,
        name="search",
        status="running",
        input={"topic": "x"},
    )
    completed = make_workflow_step(
        step_id="step_01",
        workflow_id=handle.id,
        name="search",
        status="completed",
        output={"found": 3},
        snapshot_id="snap_DONE",
    )
    workspace_mock.post(
        f"/v1/workspaces/{ws.id}/workflows/{handle.id}/steps"
    ).mock(return_value=httpx.Response(201, json=started))
    workspace_mock.patch(
        f"/v1/workspaces/{ws.id}/workflows/{handle.id}/steps/step_01"
    ).mock(return_value=httpx.Response(200, json=completed))

    step = handle.start_step("search", input={"topic": "x"})
    assert step.status == "running"

    final = handle.complete_step("step_01", output={"found": 3}, snapshot_id="snap_DONE")
    assert final.status == "completed"

    # Cached state reflects the final transition.
    assert len(handle.steps) == 1
    assert handle.steps[0].status == "completed"
    assert handle.steps[0].snapshot_id == "snap_DONE"


# ---------------------------------------------------------------------------
# cancel (whole workflow)
# ---------------------------------------------------------------------------


def test_cancel_workflow_updates_cached_status(
    ws: Workspace, workspace_mock: respx.MockRouter
):
    handle = _create_handle(ws, workspace_mock)
    cancelled = make_workflow(wf_id=handle.id, status="cancelled")
    route = workspace_mock.post(
        f"/v1/workspaces/{ws.id}/workflows/{handle.id}/cancel"
    ).mock(return_value=httpx.Response(200, json=cancelled))

    handle.cancel()

    assert route.called
    assert handle.status == "cancelled"


def test_cancel_workflow_404(ws: Workspace, workspace_mock: respx.MockRouter):
    handle = _create_handle(ws, workspace_mock)
    workspace_mock.post(
        f"/v1/workspaces/{ws.id}/workflows/{handle.id}/cancel"
    ).mock(
        return_value=httpx.Response(
            404, json=error_envelope("WORKFLOW_NOT_FOUND", "no")
        )
    )

    with pytest.raises(WorkflowNotFound):
        handle.cancel()


# ---------------------------------------------------------------------------
# resume_info
# ---------------------------------------------------------------------------


def test_resume_info_with_pending_next_step(
    ws: Workspace, workspace_mock: respx.MockRouter
):
    handle = _create_handle(ws, workspace_mock)
    last_step = make_workflow_step(
        step_id="step_01",
        workflow_id=handle.id,
        name="search",
        status="completed",
        snapshot_id="snap_AFTER",
    )
    workspace_mock.get(
        f"/v1/workspaces/{ws.id}/workflows/{handle.id}/resume"
    ).mock(
        return_value=httpx.Response(
            200,
            json=make_resume_info(
                workflow_id=handle.id,
                workflow_status="running",
                next_step="fetch",
                last_completed=last_step,
                snapshot_id="snap_AFTER",
            ),
        )
    )

    resume = handle.resume_info()

    assert isinstance(resume, ResumeInfo)
    assert resume.next_step == "fetch"
    assert resume.snapshot_id == "snap_AFTER"
    assert resume.last_completed is not None
    assert resume.last_completed.id == "step_01"


def test_resume_info_with_no_next_step_when_done(
    ws: Workspace, workspace_mock: respx.MockRouter
):
    handle = _create_handle(ws, workspace_mock)
    workspace_mock.get(
        f"/v1/workspaces/{ws.id}/workflows/{handle.id}/resume"
    ).mock(
        return_value=httpx.Response(
            200,
            json=make_resume_info(
                workflow_id=handle.id,
                workflow_status="completed",
                next_step=None,
                last_completed=make_workflow_step(status="completed"),
                snapshot_id="snap_FINAL",
            ),
        )
    )

    resume = handle.resume_info()

    assert resume.next_step is None
    assert resume.workflow_status == "completed"
    assert resume.snapshot_id == "snap_FINAL"


def test_resume_info_pristine_workflow(
    ws: Workspace, workspace_mock: respx.MockRouter
):
    """A workflow with no completed steps yet -- ``last_completed`` is ``None``."""
    handle = _create_handle(ws, workspace_mock)
    workspace_mock.get(
        f"/v1/workspaces/{ws.id}/workflows/{handle.id}/resume"
    ).mock(
        return_value=httpx.Response(
            200,
            json=make_resume_info(
                workflow_id=handle.id,
                workflow_status="pending",
                next_step="search",
                last_completed=None,
                snapshot_id=None,
            ),
        )
    )

    resume = handle.resume_info()

    assert resume.next_step == "search"
    assert resume.last_completed is None
    assert resume.snapshot_id is None


def test_resume_info_404_raises_workflow_not_found(
    ws: Workspace, workspace_mock: respx.MockRouter
):
    handle = _create_handle(ws, workspace_mock)
    workspace_mock.get(
        f"/v1/workspaces/{ws.id}/workflows/{handle.id}/resume"
    ).mock(
        return_value=httpx.Response(
            404, json=error_envelope("WORKFLOW_NOT_FOUND", "no")
        )
    )

    with pytest.raises(WorkflowNotFound):
        handle.resume_info()


# ---------------------------------------------------------------------------
# refresh
# ---------------------------------------------------------------------------


def test_refresh_re_fetches_from_server(
    ws: Workspace, workspace_mock: respx.MockRouter
):
    handle = _create_handle(ws, workspace_mock, status="pending")
    assert handle.status == "pending"

    updated = make_workflow(wf_id=handle.id, status="completed", steps=[])
    workspace_mock.get(
        f"/v1/workspaces/{ws.id}/workflows/{handle.id}"
    ).mock(return_value=httpx.Response(200, json=updated))

    handle.refresh()

    assert handle.status == "completed"


def test_refresh_404(ws: Workspace, workspace_mock: respx.MockRouter):
    handle = _create_handle(ws, workspace_mock)
    workspace_mock.get(
        f"/v1/workspaces/{ws.id}/workflows/{handle.id}"
    ).mock(
        return_value=httpx.Response(
            404, json=error_envelope("WORKFLOW_NOT_FOUND", "no")
        )
    )

    with pytest.raises(WorkflowNotFound):
        handle.refresh()
