# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 The Plinth Authors
"""Tests for the slack-mcp server endpoints + tools."""

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
    assert body["service"] == "slack-mcp"
    assert body["version"] == "0.4.0"


async def test_tools_listing_has_four_tools(client) -> None:
    resp = await client.get("/tools")
    assert resp.status_code == 200
    body = resp.json()
    assert len(body["tools"]) == 4
    tool_ids = {t["tool_id"] for t in body["tools"]}
    assert tool_ids == {
        "slack.list_channels",
        "slack.post_message",
        "slack.list_messages",
        "slack.get_user",
    }
    for t in body["tools"]:
        assert t["auth_method"] == "oauth2"
        assert t["auth_config"] == {"provider": "slack"}


# ---------------------------------------------------------------------------
# Auth — missing token
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "tool_id, payload",
    [
        ("slack.list_channels", {}),
        ("slack.post_message", {"channel": "C1", "text": "hi"}),
        ("slack.list_messages", {"channel": "C1"}),
        ("slack.get_user", {"user": "U1"}),
    ],
)
async def test_unauthorized_without_bearer(client, tool_id: str, payload: dict) -> None:
    resp = await client.post(f"/invoke/{tool_id}", json=payload)
    assert resp.status_code == 401
    body = resp.json()
    assert body["error"]["code"] == "UNAUTHORIZED"


async def test_unauthorized_with_non_bearer_scheme(client) -> None:
    resp = await client.post(
        "/invoke/slack.list_channels",
        json={},
        headers={"Authorization": "Basic abc"},
    )
    assert resp.status_code == 401


# ---------------------------------------------------------------------------
# slack.list_channels
# ---------------------------------------------------------------------------


async def test_list_channels_returns_slimmed_list(client) -> None:
    with respx.mock(assert_all_called=True) as mock:
        mock.get("https://slack.test/api/conversations.list").mock(
            return_value=Response(
                200,
                json={
                    "ok": True,
                    "channels": [
                        {
                            "id": "C1",
                            "name": "general",
                            "is_private": False,
                            "is_archived": False,
                            "is_member": True,
                            "topic": {"value": "Welcome"},
                            "purpose": {"value": "Chat"},
                            "num_members": 42,
                        },
                        {
                            "id": "C2",
                            "name": "random",
                            "is_private": False,
                            "is_archived": False,
                            "is_member": False,
                            "topic": {"value": ""},
                            "purpose": {"value": ""},
                            "num_members": 5,
                        },
                    ],
                    "response_metadata": {"next_cursor": "ZW1wYXBl"},
                },
            )
        )
        resp = await client.post(
            "/invoke/slack.list_channels",
            json={},
            headers={"Authorization": "Bearer xoxb-token"},
        )
    assert resp.status_code == 200
    result = resp.json()["result"]
    assert result["count"] == 2
    assert result["channels"][0]["id"] == "C1"
    assert result["channels"][0]["name"] == "general"
    assert result["channels"][0]["topic"] == "Welcome"
    assert result["next_cursor"] == "ZW1wYXBl"


async def test_list_channels_propagates_invalid_auth(client) -> None:
    """Slack returns 200 with ok=false on auth errors — we translate to 401."""
    with respx.mock(assert_all_called=True) as mock:
        mock.get("https://slack.test/api/conversations.list").mock(
            return_value=Response(200, json={"ok": False, "error": "invalid_auth"})
        )
        resp = await client.post(
            "/invoke/slack.list_channels",
            json={},
            headers={"Authorization": "Bearer xoxb-bad"},
        )
    assert resp.status_code == 401
    assert resp.json()["error"]["code"] == "UNAUTHORIZED"


async def test_list_channels_propagates_other_slack_errors(client) -> None:
    with respx.mock(assert_all_called=True) as mock:
        mock.get("https://slack.test/api/conversations.list").mock(
            return_value=Response(200, json={"ok": False, "error": "ratelimited"})
        )
        resp = await client.post(
            "/invoke/slack.list_channels",
            json={},
            headers={"Authorization": "Bearer xoxb-x"},
        )
    assert resp.status_code == 400
    body = resp.json()
    assert body["error"]["code"] == "TOOL_INVOCATION_FAILED"
    assert body["error"]["details"]["slack_error"] == "ratelimited"


# ---------------------------------------------------------------------------
# slack.post_message
# ---------------------------------------------------------------------------


async def test_post_message_sends_json_body(client) -> None:
    captured: dict = {}

    def _capture(request):
        captured["body"] = request.read()
        captured["auth"] = request.headers.get("Authorization")
        captured["content_type"] = request.headers.get("Content-Type")
        return Response(
            200,
            json={
                "ok": True,
                "channel": "C1",
                "ts": "1700000000.000100",
                "message": {
                    "type": "message",
                    "user": "U1",
                    "text": "hello",
                    "ts": "1700000000.000100",
                },
            },
        )

    with respx.mock(assert_all_called=True) as mock:
        mock.post("https://slack.test/api/chat.postMessage").mock(side_effect=_capture)
        resp = await client.post(
            "/invoke/slack.post_message",
            json={"channel": "C1", "text": "hello"},
            headers={"Authorization": "Bearer xoxb-test"},
        )
    assert resp.status_code == 200
    assert captured["auth"] == "Bearer xoxb-test"
    assert captured["content_type"].startswith("application/json")
    sent = json.loads(captured["body"])
    assert sent == {"channel": "C1", "text": "hello"}
    assert resp.json()["result"]["ts"] == "1700000000.000100"


async def test_post_message_supports_thread_ts(client) -> None:
    captured: dict = {}

    def _capture(request):
        captured["body"] = request.read()
        return Response(
            200,
            json={
                "ok": True,
                "channel": "C1",
                "ts": "1700000000.000200",
                "message": {"text": "reply", "user": "U1", "ts": "..."},
            },
        )

    with respx.mock(assert_all_called=True) as mock:
        mock.post("https://slack.test/api/chat.postMessage").mock(side_effect=_capture)
        resp = await client.post(
            "/invoke/slack.post_message",
            json={"channel": "C1", "text": "reply", "thread_ts": "1700000000.000100"},
            headers={"Authorization": "Bearer t"},
        )
    assert resp.status_code == 200
    sent = json.loads(captured["body"])
    assert sent["thread_ts"] == "1700000000.000100"


async def test_post_message_requires_text_or_blocks(client) -> None:
    resp = await client.post(
        "/invoke/slack.post_message",
        json={"channel": "C1"},
        headers={"Authorization": "Bearer t"},
    )
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "INVALID_ARGUMENTS"


async def test_post_message_requires_channel(client) -> None:
    resp = await client.post(
        "/invoke/slack.post_message",
        json={"text": "x"},
        headers={"Authorization": "Bearer t"},
    )
    assert resp.status_code == 400


async def test_post_message_translates_slack_error(client) -> None:
    """``channel_not_found`` is a typical app-level Slack error."""
    with respx.mock(assert_all_called=True) as mock:
        mock.post("https://slack.test/api/chat.postMessage").mock(
            return_value=Response(200, json={"ok": False, "error": "channel_not_found"})
        )
        resp = await client.post(
            "/invoke/slack.post_message",
            json={"channel": "Cbogus", "text": "hi"},
            headers={"Authorization": "Bearer t"},
        )
    assert resp.status_code == 400
    body = resp.json()
    assert body["error"]["details"]["slack_error"] == "channel_not_found"


# ---------------------------------------------------------------------------
# slack.list_messages
# ---------------------------------------------------------------------------


async def test_list_messages_returns_slimmed_messages(client) -> None:
    with respx.mock(assert_all_called=True) as mock:
        mock.get("https://slack.test/api/conversations.history").mock(
            return_value=Response(
                200,
                json={
                    "ok": True,
                    "messages": [
                        {
                            "type": "message",
                            "user": "U1",
                            "text": "hi",
                            "ts": "1700000000.001",
                        },
                        {
                            "type": "message",
                            "user": "U2",
                            "text": "hey",
                            "ts": "1700000010.002",
                            "thread_ts": "1700000000.001",
                            "reply_count": 0,
                        },
                    ],
                    "has_more": False,
                    "response_metadata": {"next_cursor": ""},
                },
            )
        )
        resp = await client.post(
            "/invoke/slack.list_messages",
            json={"channel": "C1", "limit": 10},
            headers={"Authorization": "Bearer t"},
        )
    assert resp.status_code == 200
    result = resp.json()["result"]
    assert result["count"] == 2
    assert result["has_more"] is False
    assert result["messages"][0]["text"] == "hi"
    assert result["messages"][1]["thread_ts"] == "1700000000.001"


async def test_list_messages_requires_channel(client) -> None:
    resp = await client.post(
        "/invoke/slack.list_messages",
        json={},
        headers={"Authorization": "Bearer t"},
    )
    assert resp.status_code == 400


# ---------------------------------------------------------------------------
# slack.get_user
# ---------------------------------------------------------------------------


async def test_get_user_returns_slim_user(client) -> None:
    with respx.mock(assert_all_called=True) as mock:
        mock.get("https://slack.test/api/users.info").mock(
            return_value=Response(
                200,
                json={
                    "ok": True,
                    "user": {
                        "id": "U123",
                        "name": "alice",
                        "real_name": "Alice Liddell",
                        "is_bot": False,
                        "is_admin": True,
                        "deleted": False,
                        "tz": "Europe/Berlin",
                        "profile": {
                            "real_name": "Alice Liddell",
                            "email": "alice@example.com",
                        },
                    },
                },
            )
        )
        resp = await client.post(
            "/invoke/slack.get_user",
            json={"user": "U123"},
            headers={"Authorization": "Bearer t"},
        )
    assert resp.status_code == 200
    user = resp.json()["result"]["user"]
    assert user["id"] == "U123"
    assert user["email"] == "alice@example.com"
    assert user["is_admin"] is True


async def test_get_user_translates_user_not_found(client) -> None:
    with respx.mock(assert_all_called=True) as mock:
        mock.get("https://slack.test/api/users.info").mock(
            return_value=Response(200, json={"ok": False, "error": "user_not_found"})
        )
        resp = await client.post(
            "/invoke/slack.get_user",
            json={"user": "Uxxx"},
            headers={"Authorization": "Bearer t"},
        )
    assert resp.status_code == 400
    assert resp.json()["error"]["details"]["slack_error"] == "user_not_found"


async def test_get_user_requires_user_arg(client) -> None:
    resp = await client.post(
        "/invoke/slack.get_user",
        json={},
        headers={"Authorization": "Bearer t"},
    )
    assert resp.status_code == 400


# ---------------------------------------------------------------------------
# Generic / unknown / bad input
# ---------------------------------------------------------------------------


async def test_unknown_tool_returns_404(client) -> None:
    resp = await client.post(
        "/invoke/slack.does_not_exist",
        json={},
        headers={"Authorization": "Bearer t"},
    )
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "TOOL_NOT_FOUND"


async def test_invalid_json_body_returns_400(client) -> None:
    resp = await client.post(
        "/invoke/slack.list_channels",
        content=b"not json",
        headers={
            "Authorization": "Bearer t",
            "Content-Type": "application/json",
        },
    )
    assert resp.status_code == 400


async def test_array_body_rejected(client) -> None:
    resp = await client.post(
        "/invoke/slack.list_channels",
        json=["not an object"],
        headers={"Authorization": "Bearer t"},
    )
    assert resp.status_code == 400
