# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 The Plinth Authors
"""Tests for the v1.5 Plinth Studio import endpoint.

Covers:

* Happy path → workflow created with manifest matching the definition
* Definition stored verbatim under ``workflow.metadata['definition']``
* Validation errors (missing name, missing steps, bad step type, dup names)
* Tool-id references are accepted without registry resolution
* The imported workflow can run through the normal step lifecycle
"""

from __future__ import annotations

import httpx


# ---------------------------------------------------------------------------
# Helpers


def _basic_definition() -> dict:
    """Smallest valid Studio definition."""
    return {
        "name": "lead-research-pipeline",
        "description": "Research a lead and extract facts.",
        "retry_policy": "exponential",
        "max_attempts_default": 3,
        "steps": [
            {
                "name": "search",
                "type": "tool",
                "tool_id": "web.search",
                "arguments_template": {"query": "{input.topic}", "k": 5},
                "max_attempts": 3,
            },
            {
                "name": "extract",
                "type": "llm",
                "model": "claude-sonnet-4-5",
                "system": "You are a research assistant.",
                "prompt_template": "Extract facts from:\n{step.search.output}",
                "max_attempts": 2,
            },
        ],
    }


# ---------------------------------------------------------------------------
# Happy path


async def test_import_workflow_basic(
    client: httpx.AsyncClient, workspace_id: str
) -> None:
    """A well-formed definition creates a workflow whose manifest matches."""
    resp = await client.post(
        f"/v1/workspaces/{workspace_id}/workflows/import",
        json=_basic_definition(),
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["id"].startswith("wf_")
    assert body["name"] == "lead-research-pipeline"
    assert body["status"] == "pending"
    assert body["steps_manifest"] == ["search", "extract"]
    assert body["steps"] == []


async def test_import_workflow_stores_definition_in_metadata(
    client: httpx.AsyncClient, workspace_id: str
) -> None:
    """The full definition lands at ``metadata['definition']`` for round-trip."""
    definition = _basic_definition()
    resp = await client.post(
        f"/v1/workspaces/{workspace_id}/workflows/import",
        json=definition,
    )
    assert resp.status_code == 201
    body = resp.json()
    md = body["metadata"]
    assert md["imported_via"] == "plinth-studio"
    assert md["definition"] == definition
    # The full definition is reproducible from the metadata.
    assert md["definition"]["steps"][0]["tool_id"] == "web.search"
    assert md["definition"]["steps"][1]["model"] == "claude-sonnet-4-5"


async def test_import_workflow_appears_in_list(
    client: httpx.AsyncClient, workspace_id: str
) -> None:
    """An imported workflow shows up in the workspace's workflow list."""
    resp = await client.post(
        f"/v1/workspaces/{workspace_id}/workflows/import",
        json=_basic_definition(),
    )
    assert resp.status_code == 201
    wf_id = resp.json()["id"]

    listing = await client.get(f"/v1/workspaces/{workspace_id}/workflows")
    assert listing.status_code == 200
    rows = listing.json()["workflows"]
    assert any(w["id"] == wf_id for w in rows)


async def test_import_workflow_then_get(
    client: httpx.AsyncClient, workspace_id: str
) -> None:
    """The imported workflow is retrievable via the standard GET endpoint."""
    create = await client.post(
        f"/v1/workspaces/{workspace_id}/workflows/import",
        json=_basic_definition(),
    )
    wf_id = create.json()["id"]
    resp = await client.get(f"/v1/workspaces/{workspace_id}/workflows/{wf_id}")
    assert resp.status_code == 200
    body = resp.json()
    assert body["steps_manifest"] == ["search", "extract"]
    # The definition round-trips through GET as well.
    assert body["metadata"]["definition"]["name"] == "lead-research-pipeline"


# ---------------------------------------------------------------------------
# Imported workflows are runnable


async def test_imported_workflow_can_run_steps(
    client: httpx.AsyncClient, workspace_id: str
) -> None:
    """Imported workflows accept the normal step lifecycle (no special path)."""
    create = await client.post(
        f"/v1/workspaces/{workspace_id}/workflows/import",
        json=_basic_definition(),
    )
    wf_id = create.json()["id"]

    # Start the first step.
    step_resp = await client.post(
        f"/v1/workspaces/{workspace_id}/workflows/{wf_id}/steps",
        json={"name": "search", "input": {"topic": "fisheries"}},
    )
    assert step_resp.status_code == 201, step_resp.text
    step_id = step_resp.json()["id"]
    assert step_resp.json()["status"] == "running"

    # Complete it.
    upd = await client.patch(
        f"/v1/workspaces/{workspace_id}/workflows/{wf_id}/steps/{step_id}",
        json={"status": "completed", "output": {"hits": 5}},
    )
    assert upd.status_code == 200
    assert upd.json()["status"] == "completed"


async def test_imported_workflow_status_progresses(
    client: httpx.AsyncClient, workspace_id: str
) -> None:
    """Workflow progresses through running → completed using imported manifest."""
    create = await client.post(
        f"/v1/workspaces/{workspace_id}/workflows/import",
        json={
            "name": "tiny",
            "steps": [
                {"name": "a", "type": "tool", "tool_id": "x"},
            ],
        },
    )
    wf_id = create.json()["id"]
    sresp = await client.post(
        f"/v1/workspaces/{workspace_id}/workflows/{wf_id}/steps",
        json={"name": "a"},
    )
    sid = sresp.json()["id"]
    await client.patch(
        f"/v1/workspaces/{workspace_id}/workflows/{wf_id}/steps/{sid}",
        json={"status": "completed", "output": "done"},
    )
    final = await client.get(
        f"/v1/workspaces/{workspace_id}/workflows/{wf_id}"
    )
    assert final.json()["status"] == "completed"


# ---------------------------------------------------------------------------
# Validation errors


async def test_import_missing_name_returns_400(
    client: httpx.AsyncClient, workspace_id: str
) -> None:
    resp = await client.post(
        f"/v1/workspaces/{workspace_id}/workflows/import",
        json={"steps": [{"name": "a", "type": "tool"}]},
    )
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "INVALID_ARGUMENTS"


async def test_import_empty_name_returns_400(
    client: httpx.AsyncClient, workspace_id: str
) -> None:
    resp = await client.post(
        f"/v1/workspaces/{workspace_id}/workflows/import",
        json={"name": "   ", "steps": [{"name": "a", "type": "tool"}]},
    )
    assert resp.status_code == 400


async def test_import_missing_steps_returns_400(
    client: httpx.AsyncClient, workspace_id: str
) -> None:
    resp = await client.post(
        f"/v1/workspaces/{workspace_id}/workflows/import",
        json={"name": "wf"},
    )
    assert resp.status_code == 400


async def test_import_empty_steps_returns_400(
    client: httpx.AsyncClient, workspace_id: str
) -> None:
    resp = await client.post(
        f"/v1/workspaces/{workspace_id}/workflows/import",
        json={"name": "wf", "steps": []},
    )
    assert resp.status_code == 400


async def test_import_step_missing_name_returns_400(
    client: httpx.AsyncClient, workspace_id: str
) -> None:
    resp = await client.post(
        f"/v1/workspaces/{workspace_id}/workflows/import",
        json={"name": "wf", "steps": [{"type": "tool"}]},
    )
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "INVALID_ARGUMENTS"


async def test_import_invalid_step_type_returns_400(
    client: httpx.AsyncClient, workspace_id: str
) -> None:
    resp = await client.post(
        f"/v1/workspaces/{workspace_id}/workflows/import",
        json={
            "name": "wf",
            "steps": [{"name": "a", "type": "magic-mystery"}],
        },
    )
    assert resp.status_code == 400
    body = resp.json()
    assert body["error"]["code"] == "INVALID_ARGUMENTS"
    assert "magic-mystery" in body["error"]["message"]


async def test_import_duplicate_step_names_returns_400(
    client: httpx.AsyncClient, workspace_id: str
) -> None:
    resp = await client.post(
        f"/v1/workspaces/{workspace_id}/workflows/import",
        json={
            "name": "wf",
            "steps": [
                {"name": "a", "type": "tool"},
                {"name": "a", "type": "llm"},
            ],
        },
    )
    assert resp.status_code == 400


async def test_import_unknown_workspace_returns_404(
    client: httpx.AsyncClient,
) -> None:
    resp = await client.post(
        "/v1/workspaces/ws_nope/workflows/import",
        json=_basic_definition(),
    )
    assert resp.status_code == 404


async def test_import_step_without_type_is_accepted(
    client: httpx.AsyncClient, workspace_id: str
) -> None:
    """Step ``type`` is optional — pre-typed legacy definitions still import."""
    resp = await client.post(
        f"/v1/workspaces/{workspace_id}/workflows/import",
        json={
            "name": "wf",
            "steps": [{"name": "step1"}, {"name": "step2"}],
        },
    )
    assert resp.status_code == 201
    body = resp.json()
    assert body["steps_manifest"] == ["step1", "step2"]


# ---------------------------------------------------------------------------
# Tool-id references not validated by workspace


async def test_import_unknown_tool_id_accepted(
    client: httpx.AsyncClient, workspace_id: str
) -> None:
    """Workspace does not resolve tool_id against the gateway registry.

    Any non-empty string passes through; the failure (if any) surfaces at
    run time when the worker tries to invoke the tool. This keeps the
    workspace decoupled from the gateway's tool catalog.
    """
    resp = await client.post(
        f"/v1/workspaces/{workspace_id}/workflows/import",
        json={
            "name": "with-bogus-tool",
            "steps": [
                {
                    "name": "search",
                    "type": "tool",
                    "tool_id": "definitely.not.a.real.tool",
                    "arguments_template": {},
                }
            ],
        },
    )
    assert resp.status_code == 201
    body = resp.json()
    assert body["status"] == "pending"
    md = body["metadata"]["definition"]["steps"][0]
    assert md["tool_id"] == "definitely.not.a.real.tool"


# ---------------------------------------------------------------------------
# Body parsing


async def test_import_invalid_json_body_returns_400(
    client: httpx.AsyncClient, workspace_id: str
) -> None:
    """A non-JSON body is rejected with INVALID_ARGUMENTS."""
    resp = await client.post(
        f"/v1/workspaces/{workspace_id}/workflows/import",
        content=b"not json",
        headers={"Content-Type": "application/json"},
    )
    assert resp.status_code == 400


async def test_import_array_body_returns_400(
    client: httpx.AsyncClient, workspace_id: str
) -> None:
    """An array body (instead of an object) is rejected."""
    resp = await client.post(
        f"/v1/workspaces/{workspace_id}/workflows/import",
        json=["definitely", "not", "an", "object"],
    )
    assert resp.status_code == 400


# ---------------------------------------------------------------------------
# Channel + manual step types are valid


async def test_import_channel_step_types(
    client: httpx.AsyncClient, workspace_id: str
) -> None:
    """All four supported step types pass validation."""
    resp = await client.post(
        f"/v1/workspaces/{workspace_id}/workflows/import",
        json={
            "name": "channels-flow",
            "steps": [
                {"name": "send", "type": "channel_send", "channel": "out"},
                {"name": "recv", "type": "channel_receive", "channel": "in"},
                {"name": "approve", "type": "manual"},
            ],
        },
    )
    assert resp.status_code == 201
    body = resp.json()
    assert body["steps_manifest"] == ["send", "recv", "approve"]
