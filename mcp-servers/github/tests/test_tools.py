# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 The Plinth Authors
"""Tests for the github-mcp server endpoints + tools."""

from __future__ import annotations

import pytest
import respx
from httpx import Response

from github_mcp.tools import parse_repo


# ---------------------------------------------------------------------------
# parse_repo — input validation
# ---------------------------------------------------------------------------


def test_parse_repo_basic() -> None:
    assert parse_repo("octocat/hello-world") == ("octocat", "hello-world")


def test_parse_repo_with_dots_and_hyphens() -> None:
    assert parse_repo("OWNER-1/repo.name") == ("OWNER-1", "repo.name")


def test_parse_repo_rejects_absolute_path() -> None:
    from github_mcp.tools import ToolError

    with pytest.raises(ToolError):
        parse_repo("/etc/passwd")


def test_parse_repo_rejects_traversal() -> None:
    from github_mcp.tools import ToolError

    with pytest.raises(ToolError):
        parse_repo("owner/../etc")
    with pytest.raises(ToolError):
        parse_repo("../somewhere/else")


def test_parse_repo_rejects_extra_slash() -> None:
    from github_mcp.tools import ToolError

    with pytest.raises(ToolError):
        parse_repo("owner/name/extra")


def test_parse_repo_rejects_empty() -> None:
    from github_mcp.tools import ToolError

    with pytest.raises(ToolError):
        parse_repo("")
    with pytest.raises(ToolError):
        parse_repo(None)


def test_parse_repo_rejects_bad_chars() -> None:
    from github_mcp.tools import ToolError

    with pytest.raises(ToolError):
        parse_repo("owner!/repo")
    with pytest.raises(ToolError):
        parse_repo("owner/repo with space")


# ---------------------------------------------------------------------------
# /healthz + /tools
# ---------------------------------------------------------------------------


async def test_healthz(client) -> None:
    resp = await client.get("/healthz")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ok"
    assert body["service"] == "github-mcp"
    assert body["version"] == "0.3.0"


async def test_tools_listing_has_seven_tools(client) -> None:
    resp = await client.get("/tools")
    assert resp.status_code == 200
    body = resp.json()
    assert len(body["tools"]) == 7
    tool_ids = {t["tool_id"] for t in body["tools"]}
    assert tool_ids == {
        "github.list_issues",
        "github.get_issue",
        "github.create_issue",
        "github.update_issue",
        "github.comment_on_issue",
        "github.get_repo",
        "github.search_code",
    }
    # Every tool should advertise oauth2 + provider=github so the gateway can
    # attach a token.
    for t in body["tools"]:
        assert t["auth_method"] == "oauth2"
        assert t["auth_config"] == {"provider": "github"}


# ---------------------------------------------------------------------------
# Auth — missing token
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "tool_id, payload",
    [
        ("github.list_issues", {"repo": "o/r"}),
        ("github.get_issue", {"repo": "o/r", "number": 1}),
        ("github.create_issue", {"repo": "o/r", "title": "x"}),
        ("github.update_issue", {"repo": "o/r", "number": 1, "state": "closed"}),
        ("github.comment_on_issue", {"repo": "o/r", "number": 1, "body": "x"}),
        ("github.get_repo", {"repo": "o/r"}),
        ("github.search_code", {"query": "foo"}),
    ],
)
async def test_unauthorized_without_bearer(client, tool_id: str, payload: dict) -> None:
    resp = await client.post(f"/invoke/{tool_id}", json=payload)
    assert resp.status_code == 401
    body = resp.json()
    assert body["error"]["code"] == "UNAUTHORIZED"


async def test_unauthorized_with_non_bearer_scheme(client) -> None:
    resp = await client.post(
        "/invoke/github.list_issues",
        json={"repo": "o/r"},
        headers={"Authorization": "Basic abc"},
    )
    assert resp.status_code == 401


# ---------------------------------------------------------------------------
# github.list_issues
# ---------------------------------------------------------------------------


async def test_list_issues_filters_pull_requests(client) -> None:
    with respx.mock(assert_all_called=True) as mock:
        mock.get("https://api.github.test/repos/octocat/hello/issues").mock(
            return_value=Response(
                200,
                json=[
                    {
                        "number": 1,
                        "title": "real issue",
                        "body": "...",
                        "state": "open",
                        "html_url": "https://gh/1",
                        "user": {"login": "alice", "id": 11},
                        "labels": [{"name": "bug"}],
                        "comments": 0,
                        "created_at": "2026-01-01T00:00:00Z",
                        "updated_at": "2026-01-01T00:00:00Z",
                    },
                    {
                        "number": 2,
                        "title": "PR not issue",
                        "state": "open",
                        "pull_request": {"url": "..."},
                        "user": {"login": "bob"},
                        "labels": [],
                        "html_url": "https://gh/2",
                    },
                ],
            )
        )
        resp = await client.post(
            "/invoke/github.list_issues",
            json={"repo": "octocat/hello"},
            headers={"Authorization": "Bearer token123"},
        )
    assert resp.status_code == 200
    result = resp.json()["result"]
    assert result["count"] == 1
    assert result["issues"][0]["number"] == 1
    assert result["issues"][0]["labels"] == ["bug"]


async def test_list_issues_state_validation(client) -> None:
    resp = await client.post(
        "/invoke/github.list_issues",
        json={"repo": "o/r", "state": "weird"},
        headers={"Authorization": "Bearer t"},
    )
    assert resp.status_code == 400


async def test_list_issues_propagates_github_401(client) -> None:
    with respx.mock(assert_all_called=True) as mock:
        mock.get("https://api.github.test/repos/o/r/issues").mock(
            return_value=Response(401, json={"message": "Bad creds"})
        )
        resp = await client.post(
            "/invoke/github.list_issues",
            json={"repo": "o/r"},
            headers={"Authorization": "Bearer bad"},
        )
    assert resp.status_code == 401
    assert resp.json()["error"]["code"] == "UNAUTHORIZED"


async def test_list_issues_propagates_github_404(client) -> None:
    with respx.mock(assert_all_called=True) as mock:
        mock.get("https://api.github.test/repos/o/missing/issues").mock(
            return_value=Response(404, json={"message": "Not Found"})
        )
        resp = await client.post(
            "/invoke/github.list_issues",
            json={"repo": "o/missing"},
            headers={"Authorization": "Bearer t"},
        )
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# github.get_issue
# ---------------------------------------------------------------------------


async def test_get_issue_returns_issue_and_comments(client) -> None:
    with respx.mock(assert_all_called=True) as mock:
        mock.get("https://api.github.test/repos/o/r/issues/5").mock(
            return_value=Response(
                200,
                json={
                    "number": 5,
                    "title": "Crash on startup",
                    "body": "...",
                    "state": "open",
                    "user": {"login": "alice"},
                    "labels": [],
                    "html_url": "https://gh/5",
                },
            )
        )
        mock.get("https://api.github.test/repos/o/r/issues/5/comments").mock(
            return_value=Response(
                200,
                json=[
                    {"id": 1, "body": "+1", "user": {"login": "bob"}, "created_at": "2026"},
                    {"id": 2, "body": "ack", "user": {"login": "carol"}, "created_at": "2026"},
                ],
            )
        )
        resp = await client.post(
            "/invoke/github.get_issue",
            json={"repo": "o/r", "number": 5},
            headers={"Authorization": "Bearer t"},
        )
    assert resp.status_code == 200
    body = resp.json()["result"]
    assert body["issue"]["number"] == 5
    assert len(body["comments"]) == 2


# ---------------------------------------------------------------------------
# github.create_issue
# ---------------------------------------------------------------------------


async def test_create_issue_posts_payload(client) -> None:
    captured: dict = {}

    def _capture(request):
        captured["body"] = request.read()
        captured["auth"] = request.headers.get("Authorization")
        return Response(
            201,
            json={
                "number": 99,
                "title": "Hello",
                "body": "world",
                "state": "open",
                "user": {"login": "me"},
                "labels": [{"name": "triage"}],
                "html_url": "https://gh/99",
            },
        )

    with respx.mock(assert_all_called=True) as mock:
        mock.post("https://api.github.test/repos/o/r/issues").mock(side_effect=_capture)
        resp = await client.post(
            "/invoke/github.create_issue",
            json={"repo": "o/r", "title": "Hello", "body": "world", "labels": ["triage"]},
            headers={"Authorization": "Bearer secret"},
        )
    assert resp.status_code == 200
    assert captured["auth"] == "Bearer secret"
    import json as _json

    sent = _json.loads(captured["body"])
    assert sent == {"title": "Hello", "body": "world", "labels": ["triage"]}
    issue = resp.json()["result"]["issue"]
    assert issue["number"] == 99


async def test_create_issue_requires_title(client) -> None:
    resp = await client.post(
        "/invoke/github.create_issue",
        json={"repo": "o/r", "title": ""},
        headers={"Authorization": "Bearer t"},
    )
    assert resp.status_code == 400


# ---------------------------------------------------------------------------
# github.update_issue
# ---------------------------------------------------------------------------


async def test_update_issue_patches(client) -> None:
    with respx.mock(assert_all_called=True) as mock:
        mock.patch("https://api.github.test/repos/o/r/issues/3").mock(
            return_value=Response(
                200,
                json={
                    "number": 3,
                    "title": "new",
                    "state": "closed",
                    "user": {"login": "u"},
                    "labels": [],
                    "html_url": "https://gh/3",
                },
            )
        )
        resp = await client.post(
            "/invoke/github.update_issue",
            json={"repo": "o/r", "number": 3, "state": "closed"},
            headers={"Authorization": "Bearer t"},
        )
    assert resp.status_code == 200
    assert resp.json()["result"]["issue"]["state"] == "closed"


async def test_update_issue_requires_a_field(client) -> None:
    resp = await client.post(
        "/invoke/github.update_issue",
        json={"repo": "o/r", "number": 3},
        headers={"Authorization": "Bearer t"},
    )
    assert resp.status_code == 400


async def test_update_issue_state_must_be_valid(client) -> None:
    resp = await client.post(
        "/invoke/github.update_issue",
        json={"repo": "o/r", "number": 3, "state": "in-progress"},
        headers={"Authorization": "Bearer t"},
    )
    assert resp.status_code == 400


# ---------------------------------------------------------------------------
# github.comment_on_issue
# ---------------------------------------------------------------------------


async def test_comment_on_issue_posts(client) -> None:
    with respx.mock(assert_all_called=True) as mock:
        mock.post("https://api.github.test/repos/o/r/issues/7/comments").mock(
            return_value=Response(
                201,
                json={
                    "id": 555,
                    "body": "thanks",
                    "user": {"login": "me"},
                    "created_at": "2026",
                    "html_url": "https://gh/7#c555",
                },
            )
        )
        resp = await client.post(
            "/invoke/github.comment_on_issue",
            json={"repo": "o/r", "number": 7, "body": "thanks"},
            headers={"Authorization": "Bearer t"},
        )
    assert resp.status_code == 200
    body = resp.json()["result"]["comment"]
    assert body["id"] == 555
    assert body["body"] == "thanks"


async def test_comment_requires_body(client) -> None:
    resp = await client.post(
        "/invoke/github.comment_on_issue",
        json={"repo": "o/r", "number": 1, "body": ""},
        headers={"Authorization": "Bearer t"},
    )
    assert resp.status_code == 400


# ---------------------------------------------------------------------------
# github.get_repo
# ---------------------------------------------------------------------------


async def test_get_repo(client) -> None:
    with respx.mock(assert_all_called=True) as mock:
        mock.get("https://api.github.test/repos/octocat/hello").mock(
            return_value=Response(
                200,
                json={
                    "id": 1,
                    "full_name": "octocat/hello",
                    "private": False,
                    "description": "hi",
                    "html_url": "https://gh/octocat/hello",
                    "default_branch": "main",
                    "open_issues_count": 4,
                    "stargazers_count": 100,
                    "language": "Python",
                    "owner": {"login": "octocat"},
                },
            )
        )
        resp = await client.post(
            "/invoke/github.get_repo",
            json={"repo": "octocat/hello"},
            headers={"Authorization": "Bearer t"},
        )
    assert resp.status_code == 200
    body = resp.json()["result"]["repo"]
    assert body["full_name"] == "octocat/hello"
    assert body["open_issues_count"] == 4


# ---------------------------------------------------------------------------
# github.search_code
# ---------------------------------------------------------------------------


async def test_search_code_with_repo_scope(client) -> None:
    captured: dict = {}

    def _capture(request):
        captured["url"] = str(request.url)
        return Response(
            200,
            json={
                "total_count": 1,
                "items": [
                    {
                        "name": "foo.py",
                        "path": "src/foo.py",
                        "html_url": "https://gh/o/r/blob/main/src/foo.py",
                        "repository": {"full_name": "o/r"},
                    }
                ],
            },
        )

    with respx.mock(assert_all_called=True) as mock:
        mock.get("https://api.github.test/search/code").mock(side_effect=_capture)
        resp = await client.post(
            "/invoke/github.search_code",
            json={"query": "TODO", "repo": "o/r"},
            headers={"Authorization": "Bearer t"},
        )
    assert resp.status_code == 200
    body = resp.json()["result"]
    assert body["total_count"] == 1
    # The query is URL-encoded over the wire; check both forms.
    assert "repo:o/r" in captured["url"] or "repo%3Ao%2Fr" in captured["url"]


async def test_search_code_requires_query(client) -> None:
    resp = await client.post(
        "/invoke/github.search_code",
        json={"query": ""},
        headers={"Authorization": "Bearer t"},
    )
    assert resp.status_code == 400


# ---------------------------------------------------------------------------
# Unknown tool
# ---------------------------------------------------------------------------


async def test_unknown_tool_returns_404(client) -> None:
    resp = await client.post(
        "/invoke/github.does_not_exist",
        json={},
        headers={"Authorization": "Bearer t"},
    )
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "TOOL_NOT_FOUND"


async def test_invalid_json_body_returns_400(client) -> None:
    resp = await client.post(
        "/invoke/github.list_issues",
        content=b"not json",
        headers={
            "Authorization": "Bearer t",
            "Content-Type": "application/json",
        },
    )
    assert resp.status_code == 400


async def test_array_body_rejected(client) -> None:
    resp = await client.post(
        "/invoke/github.list_issues",
        json=["not an object"],
        headers={"Authorization": "Bearer t"},
    )
    assert resp.status_code == 400


# ---------------------------------------------------------------------------
# Path traversal / repo validation in tools
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "tool_id, payload",
    [
        ("github.list_issues", {"repo": "/etc/passwd"}),
        ("github.get_issue", {"repo": "owner/../etc", "number": 1}),
        ("github.create_issue", {"repo": "owner/name/extra", "title": "x"}),
        ("github.get_repo", {"repo": ""}),
        ("github.update_issue", {"repo": "../absolute", "number": 1, "state": "open"}),
        ("github.comment_on_issue", {"repo": "owner/!bad", "number": 1, "body": "x"}),
        ("github.search_code", {"query": "x", "repo": "/abs/path"}),
    ],
)
async def test_repo_validation_per_tool(client, tool_id: str, payload: dict) -> None:
    resp = await client.post(
        f"/invoke/{tool_id}",
        json=payload,
        headers={"Authorization": "Bearer t"},
    )
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "INVALID_ARGUMENTS"
