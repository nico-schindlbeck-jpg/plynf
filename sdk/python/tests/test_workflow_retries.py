# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 The Plinth Authors
"""SDK-level tests for v1.1 workflow retries + DLQ."""

from __future__ import annotations

from typing import Any

import httpx
import pytest
import respx

from plinth import (
    DLQEntry,
    Plinth,
    Workspace,
    WorkflowHandle,
)

from .conftest import (
    make_workflow,
    make_workflow_step,
    make_workspace,
)


@pytest.fixture
def ws(client: Plinth, workspace_mock: respx.MockRouter) -> Workspace:
    workspace_mock.get("/v1/workspaces").mock(
        return_value=httpx.Response(200, json={"workspaces": [make_workspace()]})
    )
    return client.workspace("research-task-1")


def _make_dlq_entry(
    *,
    dlq_id: str = "dlqstep_01ABC",
    step_id: str = "step_01TESTSTEP",
    workflow_id: str = "wf_01ABC",
    workspace_id: str = "ws_01TESTWORKSPACE",
    step_name: str = "search",
    attempts: int = 3,
    last_error: str = "boom",
) -> dict[str, Any]:
    return {
        "id": dlq_id,
        "step_id": step_id,
        "workflow_id": workflow_id,
        "workspace_id": workspace_id,
        "step_name": step_name,
        "attempts": attempts,
        "last_error": last_error,
        "failed_at": "2026-05-09T12:00:00+00:00",
        "step_snapshot": {"name": step_name, "input": {"q": "x"}},
    }


# ---------------------------------------------------------------------------
# Workflow create with per-step retry config


def test_create_with_dict_steps_caches_retry_config(
    ws: Workspace, workspace_mock: respx.MockRouter,
) -> None:
    """When steps are dicts, retry config is captured and used by start_step."""
    payload = make_workflow(
        wf_id="wf_01ABC",
        name="pipeline",
        steps_manifest=["search", "fetch"],
        metadata={},
    )
    workspace_mock.post(
        f"/v1/workspaces/{ws.id}/workflows"
    ).mock(return_value=httpx.Response(201, json=payload))

    wf = ws.workflows.create(
        "pipeline",
        steps=[
            {
                "name": "search",
                "max_attempts": 3,
                "retry_policy": "exponential",
                "retry_initial_delay_seconds": 2.0,
            },
            {"name": "fetch", "max_attempts": 5, "retry_policy": "exponential"},
        ],
    )
    assert isinstance(wf, WorkflowHandle)
    # Server still received a list of strings.
    sent = workspace_mock.calls.last.request
    body = sent.read().decode()
    import json as _json
    parsed = _json.loads(body)
    assert parsed["steps"] == ["search", "fetch"]


def test_start_step_forwards_retry_params(
    ws: Workspace, workspace_mock: respx.MockRouter,
) -> None:
    payload = make_workflow(
        wf_id="wf_01ABC",
        name="pipeline",
        steps_manifest=["search"],
    )
    workspace_mock.post(
        f"/v1/workspaces/{ws.id}/workflows"
    ).mock(return_value=httpx.Response(201, json=payload))
    wf = ws.workflows.create(
        "pipeline",
        steps=[
            {
                "name": "search",
                "max_attempts": 3,
                "retry_policy": "exponential",
            }
        ],
    )

    step_payload = make_workflow_step(
        step_id="step_01ABC",
        workflow_id=wf.id,
        name="search",
        status="pending",
    )
    step_payload["max_attempts"] = 3
    step_payload["retry_policy"] = "exponential"

    route = workspace_mock.post(
        f"/v1/workspaces/{ws.id}/workflows/{wf.id}/steps"
    ).mock(return_value=httpx.Response(201, json=step_payload))

    wf.start_step("search", initial_status="pending")

    assert route.called
    import json as _json
    parsed = _json.loads(route.calls.last.request.read().decode())
    assert parsed["max_attempts"] == 3
    assert parsed["retry_policy"] == "exponential"


def test_start_step_explicit_kwargs_override_cached_config(
    ws: Workspace, workspace_mock: respx.MockRouter,
) -> None:
    payload = make_workflow(
        wf_id="wf_01ABC",
        name="pipeline",
        steps_manifest=["search"],
    )
    workspace_mock.post(
        f"/v1/workspaces/{ws.id}/workflows"
    ).mock(return_value=httpx.Response(201, json=payload))
    wf = ws.workflows.create(
        "pipeline",
        steps=[{"name": "search", "max_attempts": 3, "retry_policy": "fixed"}],
    )

    step_payload = make_workflow_step(
        step_id="step_01ABC",
        workflow_id=wf.id,
        name="search",
        status="pending",
    )
    route = workspace_mock.post(
        f"/v1/workspaces/{ws.id}/workflows/{wf.id}/steps"
    ).mock(return_value=httpx.Response(201, json=step_payload))

    wf.start_step(
        "search",
        initial_status="pending",
        max_attempts=10,
        retry_policy="exponential",
    )
    import json as _json
    parsed = _json.loads(route.calls.last.request.read().decode())
    # Explicit kwargs win over the dict-step config.
    assert parsed["max_attempts"] == 10
    assert parsed["retry_policy"] == "exponential"


def test_start_step_with_string_manifest_omits_retry_params(
    ws: Workspace, workspace_mock: respx.MockRouter,
) -> None:
    """A list[str] manifest produces no retry config in start_step bodies."""
    payload = make_workflow(
        wf_id="wf_01ABC",
        name="pipeline",
        steps_manifest=["search"],
    )
    workspace_mock.post(
        f"/v1/workspaces/{ws.id}/workflows"
    ).mock(return_value=httpx.Response(201, json=payload))
    wf = ws.workflows.create("pipeline", steps=["search"])
    step_payload = make_workflow_step(
        step_id="step_01ABC",
        workflow_id=wf.id,
        name="search",
        status="running",
    )
    route = workspace_mock.post(
        f"/v1/workspaces/{ws.id}/workflows/{wf.id}/steps"
    ).mock(return_value=httpx.Response(201, json=step_payload))

    wf.start_step("search")
    import json as _json
    parsed = _json.loads(route.calls.last.request.read().decode())
    assert "max_attempts" not in parsed
    assert "retry_policy" not in parsed


# ---------------------------------------------------------------------------
# DLQ proxy methods


def test_dlq_lists_entries(
    ws: Workspace, workspace_mock: respx.MockRouter,
) -> None:
    payload = make_workflow(
        wf_id="wf_01ABC",
        name="pipeline",
        steps_manifest=["search"],
    )
    workspace_mock.post(
        f"/v1/workspaces/{ws.id}/workflows"
    ).mock(return_value=httpx.Response(201, json=payload))
    wf = ws.workflows.create("pipeline", steps=["search"])

    workspace_mock.get(
        f"/v1/workspaces/{ws.id}/workflows/{wf.id}/dlq"
    ).mock(
        return_value=httpx.Response(
            200, json={"entries": [_make_dlq_entry(dlq_id="dlqstep_a"), _make_dlq_entry(dlq_id="dlqstep_b")]},
        )
    )
    entries = wf.dlq()
    assert isinstance(entries, list)
    assert len(entries) == 2
    assert all(isinstance(e, DLQEntry) for e in entries)
    assert entries[0].id == "dlqstep_a"
    assert entries[0].step_name == "search"


def test_dlq_replay_returns_new_step(
    ws: Workspace, workspace_mock: respx.MockRouter,
) -> None:
    payload = make_workflow(
        wf_id="wf_01ABC",
        name="pipeline",
        steps_manifest=["search"],
    )
    workspace_mock.post(
        f"/v1/workspaces/{ws.id}/workflows"
    ).mock(return_value=httpx.Response(201, json=payload))
    wf = ws.workflows.create("pipeline", steps=["search"])

    new_step = make_workflow_step(
        step_id="step_NEW",
        workflow_id=wf.id,
        name="search",
        status="pending",
    )
    workspace_mock.post(
        f"/v1/workspaces/{ws.id}/workflows/{wf.id}/dlq/dlqstep_a/replay"
    ).mock(
        return_value=httpx.Response(
            200, json={"dlq_id": "dlqstep_a", "replayed_step": new_step}
        )
    )
    # The replay path also refreshes the workflow.
    workspace_mock.get(
        f"/v1/workspaces/{ws.id}/workflows/{wf.id}"
    ).mock(return_value=httpx.Response(200, json=payload))

    step = wf.replay_dlq("dlqstep_a")
    assert step.id == "step_NEW"
    assert step.status == "pending"


def test_dlq_delete_returns_none(
    ws: Workspace, workspace_mock: respx.MockRouter,
) -> None:
    payload = make_workflow(
        wf_id="wf_01ABC",
        name="pipeline",
        steps_manifest=["search"],
    )
    workspace_mock.post(
        f"/v1/workspaces/{ws.id}/workflows"
    ).mock(return_value=httpx.Response(201, json=payload))
    wf = ws.workflows.create("pipeline", steps=["search"])

    route = workspace_mock.delete(
        f"/v1/workspaces/{ws.id}/workflows/{wf.id}/dlq/dlqstep_a"
    ).mock(return_value=httpx.Response(204))

    result = wf.delete_dlq("dlqstep_a")
    assert result is None
    assert route.called


# ---------------------------------------------------------------------------
# Validation


def test_create_dict_step_without_name_raises(
    ws: Workspace, workspace_mock: respx.MockRouter,
) -> None:
    with pytest.raises(ValueError):
        ws.workflows.create("pipeline", steps=[{"max_attempts": 3}])  # type: ignore[list-item]


# ---------------------------------------------------------------------------
# Mixed string + dict manifest


def test_create_mixed_string_and_dict_steps(
    ws: Workspace, workspace_mock: respx.MockRouter,
) -> None:
    payload = make_workflow(
        wf_id="wf_01ABC",
        name="pipeline",
        steps_manifest=["search", "fetch"],
    )
    route = workspace_mock.post(
        f"/v1/workspaces/{ws.id}/workflows"
    ).mock(return_value=httpx.Response(201, json=payload))
    wf = ws.workflows.create(
        "pipeline",
        steps=["search", {"name": "fetch", "max_attempts": 4}],
    )
    import json as _json
    sent = _json.loads(route.calls.last.request.read().decode())
    assert sent["steps"] == ["search", "fetch"]

    # 'fetch' has cached config; 'search' does not.
    step_payload = make_workflow_step(
        step_id="step_01",
        workflow_id=wf.id,
        name="fetch",
        status="pending",
    )
    fetch_route = workspace_mock.post(
        f"/v1/workspaces/{ws.id}/workflows/{wf.id}/steps"
    ).mock(return_value=httpx.Response(201, json=step_payload))
    wf.start_step("fetch", initial_status="pending")
    parsed = _json.loads(fetch_route.calls.last.request.read().decode())
    assert parsed["max_attempts"] == 4
