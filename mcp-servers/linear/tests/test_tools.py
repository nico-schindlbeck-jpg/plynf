# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 The Plinth Authors
"""Tests for the linear-mcp server endpoints + tools."""

from __future__ import annotations

import json

import pytest
import respx
from httpx import Response


# ---------------------------------------------------------------------------
# /healthz + /tools
# ---------------------------------------------------------------------------


async def test_healthz(client) -> None:
    resp = await client.get("/healthz")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ok"
    assert body["service"] == "linear-mcp"
    assert body["version"] == "0.4.0"


async def test_tools_listing_has_five_tools(client) -> None:
    resp = await client.get("/tools")
    assert resp.status_code == 200
    body = resp.json()
    assert len(body["tools"]) == 5
    tool_ids = {t["tool_id"] for t in body["tools"]}
    assert tool_ids == {
        "linear.list_issues",
        "linear.get_issue",
        "linear.create_issue",
        "linear.update_issue",
        "linear.comment_on_issue",
    }
    for t in body["tools"]:
        assert t["auth_method"] == "oauth2"
        assert t["auth_config"] == {"provider": "linear"}


# ---------------------------------------------------------------------------
# Auth — missing token
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "tool_id, payload",
    [
        ("linear.list_issues", {}),
        ("linear.get_issue", {"id": "issue-1"}),
        ("linear.create_issue", {"team_id": "team-1", "title": "x"}),
        ("linear.update_issue", {"id": "issue-1", "title": "x"}),
        ("linear.comment_on_issue", {"issue_id": "issue-1", "body": "hi"}),
    ],
)
async def test_unauthorized_without_bearer(client, tool_id: str, payload: dict) -> None:
    resp = await client.post(f"/invoke/{tool_id}", json=payload)
    assert resp.status_code == 401
    assert resp.json()["error"]["code"] == "UNAUTHORIZED"


async def test_unauthorized_with_non_bearer_scheme(client) -> None:
    resp = await client.post(
        "/invoke/linear.list_issues",
        json={},
        headers={"Authorization": "Basic abc"},
    )
    assert resp.status_code == 401


# ---------------------------------------------------------------------------
# linear.list_issues
# ---------------------------------------------------------------------------


async def test_list_issues_returns_slimmed_nodes(client) -> None:
    captured: dict = {}

    def _capture(request):
        captured["body"] = request.read()
        captured["auth"] = request.headers.get("Authorization")
        return Response(
            200,
            json={
                "data": {
                    "issues": {
                        "nodes": [
                            {
                                "id": "iss-1",
                                "identifier": "ENG-1",
                                "title": "First",
                                "description": "...",
                                "url": "https://linear.app/x/iss-1",
                                "priority": 2,
                                "createdAt": "2026-01-01T00:00:00Z",
                                "updatedAt": "2026-01-01T00:00:00Z",
                                "completedAt": None,
                                "state": {"id": "s1", "name": "Todo", "type": "unstarted"},
                                "assignee": {"id": "u1", "name": "Alice"},
                                "team": {"id": "t1", "key": "ENG", "name": "Eng"},
                            },
                        ],
                        "pageInfo": {"hasNextPage": False, "endCursor": None},
                    }
                }
            },
        )

    with respx.mock(assert_all_called=True) as mock:
        mock.post("https://linear.test/graphql").mock(side_effect=_capture)
        resp = await client.post(
            "/invoke/linear.list_issues",
            json={"first": 5},
            headers={"Authorization": "Bearer lin-test"},
        )
    assert resp.status_code == 200
    result = resp.json()["result"]
    assert result["count"] == 1
    assert result["issues"][0]["state"] == "Todo"
    assert result["issues"][0]["team"]["key"] == "ENG"
    assert result["has_next_page"] is False
    # The forwarded GraphQL body should embed our query + variables.
    assert captured["auth"] == "Bearer lin-test"
    body = json.loads(captured["body"])
    assert "issues" in body["query"]
    assert body["variables"]["first"] == 5


async def test_list_issues_passes_filters(client) -> None:
    captured: dict = {}

    def _capture(request):
        captured["body"] = request.read()
        return Response(
            200,
            json={
                "data": {
                    "issues": {"nodes": [], "pageInfo": {"hasNextPage": False, "endCursor": None}}
                }
            },
        )

    with respx.mock(assert_all_called=True) as mock:
        mock.post("https://linear.test/graphql").mock(side_effect=_capture)
        resp = await client.post(
            "/invoke/linear.list_issues",
            json={"team_id": "team-uuid", "assignee_id": "user-uuid", "state": "In Progress"},
            headers={"Authorization": "Bearer t"},
        )
    assert resp.status_code == 200
    body = json.loads(captured["body"])
    filt = body["variables"]["filter"]
    assert filt["team"] == {"id": {"eq": "team-uuid"}}
    assert filt["assignee"] == {"id": {"eq": "user-uuid"}}
    assert filt["state"] == {"name": {"eq": "In Progress"}}


async def test_list_issues_invalid_first(client) -> None:
    resp = await client.post(
        "/invoke/linear.list_issues",
        json={"first": 999},
        headers={"Authorization": "Bearer t"},
    )
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "INVALID_ARGUMENTS"


# ---------------------------------------------------------------------------
# linear.get_issue
# ---------------------------------------------------------------------------


async def test_get_issue_returns_issue_and_comments(client) -> None:
    with respx.mock(assert_all_called=True) as mock:
        mock.post("https://linear.test/graphql").mock(
            return_value=Response(
                200,
                json={
                    "data": {
                        "issue": {
                            "id": "iss-9",
                            "identifier": "ENG-9",
                            "title": "Crash",
                            "description": "boom",
                            "url": "https://linear.app/x/iss-9",
                            "priority": 1,
                            "createdAt": "2026",
                            "updatedAt": "2026",
                            "completedAt": None,
                            "state": {"id": "s2", "name": "In Progress", "type": "started"},
                            "assignee": {"id": "u1", "name": "Alice"},
                            "team": {"id": "t1", "key": "ENG", "name": "Eng"},
                            "comments": {
                                "nodes": [
                                    {
                                        "id": "c1",
                                        "body": "+1",
                                        "createdAt": "2026",
                                        "url": "https://linear.app/x/c1",
                                        "user": {"id": "u2", "name": "Bob"},
                                    }
                                ]
                            },
                        }
                    }
                },
            )
        )
        resp = await client.post(
            "/invoke/linear.get_issue",
            json={"id": "iss-9"},
            headers={"Authorization": "Bearer t"},
        )
    assert resp.status_code == 200
    body = resp.json()["result"]
    assert body["issue"]["id"] == "iss-9"
    assert body["issue"]["state"] == "In Progress"
    assert len(body["comments"]) == 1
    assert body["comments"][0]["user"]["name"] == "Bob"


async def test_get_issue_validates_id(client) -> None:
    resp = await client.post(
        "/invoke/linear.get_issue",
        json={"id": ""},
        headers={"Authorization": "Bearer t"},
    )
    assert resp.status_code == 400


async def test_get_issue_rejects_bad_id_chars(client) -> None:
    resp = await client.post(
        "/invoke/linear.get_issue",
        json={"id": "../etc/passwd"},
        headers={"Authorization": "Bearer t"},
    )
    assert resp.status_code == 400


# ---------------------------------------------------------------------------
# linear.create_issue
# ---------------------------------------------------------------------------


async def test_create_issue_sends_input_payload(client) -> None:
    captured: dict = {}

    def _capture(request):
        captured["body"] = request.read()
        return Response(
            200,
            json={
                "data": {
                    "issueCreate": {
                        "success": True,
                        "issue": {
                            "id": "new-1",
                            "identifier": "ENG-100",
                            "title": "Hello",
                            "description": "world",
                            "url": "https://linear.app/x/new-1",
                            "priority": 0,
                            "createdAt": "2026",
                            "updatedAt": "2026",
                            "completedAt": None,
                            "state": {"id": "s", "name": "Todo", "type": "unstarted"},
                            "assignee": None,
                            "team": {"id": "t", "key": "ENG", "name": "Eng"},
                        },
                    }
                }
            },
        )

    with respx.mock(assert_all_called=True) as mock:
        mock.post("https://linear.test/graphql").mock(side_effect=_capture)
        resp = await client.post(
            "/invoke/linear.create_issue",
            json={
                "team_id": "team-1",
                "title": "Hello",
                "description": "world",
                "priority": 2,
            },
            headers={"Authorization": "Bearer t"},
        )
    assert resp.status_code == 200
    sent = json.loads(captured["body"])
    inp = sent["variables"]["input"]
    assert inp == {
        "teamId": "team-1",
        "title": "Hello",
        "description": "world",
        "priority": 2,
    }
    assert resp.json()["result"]["issue"]["identifier"] == "ENG-100"


async def test_create_issue_requires_title(client) -> None:
    resp = await client.post(
        "/invoke/linear.create_issue",
        json={"team_id": "team-1", "title": "  "},
        headers={"Authorization": "Bearer t"},
    )
    assert resp.status_code == 400


async def test_create_issue_failure_propagates(client) -> None:
    """If success=False the tool must surface a TOOL_INVOCATION_FAILED."""
    with respx.mock(assert_all_called=True) as mock:
        mock.post("https://linear.test/graphql").mock(
            return_value=Response(
                200,
                json={"data": {"issueCreate": {"success": False, "issue": None}}},
            )
        )
        resp = await client.post(
            "/invoke/linear.create_issue",
            json={"team_id": "team-1", "title": "x"},
            headers={"Authorization": "Bearer t"},
        )
    assert resp.status_code == 502
    assert resp.json()["error"]["code"] == "TOOL_INVOCATION_FAILED"


# ---------------------------------------------------------------------------
# linear.update_issue
# ---------------------------------------------------------------------------


async def test_update_issue_sends_id_and_input(client) -> None:
    captured: dict = {}

    def _capture(request):
        captured["body"] = request.read()
        return Response(
            200,
            json={
                "data": {
                    "issueUpdate": {
                        "success": True,
                        "issue": {
                            "id": "iss-3",
                            "identifier": "ENG-3",
                            "title": "new",
                            "description": None,
                            "url": "https://linear.app/x/iss-3",
                            "priority": 0,
                            "createdAt": "2026",
                            "updatedAt": "2026",
                            "completedAt": None,
                            "state": {"id": "sd", "name": "Done", "type": "completed"},
                            "assignee": None,
                            "team": {"id": "t", "key": "ENG", "name": "Eng"},
                        },
                    }
                }
            },
        )

    with respx.mock(assert_all_called=True) as mock:
        mock.post("https://linear.test/graphql").mock(side_effect=_capture)
        resp = await client.post(
            "/invoke/linear.update_issue",
            json={"id": "iss-3", "title": "new", "state_id": "state-done"},
            headers={"Authorization": "Bearer t"},
        )
    assert resp.status_code == 200
    sent = json.loads(captured["body"])
    assert sent["variables"]["id"] == "iss-3"
    assert sent["variables"]["input"] == {"title": "new", "stateId": "state-done"}
    assert resp.json()["result"]["issue"]["state"] == "Done"


async def test_update_issue_requires_a_field(client) -> None:
    resp = await client.post(
        "/invoke/linear.update_issue",
        json={"id": "iss-3"},
        headers={"Authorization": "Bearer t"},
    )
    assert resp.status_code == 400


# ---------------------------------------------------------------------------
# linear.comment_on_issue
# ---------------------------------------------------------------------------


async def test_comment_on_issue_posts_comment(client) -> None:
    captured: dict = {}

    def _capture(request):
        captured["body"] = request.read()
        return Response(
            200,
            json={
                "data": {
                    "commentCreate": {
                        "success": True,
                        "comment": {
                            "id": "c-99",
                            "body": "thanks",
                            "createdAt": "2026",
                            "url": "https://linear.app/x/c-99",
                            "user": {"id": "u", "name": "Me"},
                        },
                    }
                }
            },
        )

    with respx.mock(assert_all_called=True) as mock:
        mock.post("https://linear.test/graphql").mock(side_effect=_capture)
        resp = await client.post(
            "/invoke/linear.comment_on_issue",
            json={"issue_id": "iss-7", "body": "thanks"},
            headers={"Authorization": "Bearer t"},
        )
    assert resp.status_code == 200
    sent = json.loads(captured["body"])
    inp = sent["variables"]["input"]
    assert inp == {"issueId": "iss-7", "body": "thanks"}
    assert resp.json()["result"]["comment"]["id"] == "c-99"


async def test_comment_requires_body(client) -> None:
    resp = await client.post(
        "/invoke/linear.comment_on_issue",
        json={"issue_id": "iss-7", "body": "  "},
        headers={"Authorization": "Bearer t"},
    )
    assert resp.status_code == 400


# ---------------------------------------------------------------------------
# GraphQL error envelope
# ---------------------------------------------------------------------------


async def test_graphql_errors_translate_to_tool_error(client) -> None:
    with respx.mock(assert_all_called=True) as mock:
        mock.post("https://linear.test/graphql").mock(
            return_value=Response(
                200,
                json={
                    "errors": [{"message": "Entity not found"}],
                    "data": None,
                },
            )
        )
        resp = await client.post(
            "/invoke/linear.get_issue",
            json={"id": "missing"},
            headers={"Authorization": "Bearer t"},
        )
    assert resp.status_code == 400
    body = resp.json()
    assert body["error"]["code"] == "TOOL_INVOCATION_FAILED"
    assert "Entity not found" in body["error"]["message"]


async def test_graphql_authentication_error_maps_to_401(client) -> None:
    with respx.mock(assert_all_called=True) as mock:
        mock.post("https://linear.test/graphql").mock(
            return_value=Response(
                200,
                json={
                    "errors": [
                        {
                            "message": "Authentication required",
                            "extensions": {"code": "AUTHENTICATION_ERROR"},
                        }
                    ],
                    "data": None,
                },
            )
        )
        resp = await client.post(
            "/invoke/linear.list_issues",
            json={},
            headers={"Authorization": "Bearer bad"},
        )
    assert resp.status_code == 401
    assert resp.json()["error"]["code"] == "UNAUTHORIZED"


async def test_http_500_translates_to_502_tool_invocation_failed(client) -> None:
    with respx.mock(assert_all_called=True) as mock:
        mock.post("https://linear.test/graphql").mock(
            return_value=Response(500, text="boom")
        )
        resp = await client.post(
            "/invoke/linear.list_issues",
            json={},
            headers={"Authorization": "Bearer t"},
        )
    assert resp.status_code == 502
    assert resp.json()["error"]["code"] == "TOOL_INVOCATION_FAILED"


# ---------------------------------------------------------------------------
# Generic / unknown / bad input
# ---------------------------------------------------------------------------


async def test_unknown_tool_returns_404(client) -> None:
    resp = await client.post(
        "/invoke/linear.does_not_exist",
        json={},
        headers={"Authorization": "Bearer t"},
    )
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "TOOL_NOT_FOUND"


async def test_invalid_json_body_returns_400(client) -> None:
    resp = await client.post(
        "/invoke/linear.list_issues",
        content=b"not json",
        headers={
            "Authorization": "Bearer t",
            "Content-Type": "application/json",
        },
    )
    assert resp.status_code == 400


async def test_array_body_rejected(client) -> None:
    resp = await client.post(
        "/invoke/linear.list_issues",
        json=["not an object"],
        headers={"Authorization": "Bearer t"},
    )
    assert resp.status_code == 400
