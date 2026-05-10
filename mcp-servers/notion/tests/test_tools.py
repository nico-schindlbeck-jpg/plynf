# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 The Plinth Authors
"""Tests for the notion-mcp server endpoints + tools."""

from __future__ import annotations

import pytest
import respx
from httpx import Response

from notion_mcp.tools import parse_notion_id


# A canonical Notion UUID we'll reuse in fixtures.
PAGE_ID = "00000000-0000-0000-0000-000000000001"
DB_ID = "11111111-1111-1111-1111-111111111111"


# ---------------------------------------------------------------------------
# parse_notion_id — input validation
# ---------------------------------------------------------------------------


def test_parse_notion_id_with_hyphens() -> None:
    assert parse_notion_id(PAGE_ID) == PAGE_ID


def test_parse_notion_id_without_hyphens() -> None:
    raw = "abcdef0123456789abcdef0123456789"
    assert parse_notion_id(raw) == raw


def test_parse_notion_id_rejects_absolute_path() -> None:
    from notion_mcp.tools import ToolError

    with pytest.raises(ToolError):
        parse_notion_id("/etc/passwd")


def test_parse_notion_id_rejects_traversal() -> None:
    from notion_mcp.tools import ToolError

    with pytest.raises(ToolError):
        parse_notion_id("../something")


def test_parse_notion_id_rejects_empty() -> None:
    from notion_mcp.tools import ToolError

    with pytest.raises(ToolError):
        parse_notion_id("")
    with pytest.raises(ToolError):
        parse_notion_id(None)


def test_parse_notion_id_rejects_bad_format() -> None:
    from notion_mcp.tools import ToolError

    with pytest.raises(ToolError):
        parse_notion_id("not-a-uuid")
    with pytest.raises(ToolError):
        parse_notion_id("ZZZ00000-0000-0000-0000-000000000001")


# ---------------------------------------------------------------------------
# /healthz + /tools
# ---------------------------------------------------------------------------


async def test_healthz(client) -> None:
    resp = await client.get("/healthz")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ok"
    assert body["service"] == "notion-mcp"
    assert body["version"] == "1.1.0"


async def test_tools_listing_has_seven_tools(client) -> None:
    resp = await client.get("/tools")
    assert resp.status_code == 200
    body = resp.json()
    assert len(body["tools"]) == 7
    tool_ids = {t["tool_id"] for t in body["tools"]}
    assert tool_ids == {
        "notion.search",
        "notion.get_page",
        "notion.create_page",
        "notion.update_page",
        "notion.append_block",
        "notion.list_databases",
        "notion.query_database",
    }
    for t in body["tools"]:
        assert t["auth_method"] == "oauth2"
        assert t["auth_config"] == {"provider": "notion"}


# ---------------------------------------------------------------------------
# Auth — missing token
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "tool_id, payload",
    [
        ("notion.search", {"query": "hello"}),
        ("notion.get_page", {"page_id": PAGE_ID}),
        ("notion.create_page", {"parent_page_id": PAGE_ID, "title": "x"}),
        ("notion.update_page", {"page_id": PAGE_ID, "archived": True}),
        ("notion.append_block", {"page_id": PAGE_ID, "blocks": [{}]}),
        ("notion.list_databases", {}),
        ("notion.query_database", {"database_id": DB_ID}),
    ],
)
async def test_unauthorized_without_bearer(client, tool_id: str, payload: dict) -> None:
    resp = await client.post(f"/invoke/{tool_id}", json=payload)
    assert resp.status_code == 401
    body = resp.json()
    assert body["error"]["code"] == "UNAUTHORIZED"


async def test_unauthorized_with_non_bearer_scheme(client) -> None:
    resp = await client.post(
        "/invoke/notion.search",
        json={"query": "x"},
        headers={"Authorization": "Basic abc"},
    )
    assert resp.status_code == 401


# ---------------------------------------------------------------------------
# notion.search
# ---------------------------------------------------------------------------


async def test_search_returns_expected_structure(client) -> None:
    captured: dict = {}

    def _capture(request):
        captured["body"] = request.read()
        captured["auth"] = request.headers.get("Authorization")
        captured["version"] = request.headers.get("Notion-Version")
        return Response(
            200,
            json={
                "object": "list",
                "results": [
                    {
                        "object": "page",
                        "id": PAGE_ID,
                        "url": "https://www.notion.so/Foo-1234",
                        "last_edited_time": "2026-05-01T00:00:00Z",
                        "properties": {
                            "title": {
                                "type": "title",
                                "title": [{"plain_text": "Foo"}],
                            }
                        },
                    },
                    {
                        "object": "database",
                        "id": DB_ID,
                        "url": "https://www.notion.so/db",
                        "last_edited_time": "2026-05-02T00:00:00Z",
                        "title": [{"plain_text": "Tasks DB"}],
                    },
                ],
                "has_more": False,
            },
        )

    with respx.mock(assert_all_called=True) as mock:
        mock.post("https://api.notion.test/v1/search").mock(side_effect=_capture)
        resp = await client.post(
            "/invoke/notion.search",
            json={"query": "foo"},
            headers={"Authorization": "Bearer secret"},
        )
    assert resp.status_code == 200
    assert captured["auth"] == "Bearer secret"
    assert captured["version"] == "2022-06-28"
    body = resp.json()["result"]
    assert body["count"] == 2
    assert body["results"][0]["title"] == "Foo"
    assert body["results"][0]["type"] == "page"
    assert body["results"][1]["title"] == "Tasks DB"
    assert body["results"][1]["type"] == "database"


async def test_search_page_size_validation(client) -> None:
    resp = await client.post(
        "/invoke/notion.search",
        json={"query": "x", "page_size": 0},
        headers={"Authorization": "Bearer t"},
    )
    assert resp.status_code == 400
    resp = await client.post(
        "/invoke/notion.search",
        json={"query": "x", "page_size": 200},
        headers={"Authorization": "Bearer t"},
    )
    assert resp.status_code == 400


async def test_search_propagates_notion_401(client) -> None:
    with respx.mock(assert_all_called=True) as mock:
        mock.post("https://api.notion.test/v1/search").mock(
            return_value=Response(401, json={"object": "error", "code": "unauthorized"})
        )
        resp = await client.post(
            "/invoke/notion.search",
            json={"query": "x"},
            headers={"Authorization": "Bearer bad"},
        )
    assert resp.status_code == 401
    assert resp.json()["error"]["code"] == "UNAUTHORIZED"


# ---------------------------------------------------------------------------
# notion.get_page
# ---------------------------------------------------------------------------


async def test_get_page_returns_page_and_content(client) -> None:
    with respx.mock(assert_all_called=True) as mock:
        mock.get(f"https://api.notion.test/v1/pages/{PAGE_ID}").mock(
            return_value=Response(
                200,
                json={
                    "object": "page",
                    "id": PAGE_ID,
                    "url": "https://www.notion.so/Foo",
                    "archived": False,
                    "created_time": "2026-05-01T00:00:00Z",
                    "last_edited_time": "2026-05-02T00:00:00Z",
                    "properties": {
                        "Name": {
                            "type": "title",
                            "title": [{"plain_text": "Hello world"}],
                        }
                    },
                    "parent": {"type": "page_id", "page_id": PAGE_ID},
                },
            )
        )
        mock.get(f"https://api.notion.test/v1/blocks/{PAGE_ID}/children").mock(
            return_value=Response(
                200,
                json={
                    "object": "list",
                    "results": [
                        {"object": "block", "id": "b1", "type": "paragraph", "has_children": False},
                        {"object": "block", "id": "b2", "type": "heading_1", "has_children": False},
                    ],
                },
            )
        )
        resp = await client.post(
            "/invoke/notion.get_page",
            json={"page_id": PAGE_ID},
            headers={"Authorization": "Bearer t"},
        )
    assert resp.status_code == 200
    body = resp.json()["result"]
    assert body["id"] == PAGE_ID
    assert body["title"] == "Hello world"
    assert len(body["content"]) == 2
    assert body["content"][0]["type"] == "paragraph"
    assert body["archived"] is False


async def test_get_page_handles_archived_page(client) -> None:
    with respx.mock(assert_all_called=True) as mock:
        mock.get(f"https://api.notion.test/v1/pages/{PAGE_ID}").mock(
            return_value=Response(
                200,
                json={
                    "object": "page",
                    "id": PAGE_ID,
                    "url": "https://www.notion.so/X",
                    "archived": True,
                    "properties": {
                        "title": {"type": "title", "title": [{"plain_text": "Old"}]}
                    },
                },
            )
        )
        mock.get(f"https://api.notion.test/v1/blocks/{PAGE_ID}/children").mock(
            return_value=Response(200, json={"results": []})
        )
        resp = await client.post(
            "/invoke/notion.get_page",
            json={"page_id": PAGE_ID},
            headers={"Authorization": "Bearer t"},
        )
    assert resp.status_code == 200
    body = resp.json()["result"]
    assert body["archived"] is True
    assert body["content"] == []


async def test_get_page_404_propagates(client) -> None:
    with respx.mock(assert_all_called=True) as mock:
        mock.get(f"https://api.notion.test/v1/pages/{PAGE_ID}").mock(
            return_value=Response(404, json={"object": "error", "code": "object_not_found"})
        )
        resp = await client.post(
            "/invoke/notion.get_page",
            json={"page_id": PAGE_ID},
            headers={"Authorization": "Bearer t"},
        )
    assert resp.status_code == 404


async def test_get_page_requires_page_id(client) -> None:
    resp = await client.post(
        "/invoke/notion.get_page",
        json={},
        headers={"Authorization": "Bearer t"},
    )
    assert resp.status_code == 400


# ---------------------------------------------------------------------------
# notion.create_page
# ---------------------------------------------------------------------------


async def test_create_page_with_database_parent(client) -> None:
    captured: dict = {}

    def _capture(request):
        captured["body"] = request.read()
        return Response(
            200,
            json={
                "object": "page",
                "id": "newpage-0000-0000-0000-000000000001",
                "url": "https://www.notion.so/new",
            },
        )

    with respx.mock(assert_all_called=True) as mock:
        mock.post("https://api.notion.test/v1/pages").mock(side_effect=_capture)
        resp = await client.post(
            "/invoke/notion.create_page",
            json={"parent_database_id": DB_ID, "title": "New row"},
            headers={"Authorization": "Bearer t"},
        )
    assert resp.status_code == 200
    body = resp.json()["result"]
    assert body["id"].startswith("newpage")
    import json as _json

    sent = _json.loads(captured["body"])
    assert sent["parent"] == {"database_id": DB_ID}
    # Default title property has the user-supplied title.
    assert "title" in sent["properties"]


async def test_create_page_with_page_parent(client) -> None:
    captured: dict = {}

    def _capture(request):
        captured["body"] = request.read()
        return Response(
            200,
            json={"object": "page", "id": PAGE_ID, "url": "https://www.notion.so/x"},
        )

    with respx.mock(assert_all_called=True) as mock:
        mock.post("https://api.notion.test/v1/pages").mock(side_effect=_capture)
        resp = await client.post(
            "/invoke/notion.create_page",
            json={
                "parent_page_id": PAGE_ID,
                "title": "Sub-page",
                "content": [
                    {
                        "object": "block",
                        "type": "paragraph",
                        "paragraph": {
                            "rich_text": [
                                {"type": "text", "text": {"content": "hello"}}
                            ]
                        },
                    }
                ],
            },
            headers={"Authorization": "Bearer t"},
        )
    assert resp.status_code == 200
    import json as _json

    sent = _json.loads(captured["body"])
    assert sent["parent"] == {"page_id": PAGE_ID}
    assert "children" in sent
    assert len(sent["children"]) == 1


async def test_create_page_requires_title(client) -> None:
    resp = await client.post(
        "/invoke/notion.create_page",
        json={"parent_page_id": PAGE_ID},
        headers={"Authorization": "Bearer t"},
    )
    assert resp.status_code == 400


async def test_create_page_requires_a_parent(client) -> None:
    resp = await client.post(
        "/invoke/notion.create_page",
        json={"title": "x"},
        headers={"Authorization": "Bearer t"},
    )
    assert resp.status_code == 400


async def test_create_page_rejects_two_parents(client) -> None:
    resp = await client.post(
        "/invoke/notion.create_page",
        json={
            "parent_database_id": DB_ID,
            "parent_page_id": PAGE_ID,
            "title": "x",
        },
        headers={"Authorization": "Bearer t"},
    )
    assert resp.status_code == 400


# ---------------------------------------------------------------------------
# notion.update_page
# ---------------------------------------------------------------------------


async def test_update_page_archives(client) -> None:
    with respx.mock(assert_all_called=True) as mock:
        mock.patch(f"https://api.notion.test/v1/pages/{PAGE_ID}").mock(
            return_value=Response(
                200,
                json={
                    "object": "page",
                    "id": PAGE_ID,
                    "archived": True,
                    "last_edited_time": "2026-05-09T00:00:00Z",
                },
            )
        )
        resp = await client.post(
            "/invoke/notion.update_page",
            json={"page_id": PAGE_ID, "archived": True},
            headers={"Authorization": "Bearer t"},
        )
    assert resp.status_code == 200
    body = resp.json()["result"]
    assert body["id"] == PAGE_ID
    assert body["archived"] is True


async def test_update_page_requires_a_field(client) -> None:
    resp = await client.post(
        "/invoke/notion.update_page",
        json={"page_id": PAGE_ID},
        headers={"Authorization": "Bearer t"},
    )
    assert resp.status_code == 400


async def test_update_page_archived_must_be_bool(client) -> None:
    resp = await client.post(
        "/invoke/notion.update_page",
        json={"page_id": PAGE_ID, "archived": "yes"},
        headers={"Authorization": "Bearer t"},
    )
    assert resp.status_code == 400


# ---------------------------------------------------------------------------
# notion.append_block
# ---------------------------------------------------------------------------


async def test_append_block_returns_count(client) -> None:
    with respx.mock(assert_all_called=True) as mock:
        mock.patch(f"https://api.notion.test/v1/blocks/{PAGE_ID}/children").mock(
            return_value=Response(
                200,
                json={
                    "object": "list",
                    "results": [
                        {"id": "b1", "object": "block"},
                        {"id": "b2", "object": "block"},
                    ],
                },
            )
        )
        resp = await client.post(
            "/invoke/notion.append_block",
            json={
                "page_id": PAGE_ID,
                "blocks": [
                    {
                        "object": "block",
                        "type": "paragraph",
                        "paragraph": {
                            "rich_text": [{"type": "text", "text": {"content": "hi"}}]
                        },
                    },
                    {
                        "object": "block",
                        "type": "paragraph",
                        "paragraph": {
                            "rich_text": [{"type": "text", "text": {"content": "hi2"}}]
                        },
                    },
                ],
            },
            headers={"Authorization": "Bearer t"},
        )
    assert resp.status_code == 200
    body = resp.json()["result"]
    assert body["appended"] == 2


async def test_append_block_requires_blocks(client) -> None:
    resp = await client.post(
        "/invoke/notion.append_block",
        json={"page_id": PAGE_ID, "blocks": []},
        headers={"Authorization": "Bearer t"},
    )
    assert resp.status_code == 400


# ---------------------------------------------------------------------------
# notion.list_databases
# ---------------------------------------------------------------------------


async def test_list_databases(client) -> None:
    captured: dict = {}

    def _capture(request):
        captured["body"] = request.read()
        return Response(
            200,
            json={
                "object": "list",
                "results": [
                    {
                        "object": "database",
                        "id": DB_ID,
                        "url": "https://www.notion.so/db1",
                        "last_edited_time": "2026-05-01T00:00:00Z",
                        "title": [{"plain_text": "Tasks"}],
                    },
                    {
                        # Pages should be filtered out by the slim helper even if
                        # the API returns them.
                        "object": "page",
                        "id": PAGE_ID,
                        "url": "https://www.notion.so/p",
                    },
                ],
            },
        )

    with respx.mock(assert_all_called=True) as mock:
        mock.post("https://api.notion.test/v1/search").mock(side_effect=_capture)
        resp = await client.post(
            "/invoke/notion.list_databases",
            json={},
            headers={"Authorization": "Bearer t"},
        )
    assert resp.status_code == 200
    body = resp.json()["result"]
    assert body["count"] == 1
    assert body["databases"][0]["id"] == DB_ID
    assert body["databases"][0]["title"] == "Tasks"
    # Confirm the search was filtered to databases.
    import json as _json

    sent = _json.loads(captured["body"])
    assert sent["filter"]["value"] == "database"


# ---------------------------------------------------------------------------
# notion.query_database
# ---------------------------------------------------------------------------


async def test_query_database_with_filter_and_sort(client) -> None:
    captured: dict = {}

    def _capture(request):
        captured["body"] = request.read()
        return Response(
            200,
            json={
                "object": "list",
                "results": [
                    {
                        "object": "page",
                        "id": PAGE_ID,
                        "url": "https://www.notion.so/x",
                        "last_edited_time": "2026-05-01T00:00:00Z",
                        "properties": {
                            "Name": {
                                "type": "title",
                                "title": [{"plain_text": "Row 1"}],
                            }
                        },
                    }
                ],
                "has_more": True,
                "next_cursor": "cursor-abc",
            },
        )

    with respx.mock(assert_all_called=True) as mock:
        mock.post(f"https://api.notion.test/v1/databases/{DB_ID}/query").mock(
            side_effect=_capture
        )
        resp = await client.post(
            "/invoke/notion.query_database",
            json={
                "database_id": DB_ID,
                "filter": {"property": "Status", "select": {"equals": "Open"}},
                "sorts": [{"timestamp": "last_edited_time", "direction": "descending"}],
                "page_size": 5,
            },
            headers={"Authorization": "Bearer t"},
        )
    assert resp.status_code == 200
    body = resp.json()["result"]
    assert body["has_more"] is True
    assert body["next_cursor"] == "cursor-abc"
    assert len(body["results"]) == 1
    assert body["results"][0]["title"] == "Row 1"
    import json as _json

    sent = _json.loads(captured["body"])
    assert sent["filter"]["property"] == "Status"
    assert sent["sorts"][0]["direction"] == "descending"
    assert sent["page_size"] == 5


async def test_query_database_with_invalid_filter(client) -> None:
    resp = await client.post(
        "/invoke/notion.query_database",
        json={"database_id": DB_ID, "filter": "not-an-object"},
        headers={"Authorization": "Bearer t"},
    )
    assert resp.status_code == 400


async def test_query_database_with_invalid_sorts(client) -> None:
    resp = await client.post(
        "/invoke/notion.query_database",
        json={"database_id": DB_ID, "sorts": "not-a-list"},
        headers={"Authorization": "Bearer t"},
    )
    assert resp.status_code == 400


# ---------------------------------------------------------------------------
# Unknown tool
# ---------------------------------------------------------------------------


async def test_unknown_tool_returns_404(client) -> None:
    resp = await client.post(
        "/invoke/notion.does_not_exist",
        json={},
        headers={"Authorization": "Bearer t"},
    )
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "TOOL_NOT_FOUND"


async def test_invalid_json_body_returns_400(client) -> None:
    resp = await client.post(
        "/invoke/notion.search",
        content=b"not json",
        headers={
            "Authorization": "Bearer t",
            "Content-Type": "application/json",
        },
    )
    assert resp.status_code == 400


async def test_array_body_rejected(client) -> None:
    resp = await client.post(
        "/invoke/notion.search",
        json=["not an object"],
        headers={"Authorization": "Bearer t"},
    )
    assert resp.status_code == 400


# ---------------------------------------------------------------------------
# ID validation per tool
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "tool_id, payload",
    [
        ("notion.get_page", {"page_id": "/etc/passwd"}),
        ("notion.update_page", {"page_id": "../absolute", "archived": True}),
        ("notion.append_block", {"page_id": "not-a-uuid", "blocks": [{}]}),
        ("notion.query_database", {"database_id": ""}),
        ("notion.create_page", {"parent_database_id": "bad-id", "title": "x"}),
    ],
)
async def test_id_validation_per_tool(client, tool_id: str, payload: dict) -> None:
    resp = await client.post(
        f"/invoke/{tool_id}",
        json=payload,
        headers={"Authorization": "Bearer t"},
    )
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "INVALID_ARGUMENTS"


# ---------------------------------------------------------------------------
# Notion-Version header (consistency check)
# ---------------------------------------------------------------------------


async def test_notion_version_header_sent(client) -> None:
    captured: dict = {}

    def _capture(request):
        captured["version"] = request.headers.get("Notion-Version")
        return Response(200, json={"results": []})

    with respx.mock(assert_all_called=True) as mock:
        mock.post("https://api.notion.test/v1/search").mock(side_effect=_capture)
        await client.post(
            "/invoke/notion.search",
            json={"query": "x"},
            headers={"Authorization": "Bearer t"},
        )
    assert captured["version"] == "2022-06-28"
