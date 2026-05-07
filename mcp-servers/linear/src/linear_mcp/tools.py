# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 The Plinth Authors
"""Tool implementations for the Linear MCP server.

Each tool is a small wrapper around a Linear GraphQL operation. Every tool
reads the bearer token from the :class:`ToolContext` passed by the route
handler; the route handler in turn lifts it off the inbound ``Authorization``
header.

Linear's API is GraphQL-only: there is one endpoint
(``https://api.linear.app/graphql``) and every tool POSTs a JSON body of
``{"query": <gql>, "variables": {...}}``. Errors are surfaced via the
top-level ``errors`` array (HTTP status is still 200 for app-level failures),
which we translate into a Plinth :class:`ToolError`.
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
    def __init__(self, message: str = "missing or invalid Linear access token") -> None:
        super().__init__("UNAUTHORIZED", message, status_code=401)


# ---------------------------------------------------------------------------
# Context
# ---------------------------------------------------------------------------


@dataclass
class ToolContext:
    """Per-request state injected into tool invocations."""

    access_token: str | None
    http_client: httpx.AsyncClient
    graphql_url: str = "https://api.linear.app/graphql"
    timeout: float = 15.0

    def require_token(self) -> str:
        if not self.access_token:
            raise Unauthorized()
        return self.access_token

    def headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self.require_token()}",
            "Accept": "application/json",
            "Content-Type": "application/json",
            "User-Agent": "plinth-linear-mcp/0.4.0",
        }


# ---------------------------------------------------------------------------
# Validation helpers
# ---------------------------------------------------------------------------


# Linear ids are UUIDs. Tolerate a hyphen-separated lower-case form *or* the
# upper-case identifier ("ENG-123") which the Linear UI displays — many tools
# accept either, but the GraphQL API is happy with either as the ``id``
# argument too. We just guard against pathological inputs.
_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,80}$")


def _require_id(value: Any, *, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ToolError("INVALID_ARGUMENTS", f"{field} is required")
    if not _ID_RE.match(value):
        raise ToolError(
            "INVALID_ARGUMENTS",
            f"{field} must match {_ID_RE.pattern}",
            details={field: value},
        )
    return value


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
            "auth_config": {"provider": "linear"},
        }


# ---------------------------------------------------------------------------
# GraphQL helper
# ---------------------------------------------------------------------------


async def _gql(
    ctx: ToolContext,
    *,
    query: str,
    variables: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """POST a GraphQL document and return the ``data`` payload.

    Raises:
        Unauthorized: If Linear rejects the token (HTTP 401 or
            authentication-related GraphQL error).
        ToolError: On other HTTP failures or non-empty ``errors`` arrays.
    """
    payload: dict[str, Any] = {"query": query}
    if variables is not None:
        payload["variables"] = variables
    try:
        resp = await ctx.http_client.post(
            ctx.graphql_url,
            json=payload,
            headers=ctx.headers(),
            timeout=ctx.timeout,
        )
    except httpx.HTTPError as exc:
        raise ToolError(
            "TOOL_INVOCATION_FAILED",
            f"Linear request failed: {exc}",
            status_code=502,
            details={"graphql_url": ctx.graphql_url},
        ) from exc

    if resp.status_code == 401:
        raise Unauthorized("Linear rejected the access token (401)")
    if resp.status_code >= 400:
        raise ToolError(
            "TOOL_INVOCATION_FAILED",
            f"Linear returned HTTP {resp.status_code}",
            status_code=502,
            details={
                "status_code": resp.status_code,
                "body_preview": resp.text[:300],
            },
        )

    try:
        body = resp.json()
    except ValueError as exc:
        raise ToolError(
            "TOOL_INVOCATION_FAILED",
            f"Linear returned non-JSON: {exc}",
            status_code=502,
        ) from exc

    if not isinstance(body, dict):
        raise ToolError(
            "TOOL_INVOCATION_FAILED",
            "Linear returned a non-object JSON response",
            status_code=502,
        )

    errors = body.get("errors")
    if isinstance(errors, list) and errors:
        first = errors[0] if isinstance(errors[0], dict) else {}
        message = str(first.get("message") or "unknown")
        # Linear distinguishes auth errors with extensions.code = "AUTHENTICATION_ERROR"
        # or message strings like "Authentication required".
        ext_code = ""
        ext = first.get("extensions") if isinstance(first.get("extensions"), dict) else {}
        if isinstance(ext, dict):
            ext_code = str(ext.get("code") or "")
        if ext_code in {"AUTHENTICATION_ERROR", "UNAUTHENTICATED"} or "authentication" in message.lower():
            raise Unauthorized(f"Linear rejected the access token: {message}")
        raise ToolError(
            "TOOL_INVOCATION_FAILED",
            f"Linear GraphQL error: {message}",
            status_code=400,
            details={"errors": errors},
        )

    data = body.get("data")
    if not isinstance(data, dict):
        raise ToolError(
            "TOOL_INVOCATION_FAILED",
            "Linear response missing data payload",
            status_code=502,
        )
    return data


# ---------------------------------------------------------------------------
# Slim helpers — keep payloads agent-friendly.
# ---------------------------------------------------------------------------


def _slim_issue(raw: dict[str, Any]) -> dict[str, Any]:
    state = raw.get("state") or {}
    assignee = raw.get("assignee") or {}
    team = raw.get("team") or {}
    return {
        "id": raw.get("id"),
        "identifier": raw.get("identifier"),
        "title": raw.get("title"),
        "description": raw.get("description"),
        "url": raw.get("url"),
        "priority": raw.get("priority"),
        "state": state.get("name") if isinstance(state, dict) else None,
        "assignee": (
            {"id": assignee.get("id"), "name": assignee.get("name")}
            if isinstance(assignee, dict) and assignee
            else None
        ),
        "team": (
            {"id": team.get("id"), "key": team.get("key"), "name": team.get("name")}
            if isinstance(team, dict) and team
            else None
        ),
        "createdAt": raw.get("createdAt"),
        "updatedAt": raw.get("updatedAt"),
        "completedAt": raw.get("completedAt"),
    }


def _slim_comment(raw: dict[str, Any]) -> dict[str, Any]:
    user = raw.get("user") or {}
    return {
        "id": raw.get("id"),
        "body": raw.get("body"),
        "user": {"id": user.get("id"), "name": user.get("name")} if user else None,
        "createdAt": raw.get("createdAt"),
        "url": raw.get("url"),
    }


# ---------------------------------------------------------------------------
# GraphQL fragments / queries
# ---------------------------------------------------------------------------


_ISSUE_FIELDS = """
id
identifier
title
description
url
priority
createdAt
updatedAt
completedAt
state { id name type }
assignee { id name }
team { id key name }
"""


# ---------------------------------------------------------------------------
# Tool: linear.list_issues
# ---------------------------------------------------------------------------


_LIST_ISSUES_QUERY = (
    "query ListIssues($first: Int!, $filter: IssueFilter, $after: String) {"
    " issues(first: $first, filter: $filter, after: $after) {"
    "  nodes { " + _ISSUE_FIELDS + " }"
    "  pageInfo { hasNextPage endCursor }"
    " }"
    "}"
)


async def _list_issues(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    first = args.get("first", 25)
    if not isinstance(first, int) or isinstance(first, bool) or first < 1 or first > 100:
        raise ToolError("INVALID_ARGUMENTS", "first must be an integer 1..100")
    after = args.get("after")
    if after is not None and not isinstance(after, str):
        raise ToolError("INVALID_ARGUMENTS", "after must be a string")

    # Build a Linear IssueFilter from a small flat schema.
    filt: dict[str, Any] = {}
    team_id = args.get("team_id")
    if team_id is not None:
        if not isinstance(team_id, str) or not team_id:
            raise ToolError("INVALID_ARGUMENTS", "team_id must be a non-empty string")
        filt["team"] = {"id": {"eq": team_id}}
    assignee_id = args.get("assignee_id")
    if assignee_id is not None:
        if not isinstance(assignee_id, str) or not assignee_id:
            raise ToolError("INVALID_ARGUMENTS", "assignee_id must be a non-empty string")
        filt["assignee"] = {"id": {"eq": assignee_id}}
    state_name = args.get("state")
    if state_name is not None:
        if not isinstance(state_name, str) or not state_name:
            raise ToolError("INVALID_ARGUMENTS", "state must be a non-empty string")
        filt["state"] = {"name": {"eq": state_name}}

    variables: dict[str, Any] = {"first": first}
    if filt:
        variables["filter"] = filt
    if after:
        variables["after"] = after

    data = await _gql(ctx, query=_LIST_ISSUES_QUERY, variables=variables)
    issues_payload = data.get("issues") or {}
    nodes = issues_payload.get("nodes") or []
    issues = [_slim_issue(n) for n in nodes if isinstance(n, dict)]
    page_info = issues_payload.get("pageInfo") or {}
    return {
        "issues": issues,
        "count": len(issues),
        "has_next_page": bool(page_info.get("hasNextPage")),
        "next_cursor": page_info.get("endCursor"),
    }


LIST_ISSUES = Tool(
    tool_id="linear.list_issues",
    name="List Linear issues",
    description="List issues, optionally filtered by team / assignee / state.",
    input_schema={
        "type": "object",
        "properties": {
            "first": {"type": "integer", "minimum": 1, "maximum": 100, "default": 25},
            "after": {"type": "string", "description": "Pagination cursor"},
            "team_id": {"type": "string"},
            "assignee_id": {"type": "string"},
            "state": {"type": "string", "description": "Workflow state name (e.g. 'In Progress')"},
        },
        "additionalProperties": False,
    },
    output_schema={"type": "object"},
    idempotent=True,
    side_effects="read",
    cache_ttl_seconds=30,
    handler=_list_issues,
)


# ---------------------------------------------------------------------------
# Tool: linear.get_issue
# ---------------------------------------------------------------------------


_GET_ISSUE_QUERY = (
    "query GetIssue($id: String!) {"
    " issue(id: $id) {" + _ISSUE_FIELDS
    + " comments(first: 50) {"
      "  nodes { id body createdAt url user { id name } }"
      " }"
      "}"
      "}"
)


async def _get_issue(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    issue_id = _require_id(args.get("id"), field="id")
    data = await _gql(ctx, query=_GET_ISSUE_QUERY, variables={"id": issue_id})
    issue = data.get("issue")
    if not isinstance(issue, dict):
        raise ToolError(
            "TOOL_INVOCATION_FAILED",
            "Linear response missing issue payload",
            status_code=404,
            details={"id": issue_id},
        )
    comments_payload = issue.get("comments") or {}
    comment_nodes = comments_payload.get("nodes") or []
    comments = [_slim_comment(c) for c in comment_nodes if isinstance(c, dict)]
    return {"issue": _slim_issue(issue), "comments": comments}


GET_ISSUE = Tool(
    tool_id="linear.get_issue",
    name="Get a Linear issue",
    description="Fetch a single issue (with comments) by id.",
    input_schema={
        "type": "object",
        "required": ["id"],
        "properties": {"id": {"type": "string", "description": "Linear issue ID (UUID)"}},
        "additionalProperties": False,
    },
    output_schema={"type": "object"},
    idempotent=True,
    side_effects="read",
    cache_ttl_seconds=30,
    handler=_get_issue,
)


# ---------------------------------------------------------------------------
# Tool: linear.create_issue
# ---------------------------------------------------------------------------


_CREATE_ISSUE_MUTATION = (
    "mutation CreateIssue($input: IssueCreateInput!) {"
    " issueCreate(input: $input) {"
    "  success"
    "  issue { " + _ISSUE_FIELDS + " }"
    " }"
    "}"
)


async def _create_issue(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    team_id = _require_id(args.get("team_id"), field="team_id")
    title = args.get("title")
    if not isinstance(title, str) or not title.strip():
        raise ToolError("INVALID_ARGUMENTS", "title is required")
    payload: dict[str, Any] = {"teamId": team_id, "title": title.strip()}
    if isinstance(args.get("description"), str):
        payload["description"] = args["description"]
    if isinstance(args.get("assignee_id"), str) and args["assignee_id"]:
        payload["assigneeId"] = args["assignee_id"]
    if "priority" in args:
        priority = args["priority"]
        if not isinstance(priority, int) or isinstance(priority, bool) or priority < 0 or priority > 4:
            raise ToolError("INVALID_ARGUMENTS", "priority must be an integer 0..4")
        payload["priority"] = priority
    if isinstance(args.get("state_id"), str) and args["state_id"]:
        payload["stateId"] = args["state_id"]
    if isinstance(args.get("label_ids"), list):
        payload["labelIds"] = [str(label) for label in args["label_ids"]]

    data = await _gql(
        ctx, query=_CREATE_ISSUE_MUTATION, variables={"input": payload}
    )
    result = data.get("issueCreate") or {}
    if not result.get("success"):
        raise ToolError(
            "TOOL_INVOCATION_FAILED",
            "Linear issueCreate did not report success",
            status_code=502,
            details={"result": result},
        )
    issue = result.get("issue")
    if not isinstance(issue, dict):
        raise ToolError(
            "TOOL_INVOCATION_FAILED",
            "Linear issueCreate returned no issue",
            status_code=502,
        )
    return {"issue": _slim_issue(issue)}


CREATE_ISSUE = Tool(
    tool_id="linear.create_issue",
    name="Create a Linear issue",
    description="Open a new issue in a Linear team.",
    input_schema={
        "type": "object",
        "required": ["team_id", "title"],
        "properties": {
            "team_id": {"type": "string", "description": "Team UUID the issue belongs to"},
            "title": {"type": "string"},
            "description": {"type": "string"},
            "assignee_id": {"type": "string"},
            "priority": {"type": "integer", "minimum": 0, "maximum": 4},
            "state_id": {"type": "string"},
            "label_ids": {"type": "array", "items": {"type": "string"}},
        },
        "additionalProperties": False,
    },
    output_schema={"type": "object"},
    idempotent=False,
    side_effects="write",
    cache_ttl_seconds=None,
    handler=_create_issue,
)


# ---------------------------------------------------------------------------
# Tool: linear.update_issue
# ---------------------------------------------------------------------------


_UPDATE_ISSUE_MUTATION = (
    "mutation UpdateIssue($id: String!, $input: IssueUpdateInput!) {"
    " issueUpdate(id: $id, input: $input) {"
    "  success"
    "  issue { " + _ISSUE_FIELDS + " }"
    " }"
    "}"
)


async def _update_issue(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    issue_id = _require_id(args.get("id"), field="id")
    update: dict[str, Any] = {}
    if isinstance(args.get("title"), str):
        update["title"] = args["title"]
    if "description" in args and (isinstance(args["description"], str) or args["description"] is None):
        update["description"] = args["description"]
    if isinstance(args.get("state_id"), str) and args["state_id"]:
        update["stateId"] = args["state_id"]
    if isinstance(args.get("assignee_id"), str) and args["assignee_id"]:
        update["assigneeId"] = args["assignee_id"]
    if "priority" in args:
        priority = args["priority"]
        if not isinstance(priority, int) or isinstance(priority, bool) or priority < 0 or priority > 4:
            raise ToolError("INVALID_ARGUMENTS", "priority must be an integer 0..4")
        update["priority"] = priority
    if isinstance(args.get("label_ids"), list):
        update["labelIds"] = [str(label) for label in args["label_ids"]]

    if not update:
        raise ToolError(
            "INVALID_ARGUMENTS",
            "at least one updatable field must be provided",
        )

    data = await _gql(
        ctx,
        query=_UPDATE_ISSUE_MUTATION,
        variables={"id": issue_id, "input": update},
    )
    result = data.get("issueUpdate") or {}
    if not result.get("success"):
        raise ToolError(
            "TOOL_INVOCATION_FAILED",
            "Linear issueUpdate did not report success",
            status_code=502,
            details={"result": result},
        )
    issue = result.get("issue")
    if not isinstance(issue, dict):
        raise ToolError(
            "TOOL_INVOCATION_FAILED",
            "Linear issueUpdate returned no issue",
            status_code=502,
        )
    return {"issue": _slim_issue(issue)}


UPDATE_ISSUE = Tool(
    tool_id="linear.update_issue",
    name="Update a Linear issue",
    description="Edit an issue's title / description / state / assignee / labels / priority.",
    input_schema={
        "type": "object",
        "required": ["id"],
        "properties": {
            "id": {"type": "string"},
            "title": {"type": "string"},
            "description": {"type": ["string", "null"]},
            "state_id": {"type": "string"},
            "assignee_id": {"type": "string"},
            "priority": {"type": "integer", "minimum": 0, "maximum": 4},
            "label_ids": {"type": "array", "items": {"type": "string"}},
        },
        "additionalProperties": False,
    },
    output_schema={"type": "object"},
    idempotent=False,
    side_effects="write",
    cache_ttl_seconds=None,
    handler=_update_issue,
)


# ---------------------------------------------------------------------------
# Tool: linear.comment_on_issue
# ---------------------------------------------------------------------------


_COMMENT_MUTATION = (
    "mutation CommentCreate($input: CommentCreateInput!) {"
    " commentCreate(input: $input) {"
    "  success"
    "  comment { id body createdAt url user { id name } }"
    " }"
    "}"
)


async def _comment_on_issue(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    issue_id = _require_id(args.get("issue_id"), field="issue_id")
    body_text = args.get("body")
    if not isinstance(body_text, str) or not body_text.strip():
        raise ToolError("INVALID_ARGUMENTS", "body must be a non-empty string")
    payload = {"issueId": issue_id, "body": body_text}
    data = await _gql(ctx, query=_COMMENT_MUTATION, variables={"input": payload})
    result = data.get("commentCreate") or {}
    if not result.get("success"):
        raise ToolError(
            "TOOL_INVOCATION_FAILED",
            "Linear commentCreate did not report success",
            status_code=502,
            details={"result": result},
        )
    comment = result.get("comment")
    if not isinstance(comment, dict):
        raise ToolError(
            "TOOL_INVOCATION_FAILED",
            "Linear commentCreate returned no comment",
            status_code=502,
        )
    return {"comment": _slim_comment(comment)}


COMMENT_ON_ISSUE = Tool(
    tool_id="linear.comment_on_issue",
    name="Comment on a Linear issue",
    description="Post a comment on an existing issue.",
    input_schema={
        "type": "object",
        "required": ["issue_id", "body"],
        "properties": {
            "issue_id": {"type": "string"},
            "body": {"type": "string"},
        },
        "additionalProperties": False,
    },
    output_schema={"type": "object"},
    idempotent=False,
    side_effects="write",
    cache_ttl_seconds=None,
    handler=_comment_on_issue,
)


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------


TOOL_LIST: list[Tool] = [
    LIST_ISSUES,
    GET_ISSUE,
    CREATE_ISSUE,
    UPDATE_ISSUE,
    COMMENT_ON_ISSUE,
]


TOOL_REGISTRY: dict[str, Tool] = {tool.tool_id: tool for tool in TOOL_LIST}
