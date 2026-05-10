# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 The Plinth Authors
"""Tests for the atlassian-mcp server endpoints + tools."""

from __future__ import annotations

import json as _json

import pytest
import respx
from httpx import Response

from atlassian_mcp.tools import parse_issue_key, parse_page_id


CLOUDID = "abc-123-cloudid"
ISSUE_KEY = "PLI-42"


def _auth_headers(cloudid: str | None = CLOUDID) -> dict[str, str]:
    headers = {"Authorization": "Bearer t"}
    if cloudid is not None:
        headers["X-Plinth-OAuth-Cloudid"] = cloudid
    return headers


# ---------------------------------------------------------------------------
# parse_issue_key — input validation
# ---------------------------------------------------------------------------


def test_parse_issue_key_valid() -> None:
    assert parse_issue_key("PLI-42") == "PLI-42"
    assert parse_issue_key("ABC123-1") == "ABC123-1"


def test_parse_issue_key_rejects_lowercase() -> None:
    from atlassian_mcp.tools import ToolError

    with pytest.raises(ToolError):
        parse_issue_key("pli-42")


def test_parse_issue_key_rejects_empty() -> None:
    from atlassian_mcp.tools import ToolError

    with pytest.raises(ToolError):
        parse_issue_key("")
    with pytest.raises(ToolError):
        parse_issue_key(None)


def test_parse_issue_key_rejects_traversal() -> None:
    from atlassian_mcp.tools import ToolError

    with pytest.raises(ToolError):
        parse_issue_key("../etc")
    with pytest.raises(ToolError):
        parse_issue_key("PLI/42")


def test_parse_issue_key_rejects_no_dash() -> None:
    from atlassian_mcp.tools import ToolError

    with pytest.raises(ToolError):
        parse_issue_key("PLI42")


def test_parse_page_id_valid() -> None:
    assert parse_page_id("12345") == "12345"
    assert parse_page_id(12345) == "12345"


def test_parse_page_id_rejects_non_numeric() -> None:
    from atlassian_mcp.tools import ToolError

    with pytest.raises(ToolError):
        parse_page_id("abc")
    with pytest.raises(ToolError):
        parse_page_id("../etc")


# ---------------------------------------------------------------------------
# /healthz + /tools
# ---------------------------------------------------------------------------


async def test_healthz(client) -> None:
    resp = await client.get("/healthz")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ok"
    assert body["service"] == "atlassian-mcp"
    assert body["version"] == "1.5.0"


async def test_tools_listing_has_eight_tools(client) -> None:
    resp = await client.get("/tools")
    assert resp.status_code == 200
    body = resp.json()
    assert len(body["tools"]) == 8
    tool_ids = {t["tool_id"] for t in body["tools"]}
    assert tool_ids == {
        "atlassian.jira_search",
        "atlassian.jira_get_issue",
        "atlassian.jira_create_issue",
        "atlassian.jira_update_issue",
        "atlassian.jira_comment",
        "atlassian.confluence_search",
        "atlassian.confluence_get_page",
        "atlassian.confluence_create_page",
    }
    for t in body["tools"]:
        assert t["auth_method"] == "oauth2"
        assert t["auth_config"] == {"provider": "atlassian"}


# ---------------------------------------------------------------------------
# Auth — missing token
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "tool_id, payload",
    [
        ("atlassian.jira_search", {"jql": "project = PLI"}),
        ("atlassian.jira_get_issue", {"issue_key": ISSUE_KEY}),
        ("atlassian.jira_create_issue", {"project_key": "PLI", "summary": "x"}),
        ("atlassian.jira_update_issue", {"issue_key": ISSUE_KEY, "fields": {"summary": "y"}}),
        ("atlassian.jira_comment", {"issue_key": ISSUE_KEY, "body": "hi"}),
        ("atlassian.confluence_search", {"cql": "type=page"}),
        ("atlassian.confluence_get_page", {"page_id": "12345"}),
        (
            "atlassian.confluence_create_page",
            {"space_id": "1", "title": "X", "content": "<p/>"},
        ),
    ],
)
async def test_unauthorized_without_bearer(client, tool_id: str, payload: dict) -> None:
    resp = await client.post(
        f"/invoke/{tool_id}",
        json=payload,
        headers={"X-Plinth-OAuth-Cloudid": CLOUDID},
    )
    assert resp.status_code == 401
    body = resp.json()
    assert body["error"]["code"] == "UNAUTHORIZED"


async def test_unauthorized_with_non_bearer_scheme(client) -> None:
    resp = await client.post(
        "/invoke/atlassian.jira_search",
        json={"jql": "project = PLI"},
        headers={"Authorization": "Basic abc", "X-Plinth-OAuth-Cloudid": CLOUDID},
    )
    assert resp.status_code == 401


# ---------------------------------------------------------------------------
# Cloudid missing
# ---------------------------------------------------------------------------


async def test_jira_search_requires_cloudid(client) -> None:
    resp = await client.post(
        "/invoke/atlassian.jira_search",
        json={"jql": "project = PLI"},
        headers={"Authorization": "Bearer t"},
    )
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "ATLASSIAN_CLOUDID_MISSING"


async def test_confluence_search_requires_cloudid(client) -> None:
    resp = await client.post(
        "/invoke/atlassian.confluence_search",
        json={"cql": "type=page"},
        headers={"Authorization": "Bearer t"},
    )
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "ATLASSIAN_CLOUDID_MISSING"


# ---------------------------------------------------------------------------
# atlassian.jira_search
# ---------------------------------------------------------------------------


async def test_jira_search_returns_slim_issues(client) -> None:
    captured: dict = {}

    def _capture(request):
        captured["body"] = request.read()
        captured["auth"] = request.headers.get("Authorization")
        return Response(
            200,
            json={
                "issues": [
                    {
                        "id": "10001",
                        "key": "PLI-1",
                        "self": "https://api.atlassian.test/.../issue/PLI-1",
                        "fields": {
                            "summary": "First",
                            "status": {"name": "Open"},
                            "issuetype": {"name": "Bug"},
                            "assignee": {"displayName": "Alice"},
                        },
                    },
                ],
                "startAt": 0,
                "total": 1,
                "maxResults": 25,
            },
        )

    with respx.mock(assert_all_called=True) as mock:
        mock.post(
            f"https://api.atlassian.test/ex/jira/{CLOUDID}/rest/api/3/search"
        ).mock(side_effect=_capture)
        resp = await client.post(
            "/invoke/atlassian.jira_search",
            json={"jql": "project = PLI", "max_results": 5},
            headers=_auth_headers(),
        )
    assert resp.status_code == 200
    assert captured["auth"] == "Bearer t"
    body = resp.json()["result"]
    assert body["total"] == 1
    assert body["issues"][0]["key"] == "PLI-1"
    assert body["issues"][0]["summary"] == "First"
    assert body["issues"][0]["status"] == "Open"
    assert body["issues"][0]["assignee"] == "Alice"
    sent = _json.loads(captured["body"])
    assert sent["jql"] == "project = PLI"
    assert sent["maxResults"] == 5


async def test_jira_search_max_results_validation(client) -> None:
    resp = await client.post(
        "/invoke/atlassian.jira_search",
        json={"jql": "p=1", "max_results": 0},
        headers=_auth_headers(),
    )
    assert resp.status_code == 400
    resp = await client.post(
        "/invoke/atlassian.jira_search",
        json={"jql": "p=1", "max_results": 200},
        headers=_auth_headers(),
    )
    assert resp.status_code == 400


async def test_jira_search_propagates_atlassian_401(client) -> None:
    with respx.mock(assert_all_called=True) as mock:
        mock.post(
            f"https://api.atlassian.test/ex/jira/{CLOUDID}/rest/api/3/search"
        ).mock(return_value=Response(401, json={"error": "unauthorized"}))
        resp = await client.post(
            "/invoke/atlassian.jira_search",
            json={"jql": "x"},
            headers=_auth_headers(),
        )
    assert resp.status_code == 401
    assert resp.json()["error"]["code"] == "UNAUTHORIZED"


# ---------------------------------------------------------------------------
# atlassian.jira_get_issue
# ---------------------------------------------------------------------------


async def test_jira_get_issue_includes_comments(client) -> None:
    with respx.mock(assert_all_called=True) as mock:
        mock.get(
            f"https://api.atlassian.test/ex/jira/{CLOUDID}/rest/api/3/issue/{ISSUE_KEY}"
        ).mock(
            return_value=Response(
                200,
                json={
                    "id": "10042",
                    "key": ISSUE_KEY,
                    "self": "https://...",
                    "fields": {
                        "summary": "demo",
                        "status": {"name": "Open"},
                        "issuetype": {"name": "Task"},
                    },
                },
            )
        )
        mock.get(
            f"https://api.atlassian.test/ex/jira/{CLOUDID}/rest/api/3/issue/{ISSUE_KEY}/comment"
        ).mock(
            return_value=Response(
                200,
                json={
                    "comments": [
                        {
                            "id": "c1",
                            "author": {"displayName": "Alice"},
                            "body": {"text": "..."},
                            "created": "2026-05-01",
                            "updated": "2026-05-01",
                        }
                    ]
                },
            )
        )
        resp = await client.post(
            "/invoke/atlassian.jira_get_issue",
            json={"issue_key": ISSUE_KEY},
            headers=_auth_headers(),
        )
    assert resp.status_code == 200
    body = resp.json()["result"]
    assert body["key"] == ISSUE_KEY
    assert len(body["comments"]) == 1
    assert body["comments"][0]["author"] == "Alice"


async def test_jira_get_issue_404_propagates(client) -> None:
    with respx.mock(assert_all_called=True) as mock:
        mock.get(
            f"https://api.atlassian.test/ex/jira/{CLOUDID}/rest/api/3/issue/{ISSUE_KEY}"
        ).mock(return_value=Response(404, json={"error": "not found"}))
        resp = await client.post(
            "/invoke/atlassian.jira_get_issue",
            json={"issue_key": ISSUE_KEY},
            headers=_auth_headers(),
        )
    assert resp.status_code == 404


async def test_jira_get_issue_validates_key(client) -> None:
    resp = await client.post(
        "/invoke/atlassian.jira_get_issue",
        json={"issue_key": "not-a-key"},
        headers=_auth_headers(),
    )
    assert resp.status_code == 400


# ---------------------------------------------------------------------------
# atlassian.jira_create_issue
# ---------------------------------------------------------------------------


async def test_jira_create_issue_sends_adf_description(client) -> None:
    captured: dict = {}

    def _capture(request):
        captured["body"] = request.read()
        return Response(
            201,
            json={
                "id": "10100",
                "key": "PLI-100",
                "self": "https://...",
            },
        )

    with respx.mock(assert_all_called=True) as mock:
        mock.post(f"https://api.atlassian.test/ex/jira/{CLOUDID}/rest/api/3/issue").mock(
            side_effect=_capture
        )
        resp = await client.post(
            "/invoke/atlassian.jira_create_issue",
            json={
                "project_key": "PLI",
                "summary": "New issue",
                "description": "Hello world",
                "issue_type": "Task",
            },
            headers=_auth_headers(),
        )
    assert resp.status_code == 200
    body = resp.json()["result"]
    assert body["key"] == "PLI-100"
    sent = _json.loads(captured["body"])
    assert sent["fields"]["project"] == {"key": "PLI"}
    assert sent["fields"]["summary"] == "New issue"
    assert sent["fields"]["issuetype"] == {"name": "Task"}
    # ADF body shape.
    assert sent["fields"]["description"]["type"] == "doc"


async def test_jira_create_issue_requires_project_key(client) -> None:
    resp = await client.post(
        "/invoke/atlassian.jira_create_issue",
        json={"summary": "x"},
        headers=_auth_headers(),
    )
    assert resp.status_code == 400


async def test_jira_create_issue_requires_summary(client) -> None:
    resp = await client.post(
        "/invoke/atlassian.jira_create_issue",
        json={"project_key": "PLI"},
        headers=_auth_headers(),
    )
    assert resp.status_code == 400


# ---------------------------------------------------------------------------
# atlassian.jira_update_issue
# ---------------------------------------------------------------------------


async def test_jira_update_issue(client) -> None:
    with respx.mock(assert_all_called=True) as mock:
        mock.put(
            f"https://api.atlassian.test/ex/jira/{CLOUDID}/rest/api/3/issue/{ISSUE_KEY}"
        ).mock(return_value=Response(204))
        resp = await client.post(
            "/invoke/atlassian.jira_update_issue",
            json={"issue_key": ISSUE_KEY, "fields": {"summary": "Updated"}},
            headers=_auth_headers(),
        )
    assert resp.status_code == 200
    body = resp.json()["result"]
    assert body["key"] == ISSUE_KEY
    assert body["updated"] is True


async def test_jira_update_issue_requires_fields(client) -> None:
    resp = await client.post(
        "/invoke/atlassian.jira_update_issue",
        json={"issue_key": ISSUE_KEY, "fields": {}},
        headers=_auth_headers(),
    )
    assert resp.status_code == 400


# ---------------------------------------------------------------------------
# atlassian.jira_comment
# ---------------------------------------------------------------------------


async def test_jira_comment_returns_id(client) -> None:
    with respx.mock(assert_all_called=True) as mock:
        mock.post(
            f"https://api.atlassian.test/ex/jira/{CLOUDID}/rest/api/3/issue/{ISSUE_KEY}/comment"
        ).mock(
            return_value=Response(
                201,
                json={"id": "c100", "created": "2026-05-09T00:00:00Z"},
            )
        )
        resp = await client.post(
            "/invoke/atlassian.jira_comment",
            json={"issue_key": ISSUE_KEY, "body": "Looks good"},
            headers=_auth_headers(),
        )
    assert resp.status_code == 200
    body = resp.json()["result"]
    assert body["id"] == "c100"


async def test_jira_comment_requires_body(client) -> None:
    resp = await client.post(
        "/invoke/atlassian.jira_comment",
        json={"issue_key": ISSUE_KEY, "body": ""},
        headers=_auth_headers(),
    )
    assert resp.status_code == 400


# ---------------------------------------------------------------------------
# atlassian.confluence_search
# ---------------------------------------------------------------------------


async def test_confluence_search_with_cql(client) -> None:
    captured: dict = {}

    def _capture(request):
        captured["params"] = dict(request.url.params)
        return Response(
            200,
            json={
                "results": [
                    {
                        "content": {
                            "id": "12345",
                            "title": "Welcome",
                            "type": "page",
                        },
                        "excerpt": "Hello",
                        "_links": {"webui": "/wiki/x"},
                    }
                ]
            },
        )

    with respx.mock(assert_all_called=True) as mock:
        mock.get(
            f"https://api.atlassian.test/ex/confluence/{CLOUDID}/wiki/rest/api/search"
        ).mock(side_effect=_capture)
        resp = await client.post(
            "/invoke/atlassian.confluence_search",
            json={"cql": "type=page", "limit": 10},
            headers=_auth_headers(),
        )
    assert resp.status_code == 200
    body = resp.json()["result"]
    assert body["count"] == 1
    assert body["results"][0]["title"] == "Welcome"
    assert captured["params"]["cql"] == "type=page"
    assert captured["params"]["limit"] == "10"


async def test_confluence_search_requires_cql(client) -> None:
    resp = await client.post(
        "/invoke/atlassian.confluence_search",
        json={},
        headers=_auth_headers(),
    )
    assert resp.status_code == 400


# ---------------------------------------------------------------------------
# atlassian.confluence_get_page
# ---------------------------------------------------------------------------


async def test_confluence_get_page_returns_storage_body(client) -> None:
    with respx.mock(assert_all_called=True) as mock:
        mock.get(
            f"https://api.atlassian.test/ex/confluence/{CLOUDID}/wiki/api/v2/pages/12345"
        ).mock(
            return_value=Response(
                200,
                json={
                    "id": 12345,
                    "title": "Welcome",
                    "spaceId": 99,
                    "version": {"number": 3},
                    "body": {"storage": {"value": "<p>hi</p>"}},
                    "_links": {"webui": "/x"},
                },
            )
        )
        resp = await client.post(
            "/invoke/atlassian.confluence_get_page",
            json={"page_id": "12345"},
            headers=_auth_headers(),
        )
    assert resp.status_code == 200
    body = resp.json()["result"]
    assert body["id"] == "12345"
    assert body["title"] == "Welcome"
    assert body["body"] == "<p>hi</p>"
    assert body["space_id"] == "99"
    assert body["version"] == 3


async def test_confluence_get_page_validates_id(client) -> None:
    resp = await client.post(
        "/invoke/atlassian.confluence_get_page",
        json={"page_id": "abc"},
        headers=_auth_headers(),
    )
    assert resp.status_code == 400


# ---------------------------------------------------------------------------
# atlassian.confluence_create_page
# ---------------------------------------------------------------------------


async def test_confluence_create_page(client) -> None:
    captured: dict = {}

    def _capture(request):
        captured["body"] = request.read()
        return Response(
            200,
            json={
                "id": 7777,
                "title": "Greetings",
                "spaceId": 99,
                "version": {"number": 1},
                "body": {"storage": {"value": "<p>Hello</p>"}},
                "_links": {"webui": "/x"},
            },
        )

    with respx.mock(assert_all_called=True) as mock:
        mock.post(
            f"https://api.atlassian.test/ex/confluence/{CLOUDID}/wiki/api/v2/pages"
        ).mock(side_effect=_capture)
        resp = await client.post(
            "/invoke/atlassian.confluence_create_page",
            json={
                "space_id": "99",
                "title": "Greetings",
                "content": "<p>Hello</p>",
            },
            headers=_auth_headers(),
        )
    assert resp.status_code == 200
    body = resp.json()["result"]
    assert body["id"] == "7777"
    sent = _json.loads(captured["body"])
    assert sent["spaceId"] == "99"
    assert sent["title"] == "Greetings"
    assert sent["body"]["value"] == "<p>Hello</p>"


async def test_confluence_create_page_requires_title(client) -> None:
    resp = await client.post(
        "/invoke/atlassian.confluence_create_page",
        json={"space_id": "99", "content": "<p/>"},
        headers=_auth_headers(),
    )
    assert resp.status_code == 400


async def test_confluence_create_page_requires_content(client) -> None:
    resp = await client.post(
        "/invoke/atlassian.confluence_create_page",
        json={"space_id": "99", "title": "x"},
        headers=_auth_headers(),
    )
    assert resp.status_code == 400


# ---------------------------------------------------------------------------
# Unknown tool / malformed body
# ---------------------------------------------------------------------------


async def test_unknown_tool_returns_404(client) -> None:
    resp = await client.post(
        "/invoke/atlassian.does_not_exist",
        json={},
        headers=_auth_headers(),
    )
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "TOOL_NOT_FOUND"


async def test_invalid_json_body_returns_400(client) -> None:
    resp = await client.post(
        "/invoke/atlassian.jira_search",
        content=b"not json",
        headers={
            **_auth_headers(),
            "Content-Type": "application/json",
        },
    )
    assert resp.status_code == 400


async def test_array_body_rejected(client) -> None:
    resp = await client.post(
        "/invoke/atlassian.jira_search",
        json=["not an object"],
        headers=_auth_headers(),
    )
    assert resp.status_code == 400


# ---------------------------------------------------------------------------
# Auth + cloudid plumbing — exact URL shape ends up with cloudid in path.
# ---------------------------------------------------------------------------


async def test_jira_request_uses_cloudid_in_path(client) -> None:
    """Confirm the URL contains the cloudid we passed via header."""
    captured: dict = {}

    def _capture(request):
        captured["path"] = request.url.path
        return Response(200, json={"issues": [], "total": 0, "startAt": 0, "maxResults": 25})

    other_cloud = "different-cloudid"
    with respx.mock(assert_all_called=True) as mock:
        mock.post(
            f"https://api.atlassian.test/ex/jira/{other_cloud}/rest/api/3/search"
        ).mock(side_effect=_capture)
        resp = await client.post(
            "/invoke/atlassian.jira_search",
            json={"jql": "x"},
            headers=_auth_headers(cloudid=other_cloud),
        )
    assert resp.status_code == 200
    assert other_cloud in captured["path"]
    assert "/ex/jira/" in captured["path"]
