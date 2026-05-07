# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 The Plinth Authors
"""Tool implementations for the Slack MCP server.

Each tool is a small wrapper around a Slack Web API endpoint. Every tool
reads the bearer token from the :class:`ToolContext` passed by the route
handler; the route handler in turn lifts it off the inbound ``Authorization``
header.

Slack's Web API has two notable quirks we handle uniformly here:

* **GET endpoints** (``conversations.list``, ``conversations.history``,
  ``users.info``) accept query parameters and return JSON.
* **POST endpoints** (``chat.postMessage``) accept a JSON body with
  ``Content-Type: application/json; charset=utf-8`` *and* the bearer token.
* Slack always returns ``HTTP 200`` even on application-level errors. The
  body is shaped ``{"ok": false, "error": "<code>"}``. We translate that
  into a Plinth :class:`ToolError` so callers don't have to special-case
  successful 2xx responses that actually failed.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Literal

import httpx

from .logging_config import get_logger

logger = get_logger(__name__)


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class ToolError(Exception):
    """A tool-level error that maps to a Plinth error envelope."""

    def __init__(
        self,
        code: str,
        message: str,
        status_code: int = 400,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status_code = status_code
        self.details = details or {}


class Unauthorized(ToolError):
    def __init__(self, message: str = "missing or invalid Slack access token") -> None:
        super().__init__("UNAUTHORIZED", message, status_code=401)


# ---------------------------------------------------------------------------
# Context
# ---------------------------------------------------------------------------


@dataclass
class ToolContext:
    """Per-request state injected into tool invocations."""

    access_token: str | None
    http_client: httpx.AsyncClient
    api_base_url: str = "https://slack.com/api"
    timeout: float = 15.0

    def require_token(self) -> str:
        if not self.access_token:
            raise Unauthorized()
        return self.access_token

    def headers(self, *, json_body: bool = False) -> dict[str, str]:
        headers = {
            "Authorization": f"Bearer {self.require_token()}",
            "Accept": "application/json",
            "User-Agent": "plinth-slack-mcp/0.4.0",
        }
        if json_body:
            # Slack requires this exact charset on chat.postMessage when
            # POSTing JSON, otherwise it falls back to form-encoded parsing.
            headers["Content-Type"] = "application/json; charset=utf-8"
        return headers

    def url(self, path: str) -> str:
        if not path.startswith("/"):
            path = "/" + path
        return f"{self.api_base_url.rstrip('/')}{path}"


# ---------------------------------------------------------------------------
# Tool record
# ---------------------------------------------------------------------------


ToolHandler = Callable[[dict[str, Any], ToolContext], Awaitable[dict[str, Any]]]


@dataclass(frozen=True)
class Tool:
    """Static description of a tool.

    Mirrors :class:`plinth_gateway.models.ToolRegistration` so a Plinth gateway
    can register the tool verbatim from ``GET /tools``.
    """

    tool_id: str
    name: str
    description: str
    input_schema: dict[str, Any]
    output_schema: dict[str, Any]
    idempotent: bool
    side_effects: Literal["none", "read", "write"]
    cache_ttl_seconds: int | None
    handler: ToolHandler

    def to_metadata(self) -> dict[str, Any]:
        return {
            "tool_id": self.tool_id,
            "name": self.name,
            "description": self.description,
            "input_schema": self.input_schema,
            "output_schema": self.output_schema,
            "idempotent": self.idempotent,
            "side_effects": self.side_effects,
            "cache_ttl_seconds": self.cache_ttl_seconds,
            "auth_method": "oauth2",
            "auth_config": {"provider": "slack"},
        }


# ---------------------------------------------------------------------------
# HTTP helpers
# ---------------------------------------------------------------------------


async def _do_get(
    ctx: ToolContext, path: str, *, params: dict[str, Any] | None = None
) -> dict[str, Any]:
    try:
        resp = await ctx.http_client.get(
            ctx.url(path),
            params=params,
            headers=ctx.headers(),
            timeout=ctx.timeout,
        )
    except httpx.HTTPError as exc:
        raise ToolError(
            "TOOL_INVOCATION_FAILED",
            f"Slack request failed: {exc}",
            status_code=502,
            details={"path": path},
        ) from exc
    return _decode_response(resp, path=path)


async def _do_post_json(
    ctx: ToolContext, path: str, *, body: dict[str, Any]
) -> dict[str, Any]:
    try:
        resp = await ctx.http_client.post(
            ctx.url(path),
            json=body,
            headers=ctx.headers(json_body=True),
            timeout=ctx.timeout,
        )
    except httpx.HTTPError as exc:
        raise ToolError(
            "TOOL_INVOCATION_FAILED",
            f"Slack request failed: {exc}",
            status_code=502,
            details={"path": path},
        ) from exc
    return _decode_response(resp, path=path)


def _decode_response(resp: httpx.Response, *, path: str) -> dict[str, Any]:
    """Parse a Slack response, translating ``ok=false`` into a ToolError.

    Slack returns 200 for application errors (with ``ok=false, error=<code>``)
    so the HTTP status alone is misleading. We treat ``invalid_auth`` and
    ``not_authed`` as 401s; everything else as a 502 bad-gateway-ish error.
    """
    if resp.status_code == 401:
        raise Unauthorized("Slack rejected the access token (401)")
    if resp.status_code >= 400:
        body_preview = resp.text[:300]
        raise ToolError(
            "TOOL_INVOCATION_FAILED",
            f"Slack returned HTTP {resp.status_code}",
            status_code=502,
            details={"path": path, "status_code": resp.status_code, "body_preview": body_preview},
        )
    try:
        body = resp.json()
    except ValueError as exc:
        raise ToolError(
            "TOOL_INVOCATION_FAILED",
            f"Slack returned non-JSON: {exc}",
            status_code=502,
            details={"path": path},
        ) from exc
    if not isinstance(body, dict):
        raise ToolError(
            "TOOL_INVOCATION_FAILED",
            "Slack returned a non-object JSON response",
            status_code=502,
            details={"path": path},
        )
    if body.get("ok") is False:
        err = str(body.get("error") or "unknown")
        if err in {"invalid_auth", "not_authed", "token_revoked", "token_expired"}:
            raise Unauthorized(f"Slack rejected the access token: {err}")
        raise ToolError(
            "TOOL_INVOCATION_FAILED",
            f"Slack API error: {err}",
            status_code=400,
            details={"path": path, "slack_error": err},
        )
    return body


# ---------------------------------------------------------------------------
# Slim helpers — keep payloads agent-friendly.
# ---------------------------------------------------------------------------


def _slim_channel(raw: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": raw.get("id"),
        "name": raw.get("name"),
        "is_private": raw.get("is_private"),
        "is_archived": raw.get("is_archived"),
        "is_member": raw.get("is_member"),
        "topic": (raw.get("topic") or {}).get("value"),
        "purpose": (raw.get("purpose") or {}).get("value"),
        "num_members": raw.get("num_members"),
    }


def _slim_message(raw: dict[str, Any]) -> dict[str, Any]:
    return {
        "ts": raw.get("ts"),
        "user": raw.get("user"),
        "text": raw.get("text"),
        "type": raw.get("type"),
        "subtype": raw.get("subtype"),
        "thread_ts": raw.get("thread_ts"),
        "reply_count": raw.get("reply_count"),
    }


def _slim_user(raw: dict[str, Any]) -> dict[str, Any]:
    profile = raw.get("profile") or {}
    return {
        "id": raw.get("id"),
        "name": raw.get("name"),
        "real_name": raw.get("real_name") or profile.get("real_name"),
        "is_bot": raw.get("is_bot"),
        "is_admin": raw.get("is_admin"),
        "deleted": raw.get("deleted"),
        "tz": raw.get("tz"),
        "email": profile.get("email"),
    }


# ---------------------------------------------------------------------------
# Tool: slack.list_channels
# ---------------------------------------------------------------------------


async def _list_channels(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    types = args.get("types", "public_channel,private_channel")
    if not isinstance(types, str):
        raise ToolError("INVALID_ARGUMENTS", "types must be a string")
    limit = args.get("limit", 100)
    if not isinstance(limit, int) or isinstance(limit, bool) or limit < 1 or limit > 1000:
        raise ToolError("INVALID_ARGUMENTS", "limit must be an integer 1..1000")
    cursor = args.get("cursor")
    if cursor is not None and not isinstance(cursor, str):
        raise ToolError("INVALID_ARGUMENTS", "cursor must be a string")
    exclude_archived = args.get("exclude_archived", True)

    params: dict[str, Any] = {
        "types": types,
        "limit": limit,
        "exclude_archived": "true" if exclude_archived else "false",
    }
    if cursor:
        params["cursor"] = cursor

    body = await _do_get(ctx, "/conversations.list", params=params)
    raw_channels = body.get("channels") or []
    channels = [_slim_channel(c) for c in raw_channels if isinstance(c, dict)]
    next_cursor = (body.get("response_metadata") or {}).get("next_cursor") or None
    return {"channels": channels, "count": len(channels), "next_cursor": next_cursor}


LIST_CHANNELS = Tool(
    tool_id="slack.list_channels",
    name="List Slack channels",
    description="List public + private (where authorized) channels in the workspace.",
    input_schema={
        "type": "object",
        "properties": {
            "types": {
                "type": "string",
                "description": "Comma-separated channel types",
                "default": "public_channel,private_channel",
            },
            "limit": {"type": "integer", "minimum": 1, "maximum": 1000, "default": 100},
            "cursor": {"type": "string"},
            "exclude_archived": {"type": "boolean", "default": True},
        },
        "additionalProperties": False,
    },
    output_schema={"type": "object"},
    idempotent=True,
    side_effects="read",
    cache_ttl_seconds=60,
    handler=_list_channels,
)


# ---------------------------------------------------------------------------
# Tool: slack.post_message
# ---------------------------------------------------------------------------


async def _post_message(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    channel = args.get("channel")
    if not isinstance(channel, str) or not channel.strip():
        raise ToolError("INVALID_ARGUMENTS", "channel is required and must be a string")
    text = args.get("text")
    blocks = args.get("blocks")
    if not isinstance(text, str) and blocks is None:
        raise ToolError(
            "INVALID_ARGUMENTS",
            "either 'text' or 'blocks' must be provided",
        )
    payload: dict[str, Any] = {"channel": channel.strip()}
    if isinstance(text, str):
        payload["text"] = text
    if blocks is not None:
        if not isinstance(blocks, list):
            raise ToolError("INVALID_ARGUMENTS", "blocks must be a list")
        payload["blocks"] = blocks
    if "thread_ts" in args:
        thread_ts = args["thread_ts"]
        if thread_ts is not None and not isinstance(thread_ts, str):
            raise ToolError("INVALID_ARGUMENTS", "thread_ts must be a string")
        if thread_ts:
            payload["thread_ts"] = thread_ts

    body = await _do_post_json(ctx, "/chat.postMessage", body=payload)
    message = body.get("message") or {}
    return {
        "ok": True,
        "channel": body.get("channel"),
        "ts": body.get("ts"),
        "message": _slim_message(message) if isinstance(message, dict) else None,
    }


POST_MESSAGE = Tool(
    tool_id="slack.post_message",
    name="Post a Slack message",
    description="Post a message to a channel (or thread).",
    input_schema={
        "type": "object",
        "required": ["channel"],
        "properties": {
            "channel": {"type": "string", "description": "Channel ID or name"},
            "text": {"type": "string"},
            "blocks": {"type": "array"},
            "thread_ts": {"type": "string"},
        },
        "additionalProperties": False,
    },
    output_schema={"type": "object"},
    idempotent=False,
    side_effects="write",
    cache_ttl_seconds=None,
    handler=_post_message,
)


# ---------------------------------------------------------------------------
# Tool: slack.list_messages
# ---------------------------------------------------------------------------


async def _list_messages(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    channel = args.get("channel")
    if not isinstance(channel, str) or not channel.strip():
        raise ToolError("INVALID_ARGUMENTS", "channel is required and must be a string")
    limit = args.get("limit", 50)
    if not isinstance(limit, int) or isinstance(limit, bool) or limit < 1 or limit > 1000:
        raise ToolError("INVALID_ARGUMENTS", "limit must be an integer 1..1000")
    cursor = args.get("cursor")
    if cursor is not None and not isinstance(cursor, str):
        raise ToolError("INVALID_ARGUMENTS", "cursor must be a string")
    oldest = args.get("oldest")
    latest = args.get("latest")
    if oldest is not None and not isinstance(oldest, str):
        raise ToolError("INVALID_ARGUMENTS", "oldest must be a string (Slack ts)")
    if latest is not None and not isinstance(latest, str):
        raise ToolError("INVALID_ARGUMENTS", "latest must be a string (Slack ts)")

    params: dict[str, Any] = {"channel": channel.strip(), "limit": limit}
    if cursor:
        params["cursor"] = cursor
    if oldest:
        params["oldest"] = oldest
    if latest:
        params["latest"] = latest

    body = await _do_get(ctx, "/conversations.history", params=params)
    raw_messages = body.get("messages") or []
    messages = [_slim_message(m) for m in raw_messages if isinstance(m, dict)]
    next_cursor = (body.get("response_metadata") or {}).get("next_cursor") or None
    return {
        "messages": messages,
        "count": len(messages),
        "has_more": bool(body.get("has_more")),
        "next_cursor": next_cursor,
    }


LIST_MESSAGES = Tool(
    tool_id="slack.list_messages",
    name="List recent Slack messages",
    description="Read recent messages from a channel.",
    input_schema={
        "type": "object",
        "required": ["channel"],
        "properties": {
            "channel": {"type": "string"},
            "limit": {"type": "integer", "minimum": 1, "maximum": 1000, "default": 50},
            "cursor": {"type": "string"},
            "oldest": {"type": "string"},
            "latest": {"type": "string"},
        },
        "additionalProperties": False,
    },
    output_schema={"type": "object"},
    idempotent=True,
    side_effects="read",
    cache_ttl_seconds=15,
    handler=_list_messages,
)


# ---------------------------------------------------------------------------
# Tool: slack.get_user
# ---------------------------------------------------------------------------


async def _get_user(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    user_id = args.get("user")
    if not isinstance(user_id, str) or not user_id.strip():
        raise ToolError("INVALID_ARGUMENTS", "user is required (Slack user ID like 'U123')")
    body = await _do_get(ctx, "/users.info", params={"user": user_id.strip()})
    raw = body.get("user")
    if not isinstance(raw, dict):
        raise ToolError(
            "TOOL_INVOCATION_FAILED",
            "Slack response missing user payload",
            status_code=502,
        )
    return {"user": _slim_user(raw)}


GET_USER = Tool(
    tool_id="slack.get_user",
    name="Get Slack user profile",
    description="Fetch profile info for a single Slack user by ID.",
    input_schema={
        "type": "object",
        "required": ["user"],
        "properties": {"user": {"type": "string", "description": "Slack user ID (e.g. U123)"}},
        "additionalProperties": False,
    },
    output_schema={"type": "object"},
    idempotent=True,
    side_effects="read",
    cache_ttl_seconds=300,
    handler=_get_user,
)


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------


TOOL_LIST: list[Tool] = [
    LIST_CHANNELS,
    POST_MESSAGE,
    LIST_MESSAGES,
    GET_USER,
]


TOOL_REGISTRY: dict[str, Tool] = {tool.tool_id: tool for tool in TOOL_LIST}
