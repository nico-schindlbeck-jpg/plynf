# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 The Plinth Authors
"""Tool implementations for the Atlassian MCP server.

Atlassian's OAuth-2.0 (3LO) flow ships a per-workspace ``cloudid`` that the
caller must inject into every REST URL — there is no per-token "default"
workspace. The Plinth gateway captures the cloudid at OAuth callback time
(via ``/oauth/token/accessible-resources``) and re-injects it on every
proxied invoke as ``X-Plinth-OAuth-Cloudid``. This module reads it off the
inbound request and shapes the per-call URLs accordingly:

* Jira REST v3 — ``/ex/jira/{cloudid}/rest/api/3/...``
* Confluence v2 — ``/ex/confluence/{cloudid}/wiki/api/v2/...``

The bearer token from ``Authorization`` flows through verbatim. Tools
return slim, agent-friendly dicts (id, key, summary, ...) rather than the
verbose Atlassian raw payloads.
"""

from __future__ import annotations

import re
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
    def __init__(self, message: str = "missing or invalid Atlassian access token") -> None:
        super().__init__("UNAUTHORIZED", message, status_code=401)


class CloudidMissing(ToolError):
    def __init__(self) -> None:
        super().__init__(
            "ATLASSIAN_CLOUDID_MISSING",
            "X-Plinth-OAuth-Cloudid header is required (gateway must inject it)",
            status_code=400,
        )


# ---------------------------------------------------------------------------
# Context
# ---------------------------------------------------------------------------


@dataclass
class ToolContext:
    """Per-request state injected into tool invocations."""

    access_token: str | None
    cloudid: str | None
    http_client: httpx.AsyncClient
    api_base_url: str = "https://api.atlassian.com"
    timeout: float = 15.0

    def require_token(self) -> str:
        if not self.access_token:
            raise Unauthorized()
        return self.access_token

    def require_cloudid(self) -> str:
        if not self.cloudid:
            raise CloudidMissing()
        return self.cloudid

    def headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self.require_token()}",
            "Accept": "application/json",
            "Content-Type": "application/json",
            "User-Agent": "plinth-atlassian-mcp/1.5.0",
        }

    def jira_url(self, path: str) -> str:
        if not path.startswith("/"):
            path = "/" + path
        return f"{self.api_base_url.rstrip('/')}/ex/jira/{self.require_cloudid()}{path}"

    def confluence_url(self, path: str) -> str:
        if not path.startswith("/"):
            path = "/" + path
        return f"{self.api_base_url.rstrip('/')}/ex/confluence/{self.require_cloudid()}{path}"


# ---------------------------------------------------------------------------
# Validation helpers
# ---------------------------------------------------------------------------


# Jira issue keys are upper-case project key + dash + integer (e.g. PLI-42).
_JIRA_KEY_RE = re.compile(r"^[A-Z][A-Z0-9_]+-\d+$")


def parse_issue_key(value: Any, *, name: str = "issue_key") -> str:
    """Validate that ``value`` is a Jira issue key (e.g. ``PLI-42``)."""
    if not isinstance(value, str) or not value:
        raise ToolError("INVALID_ARGUMENTS", f"{name} is required and must be a string")
    if "/" in value or "\\" in value or ".." in value:
        raise ToolError(
            "INVALID_ARGUMENTS",
            f"{name} must be a Jira issue key (no path traversal)",
            details={name: value},
        )
    if not _JIRA_KEY_RE.match(value):
        raise ToolError(
            "INVALID_ARGUMENTS",
            f"{name} must look like 'ABC-123'",
            details={name: value},
        )
    return value


def parse_page_id(value: Any, *, name: str = "page_id") -> str:
    """Confluence page IDs are unsigned integers (returned as strings)."""
    if not isinstance(value, (str, int)) or isinstance(value, bool):
        raise ToolError("INVALID_ARGUMENTS", f"{name} is required")
    s = str(value).strip()
    if not s or "/" in s or ".." in s or not s.isdigit():
        raise ToolError(
            "INVALID_ARGUMENTS",
            f"{name} must be a numeric Confluence page id",
            details={name: value},
        )
    return s


# ---------------------------------------------------------------------------
# Tool record
# ---------------------------------------------------------------------------


ToolHandler = Callable[[dict[str, Any], ToolContext], Awaitable[dict[str, Any]]]


@dataclass(frozen=True)
class Tool:
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
            "auth_config": {"provider": "atlassian"},
        }


# ---------------------------------------------------------------------------
# HTTP helpers
# ---------------------------------------------------------------------------


async def _do_get(ctx: ToolContext, url: str, *, params: dict[str, Any] | None = None) -> Any:
    try:
        resp = await ctx.http_client.get(
            url, params=params, headers=ctx.headers(), timeout=ctx.timeout
        )
    except httpx.HTTPError as exc:
        raise ToolError(
            "TOOL_INVOCATION_FAILED",
            f"Atlassian request failed: {exc}",
            status_code=502,
            details={"url": url},
        ) from exc
    return _decode_response(resp, url=url)


async def _do_post(ctx: ToolContext, url: str, *, body: dict[str, Any]) -> Any:
    try:
        resp = await ctx.http_client.post(
            url, json=body, headers=ctx.headers(), timeout=ctx.timeout
        )
    except httpx.HTTPError as exc:
        raise ToolError(
            "TOOL_INVOCATION_FAILED",
            f"Atlassian request failed: {exc}",
            status_code=502,
            details={"url": url},
        ) from exc
    return _decode_response(resp, url=url)


async def _do_put(ctx: ToolContext, url: str, *, body: dict[str, Any]) -> Any:
    try:
        resp = await ctx.http_client.put(
            url, json=body, headers=ctx.headers(), timeout=ctx.timeout
        )
    except httpx.HTTPError as exc:
        raise ToolError(
            "TOOL_INVOCATION_FAILED",
            f"Atlassian request failed: {exc}",
            status_code=502,
            details={"url": url},
        ) from exc
    return _decode_response(resp, url=url)


def _decode_response(resp: httpx.Response, *, url: str) -> Any:
    if resp.status_code == 401:
        raise Unauthorized("Atlassian rejected the access token (401)")
    if resp.status_code == 403:
        raise ToolError(
            "TOOL_INVOCATION_FAILED",
            "Atlassian permission denied (403)",
            status_code=403,
            details={"url": url, "status_code": 403},
        )
    if resp.status_code == 404:
        raise ToolError(
            "TOOL_INVOCATION_FAILED",
            "Atlassian resource not found",
            status_code=404,
            details={"url": url, "status_code": 404},
        )
    # 204 No Content is permitted and decoded as ``None``.
    if resp.status_code == 204:
        return None
    if resp.status_code >= 400:
        body_preview = resp.text[:300]
        raise ToolError(
            "TOOL_INVOCATION_FAILED",
            f"Atlassian returned HTTP {resp.status_code}",
            status_code=502,
            details={"url": url, "status_code": resp.status_code, "body_preview": body_preview},
        )
    if not resp.text:
        return None
    try:
        return resp.json()
    except ValueError as exc:
        raise ToolError(
            "TOOL_INVOCATION_FAILED",
            f"Atlassian returned non-JSON: {exc}",
            status_code=502,
            details={"url": url},
        ) from exc


# ---------------------------------------------------------------------------
# Slim helpers
# ---------------------------------------------------------------------------


def _slim_issue(raw: dict[str, Any]) -> dict[str, Any]:
    fields = raw.get("fields") or {}
    status = (fields.get("status") or {}).get("name") if isinstance(fields, dict) else None
    issue_type = (
        (fields.get("issuetype") or {}).get("name") if isinstance(fields, dict) else None
    )
    assignee = fields.get("assignee") if isinstance(fields, dict) else None
    assignee_name = (
        assignee.get("displayName") if isinstance(assignee, dict) else None
    )
    return {
        "id": raw.get("id"),
        "key": raw.get("key"),
        "summary": fields.get("summary") if isinstance(fields, dict) else None,
        "status": status,
        "issue_type": issue_type,
        "assignee": assignee_name,
        "url": raw.get("self"),
    }


def _slim_page(raw: dict[str, Any]) -> dict[str, Any]:
    title = raw.get("title")
    body = raw.get("body") or {}
    storage = body.get("storage") if isinstance(body, dict) else None
    storage_value = (
        storage.get("value") if isinstance(storage, dict) else None
    )
    space = raw.get("spaceId") or raw.get("space_id")
    return {
        "id": str(raw.get("id")) if raw.get("id") is not None else None,
        "title": title,
        "space_id": str(space) if space is not None else None,
        "version": ((raw.get("version") or {}).get("number") if isinstance(raw.get("version"), dict) else None),
        "body": storage_value,
        "url": (raw.get("_links") or {}).get("webui") if isinstance(raw.get("_links"), dict) else None,
    }


# ---------------------------------------------------------------------------
# Tool: atlassian.jira_search
# ---------------------------------------------------------------------------


async def _jira_search(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    jql = args.get("jql")
    if jql is not None and not isinstance(jql, str):
        raise ToolError("INVALID_ARGUMENTS", "jql must be a string")
    max_results = args.get("max_results", 25)
    if (
        not isinstance(max_results, int)
        or isinstance(max_results, bool)
        or max_results < 1
        or max_results > 100
    ):
        raise ToolError("INVALID_ARGUMENTS", "max_results must be an integer 1..100")

    body: dict[str, Any] = {"jql": jql or "", "maxResults": max_results}
    if "fields" in args:
        fields = args.get("fields")
        if not isinstance(fields, list) or not all(isinstance(f, str) for f in fields):
            raise ToolError("INVALID_ARGUMENTS", "fields must be a list of strings")
        body["fields"] = fields
    raw = await _do_post(ctx, ctx.jira_url("/rest/api/3/search"), body=body)
    if not isinstance(raw, dict):
        raise ToolError("TOOL_INVOCATION_FAILED", "Atlassian returned non-object", status_code=502)
    items = raw.get("issues") or []
    issues = [_slim_issue(item) for item in items if isinstance(item, dict)]
    return {
        "issues": issues,
        "total": int(raw.get("total") or len(issues)),
        "start_at": int(raw.get("startAt") or 0),
        "max_results": int(raw.get("maxResults") or max_results),
    }


JIRA_SEARCH = Tool(
    tool_id="atlassian.jira_search",
    name="Search Jira issues with JQL",
    description="Run a JQL search and return slim issue rows.",
    input_schema={
        "type": "object",
        "properties": {
            "jql": {"type": "string"},
            "max_results": {"type": "integer", "minimum": 1, "maximum": 100, "default": 25},
            "fields": {"type": "array", "items": {"type": "string"}},
        },
        "additionalProperties": False,
    },
    output_schema={"type": "object"},
    idempotent=True,
    side_effects="read",
    cache_ttl_seconds=30,
    handler=_jira_search,
)


# ---------------------------------------------------------------------------
# Tool: atlassian.jira_get_issue
# ---------------------------------------------------------------------------


async def _jira_get_issue(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    issue_key = parse_issue_key(args.get("issue_key"), name="issue_key")
    issue = await _do_get(ctx, ctx.jira_url(f"/rest/api/3/issue/{issue_key}"))
    if not isinstance(issue, dict):
        raise ToolError("TOOL_INVOCATION_FAILED", "Atlassian returned non-object", status_code=502)

    # Fetch comments — separate endpoint.
    comments_raw = await _do_get(
        ctx, ctx.jira_url(f"/rest/api/3/issue/{issue_key}/comment")
    )
    comments: list[dict[str, Any]] = []
    if isinstance(comments_raw, dict):
        items = comments_raw.get("comments") or []
        if isinstance(items, list):
            for item in items:
                if not isinstance(item, dict):
                    continue
                author = item.get("author") if isinstance(item.get("author"), dict) else {}
                comments.append(
                    {
                        "id": item.get("id"),
                        "author": author.get("displayName") if isinstance(author, dict) else None,
                        "body": item.get("body"),
                        "created": item.get("created"),
                        "updated": item.get("updated"),
                    }
                )
    slim = _slim_issue(issue)
    slim["fields"] = issue.get("fields") or {}
    slim["comments"] = comments
    return slim


JIRA_GET_ISSUE = Tool(
    tool_id="atlassian.jira_get_issue",
    name="Get a Jira issue (with comments)",
    description="Fetch a single Jira issue + its comments.",
    input_schema={
        "type": "object",
        "required": ["issue_key"],
        "properties": {
            "issue_key": {"type": "string", "description": "e.g. 'PLI-42'"},
        },
        "additionalProperties": False,
    },
    output_schema={"type": "object"},
    idempotent=True,
    side_effects="read",
    cache_ttl_seconds=30,
    handler=_jira_get_issue,
)


# ---------------------------------------------------------------------------
# Tool: atlassian.jira_create_issue
# ---------------------------------------------------------------------------


async def _jira_create_issue(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    project_key = args.get("project_key")
    if not isinstance(project_key, str) or not project_key.strip():
        raise ToolError("INVALID_ARGUMENTS", "project_key is required")
    summary = args.get("summary")
    if not isinstance(summary, str) or not summary.strip():
        raise ToolError("INVALID_ARGUMENTS", "summary is required")
    issue_type = args.get("issue_type", "Task")
    if not isinstance(issue_type, str) or not issue_type.strip():
        raise ToolError("INVALID_ARGUMENTS", "issue_type must be a non-empty string")

    fields: dict[str, Any] = {
        "project": {"key": project_key.strip()},
        "summary": summary.strip(),
        "issuetype": {"name": issue_type.strip()},
    }
    description = args.get("description")
    if isinstance(description, str) and description.strip():
        # Atlassian Document Format (ADF) — minimal "doc → paragraph → text".
        fields["description"] = {
            "type": "doc",
            "version": 1,
            "content": [
                {
                    "type": "paragraph",
                    "content": [{"type": "text", "text": description}],
                }
            ],
        }
    if "assignee_account_id" in args:
        aid = args.get("assignee_account_id")
        if not isinstance(aid, str) or not aid:
            raise ToolError("INVALID_ARGUMENTS", "assignee_account_id must be a string")
        fields["assignee"] = {"accountId": aid}
    if "extra_fields" in args:
        extra = args.get("extra_fields")
        if not isinstance(extra, dict):
            raise ToolError("INVALID_ARGUMENTS", "extra_fields must be an object")
        # Caller-supplied fields win on key collisions (assignee, etc.).
        fields.update(extra)

    raw = await _do_post(ctx, ctx.jira_url("/rest/api/3/issue"), body={"fields": fields})
    if not isinstance(raw, dict):
        raise ToolError("TOOL_INVOCATION_FAILED", "Atlassian returned non-object", status_code=502)
    return {"id": raw.get("id"), "key": raw.get("key"), "url": raw.get("self")}


JIRA_CREATE_ISSUE = Tool(
    tool_id="atlassian.jira_create_issue",
    name="Create a Jira issue",
    description="Create a new Jira issue (project_key + summary required).",
    input_schema={
        "type": "object",
        "required": ["project_key", "summary"],
        "properties": {
            "project_key": {"type": "string"},
            "summary": {"type": "string"},
            "issue_type": {"type": "string", "default": "Task"},
            "description": {"type": "string"},
            "assignee_account_id": {"type": "string"},
            "extra_fields": {"type": "object"},
        },
        "additionalProperties": False,
    },
    output_schema={"type": "object"},
    idempotent=False,
    side_effects="write",
    cache_ttl_seconds=None,
    handler=_jira_create_issue,
)


# ---------------------------------------------------------------------------
# Tool: atlassian.jira_update_issue
# ---------------------------------------------------------------------------


async def _jira_update_issue(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    issue_key = parse_issue_key(args.get("issue_key"), name="issue_key")
    fields = args.get("fields")
    if not isinstance(fields, dict) or not fields:
        raise ToolError("INVALID_ARGUMENTS", "fields must be a non-empty object")

    await _do_put(ctx, ctx.jira_url(f"/rest/api/3/issue/{issue_key}"), body={"fields": fields})
    return {"key": issue_key, "updated": True}


JIRA_UPDATE_ISSUE = Tool(
    tool_id="atlassian.jira_update_issue",
    name="Update a Jira issue",
    description="Edit fields on an existing Jira issue.",
    input_schema={
        "type": "object",
        "required": ["issue_key", "fields"],
        "properties": {
            "issue_key": {"type": "string"},
            "fields": {"type": "object"},
        },
        "additionalProperties": False,
    },
    output_schema={"type": "object"},
    idempotent=False,
    side_effects="write",
    cache_ttl_seconds=None,
    handler=_jira_update_issue,
)


# ---------------------------------------------------------------------------
# Tool: atlassian.jira_comment
# ---------------------------------------------------------------------------


async def _jira_comment(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    issue_key = parse_issue_key(args.get("issue_key"), name="issue_key")
    body_text = args.get("body")
    if not isinstance(body_text, str) or not body_text.strip():
        raise ToolError("INVALID_ARGUMENTS", "body is required")
    payload = {
        "body": {
            "type": "doc",
            "version": 1,
            "content": [
                {
                    "type": "paragraph",
                    "content": [{"type": "text", "text": body_text}],
                }
            ],
        }
    }
    raw = await _do_post(
        ctx, ctx.jira_url(f"/rest/api/3/issue/{issue_key}/comment"), body=payload
    )
    if not isinstance(raw, dict):
        raise ToolError("TOOL_INVOCATION_FAILED", "Atlassian returned non-object", status_code=502)
    return {"id": raw.get("id"), "issue_key": issue_key, "created": raw.get("created")}


JIRA_COMMENT = Tool(
    tool_id="atlassian.jira_comment",
    name="Add a Jira comment",
    description="Add a comment to a Jira issue.",
    input_schema={
        "type": "object",
        "required": ["issue_key", "body"],
        "properties": {
            "issue_key": {"type": "string"},
            "body": {"type": "string"},
        },
        "additionalProperties": False,
    },
    output_schema={"type": "object"},
    idempotent=False,
    side_effects="write",
    cache_ttl_seconds=None,
    handler=_jira_comment,
)


# ---------------------------------------------------------------------------
# Tool: atlassian.confluence_search
# ---------------------------------------------------------------------------


async def _confluence_search(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    cql = args.get("cql")
    if not isinstance(cql, str) or not cql.strip():
        raise ToolError("INVALID_ARGUMENTS", "cql is required and must be a non-empty string")
    limit = args.get("limit", 25)
    if (
        not isinstance(limit, int)
        or isinstance(limit, bool)
        or limit < 1
        or limit > 100
    ):
        raise ToolError("INVALID_ARGUMENTS", "limit must be an integer 1..100")

    # Confluence v1 search remains the canonical CQL endpoint.
    raw = await _do_get(
        ctx,
        ctx.confluence_url("/wiki/rest/api/search"),
        params={"cql": cql, "limit": limit},
    )
    if not isinstance(raw, dict):
        raise ToolError("TOOL_INVOCATION_FAILED", "Atlassian returned non-object", status_code=502)
    items = raw.get("results") or []
    pages: list[dict[str, Any]] = []
    if isinstance(items, list):
        for item in items:
            if not isinstance(item, dict):
                continue
            content = item.get("content") if isinstance(item.get("content"), dict) else item
            pages.append(
                {
                    "id": str(content.get("id")) if content.get("id") is not None else None,
                    "title": content.get("title") or item.get("title"),
                    "type": content.get("type") or item.get("type"),
                    "url": (item.get("_links") or {}).get("webui")
                    if isinstance(item.get("_links"), dict)
                    else None,
                    "excerpt": item.get("excerpt"),
                }
            )
    return {"results": pages, "count": len(pages)}


CONFLUENCE_SEARCH = Tool(
    tool_id="atlassian.confluence_search",
    name="Search Confluence with CQL",
    description="Search Confluence pages using CQL; returns slim hits.",
    input_schema={
        "type": "object",
        "required": ["cql"],
        "properties": {
            "cql": {"type": "string"},
            "limit": {"type": "integer", "minimum": 1, "maximum": 100, "default": 25},
        },
        "additionalProperties": False,
    },
    output_schema={"type": "object"},
    idempotent=True,
    side_effects="read",
    cache_ttl_seconds=30,
    handler=_confluence_search,
)


# ---------------------------------------------------------------------------
# Tool: atlassian.confluence_get_page
# ---------------------------------------------------------------------------


async def _confluence_get_page(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    page_id = parse_page_id(args.get("page_id"), name="page_id")
    raw = await _do_get(
        ctx,
        ctx.confluence_url(f"/wiki/api/v2/pages/{page_id}"),
        params={"body-format": "storage"},
    )
    if not isinstance(raw, dict):
        raise ToolError("TOOL_INVOCATION_FAILED", "Atlassian returned non-object", status_code=502)
    return _slim_page(raw)


CONFLUENCE_GET_PAGE = Tool(
    tool_id="atlassian.confluence_get_page",
    name="Get a Confluence page",
    description="Fetch a Confluence page (with storage-format body).",
    input_schema={
        "type": "object",
        "required": ["page_id"],
        "properties": {
            "page_id": {"type": "string"},
        },
        "additionalProperties": False,
    },
    output_schema={"type": "object"},
    idempotent=True,
    side_effects="read",
    cache_ttl_seconds=30,
    handler=_confluence_get_page,
)


# ---------------------------------------------------------------------------
# Tool: atlassian.confluence_create_page
# ---------------------------------------------------------------------------


async def _confluence_create_page(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    space_id = args.get("space_id")
    if not isinstance(space_id, (str, int)) or isinstance(space_id, bool):
        raise ToolError("INVALID_ARGUMENTS", "space_id is required")
    space_id_str = str(space_id).strip()
    if not space_id_str:
        raise ToolError("INVALID_ARGUMENTS", "space_id is required")
    title = args.get("title")
    if not isinstance(title, str) or not title.strip():
        raise ToolError("INVALID_ARGUMENTS", "title is required")
    content = args.get("content")
    if not isinstance(content, str):
        raise ToolError("INVALID_ARGUMENTS", "content is required (storage-format string)")

    body = {
        "spaceId": space_id_str,
        "status": args.get("status", "current"),
        "title": title.strip(),
        "body": {
            "representation": "storage",
            "value": content,
        },
    }
    if "parent_id" in args:
        parent_id = args.get("parent_id")
        body["parentId"] = parse_page_id(parent_id, name="parent_id")
    raw = await _do_post(ctx, ctx.confluence_url("/wiki/api/v2/pages"), body=body)
    if not isinstance(raw, dict):
        raise ToolError("TOOL_INVOCATION_FAILED", "Atlassian returned non-object", status_code=502)
    return _slim_page(raw)


CONFLUENCE_CREATE_PAGE = Tool(
    tool_id="atlassian.confluence_create_page",
    name="Create a Confluence page",
    description="Create a new Confluence page in a space.",
    input_schema={
        "type": "object",
        "required": ["space_id", "title", "content"],
        "properties": {
            "space_id": {"type": "string"},
            "title": {"type": "string"},
            "content": {"type": "string", "description": "Confluence storage format"},
            "parent_id": {"type": "string"},
            "status": {"type": "string", "default": "current"},
        },
        "additionalProperties": False,
    },
    output_schema={"type": "object"},
    idempotent=False,
    side_effects="write",
    cache_ttl_seconds=None,
    handler=_confluence_create_page,
)


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------


TOOL_LIST: list[Tool] = [
    JIRA_SEARCH,
    JIRA_GET_ISSUE,
    JIRA_CREATE_ISSUE,
    JIRA_UPDATE_ISSUE,
    JIRA_COMMENT,
    CONFLUENCE_SEARCH,
    CONFLUENCE_GET_PAGE,
    CONFLUENCE_CREATE_PAGE,
]


TOOL_REGISTRY: dict[str, Tool] = {tool.tool_id: tool for tool in TOOL_LIST}
