# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 The Plinth Authors
"""Tests for the google-workspace-mcp server endpoints + tools."""

from __future__ import annotations

import pytest
import respx
from httpx import Response

from google_workspace_mcp.tools import (
    parse_calendar_id,
    parse_file_id,
    parse_label_id,
)


FILE_ID = "1A2bC3dE4FGhIJklmNOpQRSTuvWXyz_-"
DOC_ID = "abcdefghijklmnopqrstuvwxyz0123456789ABCD"
SHEET_ID = "sheetID-Abc_123-XYZ_456_def_789"


# ---------------------------------------------------------------------------
# Validators
# ---------------------------------------------------------------------------


def test_parse_file_id_accepts_valid() -> None:
    assert parse_file_id(FILE_ID) == FILE_ID


def test_parse_file_id_rejects_path_traversal() -> None:
    from google_workspace_mcp.tools import ToolError

    with pytest.raises(ToolError):
        parse_file_id("../escape")
    with pytest.raises(ToolError):
        parse_file_id("/etc/passwd")


def test_parse_file_id_rejects_short() -> None:
    from google_workspace_mcp.tools import ToolError

    with pytest.raises(ToolError):
        parse_file_id("abc")


def test_parse_file_id_rejects_invalid_chars() -> None:
    from google_workspace_mcp.tools import ToolError

    with pytest.raises(ToolError):
        parse_file_id("with space inside id")
    with pytest.raises(ToolError):
        parse_file_id("with$dollar$signs$here$invalid")


def test_parse_calendar_id_primary() -> None:
    assert parse_calendar_id("primary") == "primary"


def test_parse_calendar_id_email_like() -> None:
    cal = "calendar@group.calendar.google.com"
    assert parse_calendar_id(cal) == cal


def test_parse_calendar_id_rejects_traversal() -> None:
    from google_workspace_mcp.tools import ToolError

    with pytest.raises(ToolError):
        parse_calendar_id("/cal")
    with pytest.raises(ToolError):
        parse_calendar_id("../foo")


def test_parse_label_id_inbox() -> None:
    assert parse_label_id("INBOX") == "INBOX"
    assert parse_label_id("Label_1") == "Label_1"


def test_parse_label_id_rejects_dots() -> None:
    from google_workspace_mcp.tools import ToolError

    with pytest.raises(ToolError):
        parse_label_id("with.dot")
    with pytest.raises(ToolError):
        parse_label_id("")


# ---------------------------------------------------------------------------
# /healthz + /tools
# ---------------------------------------------------------------------------


async def test_healthz(client) -> None:
    resp = await client.get("/healthz")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ok"
    assert body["service"] == "google-workspace-mcp"
    assert body["version"] == "1.1.0"


async def test_tools_listing_has_eight_tools(client) -> None:
    resp = await client.get("/tools")
    assert resp.status_code == 200
    body = resp.json()
    assert len(body["tools"]) == 8
    tool_ids = {t["tool_id"] for t in body["tools"]}
    assert tool_ids == {
        "google.drive_search",
        "google.drive_read",
        "google.docs_create",
        "google.docs_append",
        "google.sheets_read",
        "google.sheets_append_row",
        "google.calendar_list_events",
        "google.gmail_list_messages",
    }
    for t in body["tools"]:
        assert t["auth_method"] == "oauth2"
        assert t["auth_config"] == {"provider": "google"}


# ---------------------------------------------------------------------------
# Auth — missing token
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "tool_id, payload",
    [
        ("google.drive_search", {"query": "name='x'"}),
        ("google.drive_read", {"file_id": FILE_ID}),
        ("google.docs_create", {"title": "x"}),
        ("google.docs_append", {"document_id": DOC_ID, "content": "hi"}),
        ("google.sheets_read", {"spreadsheet_id": SHEET_ID, "range": "A1:B2"}),
        (
            "google.sheets_append_row",
            {"spreadsheet_id": SHEET_ID, "range": "A1:B2", "values": ["a"]},
        ),
        ("google.calendar_list_events", {}),
        ("google.gmail_list_messages", {}),
    ],
)
async def test_unauthorized_without_bearer(client, tool_id: str, payload: dict) -> None:
    resp = await client.post(f"/invoke/{tool_id}", json=payload)
    assert resp.status_code == 401
    body = resp.json()
    assert body["error"]["code"] == "UNAUTHORIZED"


async def test_unauthorized_with_non_bearer_scheme(client) -> None:
    resp = await client.post(
        "/invoke/google.drive_search",
        json={"query": "x"},
        headers={"Authorization": "Basic abc"},
    )
    assert resp.status_code == 401


# ---------------------------------------------------------------------------
# google.drive_search
# ---------------------------------------------------------------------------


async def test_drive_search_returns_files(client) -> None:
    captured: dict = {}

    def _capture(request):
        captured["url"] = str(request.url)
        captured["auth"] = request.headers.get("Authorization")
        return Response(
            200,
            json={
                "files": [
                    {
                        "id": FILE_ID,
                        "name": "Spec.gdoc",
                        "mimeType": "application/vnd.google-apps.document",
                        "webViewLink": "https://docs.google.com/document/d/X",
                        "modifiedTime": "2026-05-01T00:00:00Z",
                    },
                    {
                        "id": "another-id-here-1234",
                        "name": "Notes.txt",
                        "mimeType": "text/plain",
                        "webViewLink": None,
                        "modifiedTime": "2026-05-02T00:00:00Z",
                    },
                ],
                "nextPageToken": "tok-1",
            },
        )

    with respx.mock(assert_all_called=True) as mock:
        mock.get("https://drive.test/drive/v3/files").mock(side_effect=_capture)
        resp = await client.post(
            "/invoke/google.drive_search",
            json={"query": "name contains 'spec'", "page_size": 5},
            headers={"Authorization": "Bearer secret"},
        )
    assert resp.status_code == 200
    body = resp.json()["result"]
    assert body["count"] == 2
    assert body["nextPageToken"] == "tok-1"
    assert body["files"][0]["name"] == "Spec.gdoc"
    assert "spec" in captured["url"].lower() or "spec" in captured["url"]
    assert captured["auth"] == "Bearer secret"


async def test_drive_search_requires_query(client) -> None:
    resp = await client.post(
        "/invoke/google.drive_search",
        json={"query": ""},
        headers={"Authorization": "Bearer t"},
    )
    assert resp.status_code == 400


async def test_drive_search_page_size_validation(client) -> None:
    resp = await client.post(
        "/invoke/google.drive_search",
        json={"query": "x", "page_size": 0},
        headers={"Authorization": "Bearer t"},
    )
    assert resp.status_code == 400
    resp = await client.post(
        "/invoke/google.drive_search",
        json={"query": "x", "page_size": 200},
        headers={"Authorization": "Bearer t"},
    )
    assert resp.status_code == 400


async def test_drive_search_propagates_401(client) -> None:
    with respx.mock(assert_all_called=True) as mock:
        mock.get("https://drive.test/drive/v3/files").mock(
            return_value=Response(401, json={"error": {"message": "bad creds"}})
        )
        resp = await client.post(
            "/invoke/google.drive_search",
            json={"query": "x"},
            headers={"Authorization": "Bearer bad"},
        )
    assert resp.status_code == 401


# ---------------------------------------------------------------------------
# google.drive_read
# ---------------------------------------------------------------------------


async def test_drive_read_native_doc_uses_export(client) -> None:
    captured: dict = {}

    def _capture_export(request):
        captured["export_url"] = str(request.url)
        return Response(200, text="exported text content")

    with respx.mock(assert_all_called=True) as mock:
        mock.get(f"https://drive.test/drive/v3/files/{FILE_ID}").mock(
            return_value=Response(
                200,
                json={
                    "id": FILE_ID,
                    "name": "My Doc",
                    "mimeType": "application/vnd.google-apps.document",
                    "modifiedTime": "2026-05-01T00:00:00Z",
                },
            )
        )
        mock.get(f"https://drive.test/drive/v3/files/{FILE_ID}/export").mock(
            side_effect=_capture_export
        )
        resp = await client.post(
            "/invoke/google.drive_read",
            json={"file_id": FILE_ID},
            headers={"Authorization": "Bearer t"},
        )
    assert resp.status_code == 200
    body = resp.json()["result"]
    assert body["content"] == "exported text content"
    assert body["mimeType"] == "text/plain"
    assert "mimeType=text" in captured["export_url"]


async def test_drive_read_explicit_mime_override(client) -> None:
    captured: dict = {}

    def _capture_export(request):
        captured["url"] = str(request.url)
        return Response(200, text="csv,row,here")

    with respx.mock(assert_all_called=True) as mock:
        mock.get(f"https://drive.test/drive/v3/files/{FILE_ID}").mock(
            return_value=Response(
                200,
                json={
                    "id": FILE_ID,
                    "name": "Sheet",
                    "mimeType": "application/vnd.google-apps.spreadsheet",
                },
            )
        )
        mock.get(f"https://drive.test/drive/v3/files/{FILE_ID}/export").mock(
            side_effect=_capture_export
        )
        resp = await client.post(
            "/invoke/google.drive_read",
            json={"file_id": FILE_ID, "mime_type": "text/csv"},
            headers={"Authorization": "Bearer t"},
        )
    assert resp.status_code == 200
    body = resp.json()["result"]
    assert body["mimeType"] == "text/csv"
    assert "csv" in captured["url"]


async def test_drive_read_non_native_uses_alt_media(client) -> None:
    with respx.mock(assert_all_called=True) as mock:
        mock.get(f"https://drive.test/drive/v3/files/{FILE_ID}").mock(
            side_effect=[
                Response(
                    200,
                    json={
                        "id": FILE_ID,
                        "name": "report.pdf",
                        "mimeType": "application/pdf",
                    },
                ),
                Response(200, text="raw-pdf-bytes"),
            ]
        )
        resp = await client.post(
            "/invoke/google.drive_read",
            json={"file_id": FILE_ID},
            headers={"Authorization": "Bearer t"},
        )
    assert resp.status_code == 200
    body = resp.json()["result"]
    assert body["mimeType"] == "application/pdf"
    assert body["content"] == "raw-pdf-bytes"


async def test_drive_read_404(client) -> None:
    with respx.mock(assert_all_called=True) as mock:
        mock.get(f"https://drive.test/drive/v3/files/{FILE_ID}").mock(
            return_value=Response(404, json={"error": {"message": "not found"}})
        )
        resp = await client.post(
            "/invoke/google.drive_read",
            json={"file_id": FILE_ID},
            headers={"Authorization": "Bearer t"},
        )
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# google.docs_create
# ---------------------------------------------------------------------------


async def test_docs_create_without_content(client) -> None:
    captured: dict = {}

    def _capture(request):
        captured["body"] = request.read()
        return Response(200, json={"documentId": DOC_ID, "title": "Hello"})

    with respx.mock(assert_all_called=True) as mock:
        mock.post("https://docs.test/v1/documents").mock(side_effect=_capture)
        resp = await client.post(
            "/invoke/google.docs_create",
            json={"title": "Hello"},
            headers={"Authorization": "Bearer t"},
        )
    assert resp.status_code == 200
    body = resp.json()["result"]
    assert body["document_id"] == DOC_ID
    assert "/edit" in body["url"]
    import json as _json

    sent = _json.loads(captured["body"])
    assert sent == {"title": "Hello"}


async def test_docs_create_with_content_inserts_text(client) -> None:
    captured: dict = {}

    def _capture_create(request):
        return Response(200, json={"documentId": DOC_ID})

    def _capture_update(request):
        captured["update_body"] = request.read()
        return Response(200, json={"documentId": DOC_ID})

    with respx.mock(assert_all_called=True) as mock:
        mock.post("https://docs.test/v1/documents").mock(side_effect=_capture_create)
        mock.post(
            f"https://docs.test/v1/documents/{DOC_ID}:batchUpdate"
        ).mock(side_effect=_capture_update)
        resp = await client.post(
            "/invoke/google.docs_create",
            json={"title": "Hello", "content": "Initial body."},
            headers={"Authorization": "Bearer t"},
        )
    assert resp.status_code == 200
    import json as _json

    sent = _json.loads(captured["update_body"])
    assert sent["requests"][0]["insertText"]["text"] == "Initial body."


async def test_docs_create_requires_title(client) -> None:
    resp = await client.post(
        "/invoke/google.docs_create",
        json={"title": "  "},
        headers={"Authorization": "Bearer t"},
    )
    assert resp.status_code == 400


# ---------------------------------------------------------------------------
# google.docs_append
# ---------------------------------------------------------------------------


async def test_docs_append_inserts_at_end(client) -> None:
    captured: dict = {}

    def _capture_update(request):
        captured["update_body"] = request.read()
        return Response(200, json={"documentId": DOC_ID, "writeControl": {}})

    with respx.mock(assert_all_called=True) as mock:
        mock.get(f"https://docs.test/v1/documents/{DOC_ID}").mock(
            return_value=Response(
                200,
                json={
                    "documentId": DOC_ID,
                    "body": {
                        "content": [
                            {"endIndex": 1},
                            {"endIndex": 47},
                        ]
                    },
                },
            )
        )
        mock.post(
            f"https://docs.test/v1/documents/{DOC_ID}:batchUpdate"
        ).mock(side_effect=_capture_update)
        resp = await client.post(
            "/invoke/google.docs_append",
            json={"document_id": DOC_ID, "content": "Appended text."},
            headers={"Authorization": "Bearer t"},
        )
    assert resp.status_code == 200
    body = resp.json()["result"]
    assert body["document_id"] == DOC_ID
    import json as _json

    sent = _json.loads(captured["update_body"])
    insert = sent["requests"][0]["insertText"]
    assert insert["text"] == "Appended text."
    # The doc's max endIndex is 47 — insert should be at 46.
    assert insert["location"]["index"] == 46


async def test_docs_append_requires_content(client) -> None:
    resp = await client.post(
        "/invoke/google.docs_append",
        json={"document_id": DOC_ID, "content": ""},
        headers={"Authorization": "Bearer t"},
    )
    assert resp.status_code == 400


# ---------------------------------------------------------------------------
# google.sheets_read
# ---------------------------------------------------------------------------


async def test_sheets_read_returns_values(client) -> None:
    with respx.mock(assert_all_called=True) as mock:
        mock.get(
            f"https://sheets.test/v4/spreadsheets/{SHEET_ID}/values/Sheet1!A1:B2"
        ).mock(
            return_value=Response(
                200,
                json={
                    "range": "Sheet1!A1:B2",
                    "majorDimension": "ROWS",
                    "values": [["a", "b"], ["1", "2"]],
                },
            )
        )
        resp = await client.post(
            "/invoke/google.sheets_read",
            json={"spreadsheet_id": SHEET_ID, "range": "Sheet1!A1:B2"},
            headers={"Authorization": "Bearer t"},
        )
    assert resp.status_code == 200
    body = resp.json()["result"]
    assert body["values"] == [["a", "b"], ["1", "2"]]
    assert body["range"] == "Sheet1!A1:B2"


async def test_sheets_read_requires_range(client) -> None:
    resp = await client.post(
        "/invoke/google.sheets_read",
        json={"spreadsheet_id": SHEET_ID, "range": ""},
        headers={"Authorization": "Bearer t"},
    )
    assert resp.status_code == 400


async def test_sheets_read_rejects_path_in_range(client) -> None:
    resp = await client.post(
        "/invoke/google.sheets_read",
        json={"spreadsheet_id": SHEET_ID, "range": "../etc/passwd"},
        headers={"Authorization": "Bearer t"},
    )
    assert resp.status_code == 400


# ---------------------------------------------------------------------------
# google.sheets_append_row
# ---------------------------------------------------------------------------


async def test_sheets_append_row(client) -> None:
    captured: dict = {}

    def _capture(request):
        captured["body"] = request.read()
        captured["params"] = str(request.url)
        return Response(
            200,
            json={
                "updates": {
                    "spreadsheetId": SHEET_ID,
                    "updatedRange": "Sheet1!A4:C4",
                    "updatedRows": 1,
                    "updatedColumns": 3,
                    "updatedCells": 3,
                },
            },
        )

    with respx.mock(assert_all_called=True) as mock:
        mock.post(
            f"https://sheets.test/v4/spreadsheets/{SHEET_ID}/values/Sheet1!A1:C1:append"
        ).mock(side_effect=_capture)
        resp = await client.post(
            "/invoke/google.sheets_append_row",
            json={
                "spreadsheet_id": SHEET_ID,
                "range": "Sheet1!A1:C1",
                "values": ["alpha", "beta", "gamma"],
            },
            headers={"Authorization": "Bearer t"},
        )
    assert resp.status_code == 200
    body = resp.json()["result"]
    assert body["updates"]["updatedCells"] == 3
    assert "valueInputOption=USER_ENTERED" in captured["params"]
    import json as _json

    sent = _json.loads(captured["body"])
    assert sent == {"values": [["alpha", "beta", "gamma"]]}


async def test_sheets_append_row_requires_values(client) -> None:
    resp = await client.post(
        "/invoke/google.sheets_append_row",
        json={"spreadsheet_id": SHEET_ID, "range": "A1:B1", "values": []},
        headers={"Authorization": "Bearer t"},
    )
    assert resp.status_code == 400


# ---------------------------------------------------------------------------
# google.calendar_list_events
# ---------------------------------------------------------------------------


async def test_calendar_list_events_default_primary(client) -> None:
    with respx.mock(assert_all_called=True) as mock:
        mock.get("https://calendar.test/calendar/v3/calendars/primary/events").mock(
            return_value=Response(
                200,
                json={
                    "items": [
                        {
                            "id": "evt1",
                            "summary": "Standup",
                            "start": {"dateTime": "2026-05-10T09:00:00Z"},
                            "end": {"dateTime": "2026-05-10T09:30:00Z"},
                            "attendees": [
                                {"email": "a@b.com", "responseStatus": "accepted"}
                            ],
                            "htmlLink": "https://cal.example/evt1",
                            "status": "confirmed",
                        }
                    ],
                    "nextPageToken": "page-2",
                },
            )
        )
        resp = await client.post(
            "/invoke/google.calendar_list_events",
            json={},
            headers={"Authorization": "Bearer t"},
        )
    assert resp.status_code == 200
    body = resp.json()["result"]
    assert body["count"] == 1
    assert body["events"][0]["summary"] == "Standup"
    assert body["events"][0]["attendees"][0]["email"] == "a@b.com"
    assert body["nextPageToken"] == "page-2"


async def test_calendar_list_events_with_explicit_calendar(client) -> None:
    with respx.mock(assert_all_called=True) as mock:
        mock.get(
            "https://calendar.test/calendar/v3/calendars/team@group.calendar.google.com/events"
        ).mock(return_value=Response(200, json={"items": []}))
        resp = await client.post(
            "/invoke/google.calendar_list_events",
            json={
                "calendar_id": "team@group.calendar.google.com",
                "max_results": 5,
            },
            headers={"Authorization": "Bearer t"},
        )
    assert resp.status_code == 200
    assert resp.json()["result"]["events"] == []


async def test_calendar_max_results_validation(client) -> None:
    resp = await client.post(
        "/invoke/google.calendar_list_events",
        json={"max_results": 0},
        headers={"Authorization": "Bearer t"},
    )
    assert resp.status_code == 400
    resp = await client.post(
        "/invoke/google.calendar_list_events",
        json={"max_results": 1000},
        headers={"Authorization": "Bearer t"},
    )
    assert resp.status_code == 400


# ---------------------------------------------------------------------------
# google.gmail_list_messages
# ---------------------------------------------------------------------------


async def test_gmail_list_messages_inbox(client) -> None:
    captured: dict = {}

    def _capture_list(request):
        captured["list_url"] = str(request.url)
        return Response(
            200,
            json={
                "messages": [
                    {"id": "m1", "threadId": "t1"},
                    {"id": "m2", "threadId": "t2"},
                ],
                "resultSizeEstimate": 2,
            },
        )

    def _msg_response(msg_id: str, subject: str, sender: str, date: str):
        return Response(
            200,
            json={
                "id": msg_id,
                "threadId": "t",
                "snippet": f"snippet for {msg_id}",
                "labelIds": ["INBOX", "UNREAD"],
                "payload": {
                    "headers": [
                        {"name": "Subject", "value": subject},
                        {"name": "From", "value": sender},
                        {"name": "Date", "value": date},
                    ]
                },
            },
        )

    with respx.mock(assert_all_called=True) as mock:
        mock.get("https://gmail.test/gmail/v1/users/me/messages").mock(
            side_effect=_capture_list
        )
        mock.get("https://gmail.test/gmail/v1/users/me/messages/m1").mock(
            return_value=_msg_response("m1", "Hello world", "alice@example.com", "Mon, 10 May 2026 09:00:00 +0000")
        )
        mock.get("https://gmail.test/gmail/v1/users/me/messages/m2").mock(
            return_value=_msg_response("m2", "Reminder", "bob@example.com", "Mon, 10 May 2026 10:00:00 +0000")
        )
        resp = await client.post(
            "/invoke/google.gmail_list_messages",
            json={"max_results": 2},
            headers={"Authorization": "Bearer t"},
        )
    assert resp.status_code == 200
    body = resp.json()["result"]
    assert body["count"] == 2
    assert body["messages"][0]["subject"] == "Hello world"
    assert body["messages"][0]["from"] == "alice@example.com"
    # Confirm format=metadata + Subject/From/Date were requested.
    assert "labelIds=INBOX" in captured["list_url"]


async def test_gmail_list_messages_query_filter(client) -> None:
    captured: dict = {}

    def _capture_list(request):
        captured["list_url"] = str(request.url)
        return Response(200, json={"messages": []})

    with respx.mock(assert_all_called=True) as mock:
        mock.get("https://gmail.test/gmail/v1/users/me/messages").mock(
            side_effect=_capture_list
        )
        resp = await client.post(
            "/invoke/google.gmail_list_messages",
            json={
                "label_ids": ["INBOX"],
                "query": "is:unread",
                "max_results": 5,
            },
            headers={"Authorization": "Bearer t"},
        )
    assert resp.status_code == 200
    assert resp.json()["result"]["messages"] == []
    assert "q=is" in captured["list_url"]


async def test_gmail_list_messages_label_validation(client) -> None:
    resp = await client.post(
        "/invoke/google.gmail_list_messages",
        json={"label_ids": []},
        headers={"Authorization": "Bearer t"},
    )
    assert resp.status_code == 400
    resp = await client.post(
        "/invoke/google.gmail_list_messages",
        json={"label_ids": ["INVALID.LABEL"]},
        headers={"Authorization": "Bearer t"},
    )
    assert resp.status_code == 400


async def test_gmail_max_results_validation(client) -> None:
    resp = await client.post(
        "/invoke/google.gmail_list_messages",
        json={"max_results": 0},
        headers={"Authorization": "Bearer t"},
    )
    assert resp.status_code == 400
    resp = await client.post(
        "/invoke/google.gmail_list_messages",
        json={"max_results": 100},
        headers={"Authorization": "Bearer t"},
    )
    assert resp.status_code == 400


# ---------------------------------------------------------------------------
# Unknown tool / generic body errors
# ---------------------------------------------------------------------------


async def test_unknown_tool_returns_404(client) -> None:
    resp = await client.post(
        "/invoke/google.does_not_exist",
        json={},
        headers={"Authorization": "Bearer t"},
    )
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "TOOL_NOT_FOUND"


async def test_invalid_json_body_returns_400(client) -> None:
    resp = await client.post(
        "/invoke/google.drive_search",
        content=b"not json",
        headers={
            "Authorization": "Bearer t",
            "Content-Type": "application/json",
        },
    )
    assert resp.status_code == 400


async def test_array_body_rejected(client) -> None:
    resp = await client.post(
        "/invoke/google.drive_search",
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
        ("google.drive_read", {"file_id": "/etc/passwd"}),
        ("google.drive_read", {"file_id": "../escape"}),
        ("google.docs_append", {"document_id": "abc", "content": "x"}),
        ("google.sheets_read", {"spreadsheet_id": "", "range": "A1"}),
        (
            "google.sheets_append_row",
            {"spreadsheet_id": "with space", "range": "A1", "values": ["x"]},
        ),
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
