# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 The Plinth Authors
"""Tests for the asana-mcp server endpoints + tools."""

from __future__ import annotations

import json as _json

import pytest
import respx
from httpx import Response

from asana_mcp.tools import parse_gid


WORKSPACE_GID = "12345"
PROJECT_GID = "67890"
TASK_GID = "11111"


def _auth_headers() -> dict[str, str]:
    return {"Authorization": "Bearer t"}


# ---------------------------------------------------------------------------
# parse_gid — input validation
# ---------------------------------------------------------------------------


def test_parse_gid_string() -> None:
    assert parse_gid("123456") == "123456"


def test_parse_gid_int() -> None:
    assert parse_gid(123456) == "123456"


def test_parse_gid_rejects_traversal() -> None:
    from asana_mcp.tools import ToolError

    with pytest.raises(ToolError):
        parse_gid("../etc")
    with pytest.raises(ToolError):
        parse_gid("12/34")


def test_parse_gid_rejects_non_numeric() -> None:
    from asana_mcp.tools import ToolError

    with pytest.raises(ToolError):
        parse_gid("abc")
    with pytest.raises(ToolError):
        parse_gid("12abc")


def test_parse_gid_rejects_empty() -> None:
    from asana_mcp.tools import ToolError

    with pytest.raises(ToolError):
        parse_gid("")
    with pytest.raises(ToolError):
        parse_gid(None)


# ---------------------------------------------------------------------------
# /healthz + /tools
# ---------------------------------------------------------------------------


async def test_healthz(client) -> None:
    resp = await client.get("/healthz")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ok"
    assert body["service"] == "asana-mcp"
    assert body["version"] == "1.5.0"


async def test_tools_listing_has_six_tools(client) -> None:
    resp = await client.get("/tools")
    assert resp.status_code == 200
    body = resp.json()
    assert len(body["tools"]) == 6
    tool_ids = {t["tool_id"] for t in body["tools"]}
    assert tool_ids == {
        "asana.list_workspaces",
        "asana.list_projects",
        "asana.list_tasks",
        "asana.get_task",
        "asana.create_task",
        "asana.update_task",
    }
    for t in body["tools"]:
        assert t["auth_method"] == "oauth2"
        assert t["auth_config"] == {"provider": "asana"}


# ---------------------------------------------------------------------------
# Auth — missing token
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "tool_id, payload",
    [
        ("asana.list_workspaces", {}),
        ("asana.list_projects", {"workspace_gid": WORKSPACE_GID}),
        ("asana.list_tasks", {"project_gid": PROJECT_GID}),
        ("asana.get_task", {"task_gid": TASK_GID}),
        ("asana.create_task", {"name": "Test", "workspace_gid": WORKSPACE_GID}),
        ("asana.update_task", {"task_gid": TASK_GID, "completed": True}),
    ],
)
async def test_unauthorized_without_bearer(client, tool_id: str, payload: dict) -> None:
    resp = await client.post(f"/invoke/{tool_id}", json=payload)
    assert resp.status_code == 401
    body = resp.json()
    assert body["error"]["code"] == "UNAUTHORIZED"


async def test_unauthorized_with_non_bearer_scheme(client) -> None:
    resp = await client.post(
        "/invoke/asana.list_workspaces",
        json={},
        headers={"Authorization": "Basic abc"},
    )
    assert resp.status_code == 401


# ---------------------------------------------------------------------------
# asana.list_workspaces
# ---------------------------------------------------------------------------


async def test_list_workspaces(client) -> None:
    captured: dict = {}

    def _capture(request):
        captured["auth"] = request.headers.get("Authorization")
        return Response(
            200,
            json={
                "data": [
                    {
                        "gid": WORKSPACE_GID,
                        "name": "Acme",
                        "resource_type": "workspace",
                    },
                    {
                        "gid": "999",
                        "name": "Beta",
                        "resource_type": "workspace",
                    },
                ]
            },
        )

    with respx.mock(assert_all_called=True) as mock:
        mock.get("https://app.asana.test/api/1.0/workspaces").mock(side_effect=_capture)
        resp = await client.post(
            "/invoke/asana.list_workspaces",
            json={},
            headers=_auth_headers(),
        )
    assert resp.status_code == 200
    assert captured["auth"] == "Bearer t"
    body = resp.json()["result"]
    assert body["count"] == 2
    assert body["workspaces"][0]["gid"] == WORKSPACE_GID
    assert body["workspaces"][0]["name"] == "Acme"


async def test_list_workspaces_empty(client) -> None:
    with respx.mock(assert_all_called=True) as mock:
        mock.get("https://app.asana.test/api/1.0/workspaces").mock(
            return_value=Response(200, json={"data": []})
        )
        resp = await client.post(
            "/invoke/asana.list_workspaces",
            json={},
            headers=_auth_headers(),
        )
    assert resp.status_code == 200
    assert resp.json()["result"]["count"] == 0


# ---------------------------------------------------------------------------
# asana.list_projects
# ---------------------------------------------------------------------------


async def test_list_projects(client) -> None:
    captured: dict = {}

    def _capture(request):
        captured["params"] = dict(request.url.params)
        return Response(
            200,
            json={
                "data": [
                    {
                        "gid": PROJECT_GID,
                        "name": "Backend",
                        "archived": False,
                        "workspace": {"gid": WORKSPACE_GID},
                        "resource_type": "project",
                    }
                ]
            },
        )

    with respx.mock(assert_all_called=True) as mock:
        mock.get("https://app.asana.test/api/1.0/projects").mock(side_effect=_capture)
        resp = await client.post(
            "/invoke/asana.list_projects",
            json={"workspace_gid": WORKSPACE_GID, "archived": False},
            headers=_auth_headers(),
        )
    assert resp.status_code == 200
    body = resp.json()["result"]
    assert body["count"] == 1
    assert body["projects"][0]["gid"] == PROJECT_GID
    assert captured["params"]["workspace"] == WORKSPACE_GID
    assert captured["params"]["archived"] == "false"


async def test_list_projects_requires_workspace_gid(client) -> None:
    resp = await client.post(
        "/invoke/asana.list_projects",
        json={},
        headers=_auth_headers(),
    )
    assert resp.status_code == 400


async def test_list_projects_archived_must_be_bool(client) -> None:
    resp = await client.post(
        "/invoke/asana.list_projects",
        json={"workspace_gid": WORKSPACE_GID, "archived": "yes"},
        headers=_auth_headers(),
    )
    assert resp.status_code == 400


# ---------------------------------------------------------------------------
# asana.list_tasks
# ---------------------------------------------------------------------------


async def test_list_tasks(client) -> None:
    captured: dict = {}

    def _capture(request):
        captured["params"] = dict(request.url.params)
        return Response(
            200,
            json={
                "data": [
                    {
                        "gid": TASK_GID,
                        "name": "Ship feature",
                        "completed": False,
                        "assignee": {"name": "Alice"},
                        "resource_type": "task",
                    }
                ]
            },
        )

    with respx.mock(assert_all_called=True) as mock:
        mock.get(
            f"https://app.asana.test/api/1.0/projects/{PROJECT_GID}/tasks"
        ).mock(side_effect=_capture)
        resp = await client.post(
            "/invoke/asana.list_tasks",
            json={"project_gid": PROJECT_GID, "limit": 10},
            headers=_auth_headers(),
        )
    assert resp.status_code == 200
    body = resp.json()["result"]
    assert body["count"] == 1
    assert body["tasks"][0]["assignee"] == "Alice"
    assert captured["params"]["limit"] == "10"


async def test_list_tasks_validates_limit(client) -> None:
    resp = await client.post(
        "/invoke/asana.list_tasks",
        json={"project_gid": PROJECT_GID, "limit": 0},
        headers=_auth_headers(),
    )
    assert resp.status_code == 400
    resp = await client.post(
        "/invoke/asana.list_tasks",
        json={"project_gid": PROJECT_GID, "limit": 200},
        headers=_auth_headers(),
    )
    assert resp.status_code == 400


# ---------------------------------------------------------------------------
# asana.get_task
# ---------------------------------------------------------------------------


async def test_get_task(client) -> None:
    with respx.mock(assert_all_called=True) as mock:
        mock.get(f"https://app.asana.test/api/1.0/tasks/{TASK_GID}").mock(
            return_value=Response(
                200,
                json={
                    "data": {
                        "gid": TASK_GID,
                        "name": "Ship feature",
                        "completed": False,
                        "due_on": "2026-06-01",
                        "notes": "Plan",
                        "permalink_url": "https://app.asana.com/x",
                        "assignee": {"name": "Alice"},
                        "projects": [{"gid": PROJECT_GID, "name": "Backend"}],
                        "resource_type": "task",
                    }
                },
            )
        )
        resp = await client.post(
            "/invoke/asana.get_task",
            json={"task_gid": TASK_GID},
            headers=_auth_headers(),
        )
    assert resp.status_code == 200
    body = resp.json()["result"]
    assert body["gid"] == TASK_GID
    assert body["due_on"] == "2026-06-01"
    assert body["assignee"] == "Alice"
    assert body["projects"][0]["gid"] == PROJECT_GID


async def test_get_task_404(client) -> None:
    with respx.mock(assert_all_called=True) as mock:
        mock.get(f"https://app.asana.test/api/1.0/tasks/{TASK_GID}").mock(
            return_value=Response(404, json={"errors": [{"message": "not found"}]})
        )
        resp = await client.post(
            "/invoke/asana.get_task",
            json={"task_gid": TASK_GID},
            headers=_auth_headers(),
        )
    assert resp.status_code == 404


async def test_get_task_validates_gid(client) -> None:
    resp = await client.post(
        "/invoke/asana.get_task",
        json={"task_gid": "abc"},
        headers=_auth_headers(),
    )
    assert resp.status_code == 400


# ---------------------------------------------------------------------------
# asana.create_task
# ---------------------------------------------------------------------------


async def test_create_task_in_workspace(client) -> None:
    captured: dict = {}

    def _capture(request):
        captured["body"] = request.read()
        return Response(
            201,
            json={
                "data": {
                    "gid": "new-1",
                    "name": "Test",
                    "completed": False,
                    "resource_type": "task",
                }
            },
        )

    with respx.mock(assert_all_called=True) as mock:
        mock.post("https://app.asana.test/api/1.0/tasks").mock(side_effect=_capture)
        resp = await client.post(
            "/invoke/asana.create_task",
            json={
                "name": "Test",
                "workspace_gid": WORKSPACE_GID,
                "notes": "hello",
            },
            headers=_auth_headers(),
        )
    assert resp.status_code == 200
    sent = _json.loads(captured["body"])
    assert sent["data"]["name"] == "Test"
    assert sent["data"]["workspace"] == WORKSPACE_GID
    assert sent["data"]["notes"] == "hello"


async def test_create_task_in_projects(client) -> None:
    captured: dict = {}

    def _capture(request):
        captured["body"] = request.read()
        return Response(
            201,
            json={"data": {"gid": "new-2", "name": "Test", "completed": False}},
        )

    with respx.mock(assert_all_called=True) as mock:
        mock.post("https://app.asana.test/api/1.0/tasks").mock(side_effect=_capture)
        resp = await client.post(
            "/invoke/asana.create_task",
            json={
                "name": "Test",
                "project_gids": [PROJECT_GID],
                "due_on": "2026-06-01",
            },
            headers=_auth_headers(),
        )
    assert resp.status_code == 200
    sent = _json.loads(captured["body"])
    assert sent["data"]["projects"] == [PROJECT_GID]
    assert sent["data"]["due_on"] == "2026-06-01"


async def test_create_task_requires_name(client) -> None:
    resp = await client.post(
        "/invoke/asana.create_task",
        json={"workspace_gid": WORKSPACE_GID},
        headers=_auth_headers(),
    )
    assert resp.status_code == 400


async def test_create_task_requires_workspace_or_projects(client) -> None:
    resp = await client.post(
        "/invoke/asana.create_task",
        json={"name": "x"},
        headers=_auth_headers(),
    )
    assert resp.status_code == 400


async def test_create_task_rejects_both_workspace_and_projects(client) -> None:
    resp = await client.post(
        "/invoke/asana.create_task",
        json={
            "name": "x",
            "workspace_gid": WORKSPACE_GID,
            "project_gids": [PROJECT_GID],
        },
        headers=_auth_headers(),
    )
    assert resp.status_code == 400


async def test_create_task_rejects_empty_project_list(client) -> None:
    resp = await client.post(
        "/invoke/asana.create_task",
        json={"name": "x", "project_gids": []},
        headers=_auth_headers(),
    )
    assert resp.status_code == 400


# ---------------------------------------------------------------------------
# asana.update_task
# ---------------------------------------------------------------------------


async def test_update_task_completed(client) -> None:
    captured: dict = {}

    def _capture(request):
        captured["body"] = request.read()
        return Response(
            200,
            json={"data": {"gid": TASK_GID, "name": "x", "completed": True}},
        )

    with respx.mock(assert_all_called=True) as mock:
        mock.put(f"https://app.asana.test/api/1.0/tasks/{TASK_GID}").mock(
            side_effect=_capture
        )
        resp = await client.post(
            "/invoke/asana.update_task",
            json={"task_gid": TASK_GID, "completed": True},
            headers=_auth_headers(),
        )
    assert resp.status_code == 200
    body = resp.json()["result"]
    assert body["completed"] is True
    sent = _json.loads(captured["body"])
    assert sent["data"]["completed"] is True


async def test_update_task_requires_a_field(client) -> None:
    resp = await client.post(
        "/invoke/asana.update_task",
        json={"task_gid": TASK_GID},
        headers=_auth_headers(),
    )
    assert resp.status_code == 400


async def test_update_task_completed_must_be_bool(client) -> None:
    resp = await client.post(
        "/invoke/asana.update_task",
        json={"task_gid": TASK_GID, "completed": "yes"},
        headers=_auth_headers(),
    )
    assert resp.status_code == 400


async def test_update_task_propagates_401(client) -> None:
    with respx.mock(assert_all_called=True) as mock:
        mock.put(f"https://app.asana.test/api/1.0/tasks/{TASK_GID}").mock(
            return_value=Response(401, json={"errors": [{"message": "unauthorized"}]})
        )
        resp = await client.post(
            "/invoke/asana.update_task",
            json={"task_gid": TASK_GID, "completed": True},
            headers=_auth_headers(),
        )
    assert resp.status_code == 401
    assert resp.json()["error"]["code"] == "UNAUTHORIZED"


# ---------------------------------------------------------------------------
# Unknown tool / malformed body
# ---------------------------------------------------------------------------


async def test_unknown_tool_returns_404(client) -> None:
    resp = await client.post(
        "/invoke/asana.does_not_exist",
        json={},
        headers=_auth_headers(),
    )
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "TOOL_NOT_FOUND"


async def test_invalid_json_body_returns_400(client) -> None:
    resp = await client.post(
        "/invoke/asana.list_workspaces",
        content=b"not json",
        headers={
            **_auth_headers(),
            "Content-Type": "application/json",
        },
    )
    assert resp.status_code == 400


async def test_array_body_rejected(client) -> None:
    resp = await client.post(
        "/invoke/asana.list_workspaces",
        json=["not", "an", "object"],
        headers=_auth_headers(),
    )
    assert resp.status_code == 400
