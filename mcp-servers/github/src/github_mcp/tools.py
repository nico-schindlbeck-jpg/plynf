# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 The Plinth Authors
"""Tool implementations for the GitHub MCP server.

Each tool is a small wrapper around a GitHub REST endpoint. Every tool reads
the bearer token from the :class:`ToolContext` passed by the route handler;
the route handler in turn lifts it off the inbound ``Authorization`` header.

Path validation: every tool that accepts a ``repo`` argument enforces the
``owner/name`` shape (no absolute paths, no traversal segments). This is
defence-in-depth — ``api.github.com`` would reject malformed paths anyway,
but we don't want to forward attacker-controlled strings into a URL.
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
    def __init__(self, message: str = "missing or invalid GitHub access token") -> None:
        super().__init__("UNAUTHORIZED", message, status_code=401)


# ---------------------------------------------------------------------------
# Context
# ---------------------------------------------------------------------------


@dataclass
class ToolContext:
    """Per-request state injected into tool invocations.

    Attributes:
        access_token: The GitHub bearer token. Validated non-empty at the route
            layer; tools that need it should use :meth:`require_token`.
        http_client: Shared httpx async client.
        api_base_url: Base of the GitHub REST API (overridable for tests).
        api_version: Value sent as ``X-GitHub-Api-Version``.
        timeout: Per-request timeout in seconds.
    """

    access_token: str | None
    http_client: httpx.AsyncClient
    api_base_url: str = "https://api.github.com"
    api_version: str = "2022-11-28"
    timeout: float = 15.0

    def require_token(self) -> str:
        if not self.access_token:
            raise Unauthorized()
        return self.access_token

    def headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self.require_token()}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": self.api_version,
            "User-Agent": "plinth-github-mcp/0.3.0",
        }

    def url(self, path: str) -> str:
        if not path.startswith("/"):
            path = "/" + path
        return f"{self.api_base_url.rstrip('/')}{path}"


# ---------------------------------------------------------------------------
# Repo validation
# ---------------------------------------------------------------------------


# GitHub usernames + repo names (technically a few other chars are permitted
# in repo names like dots, hyphens, underscores). The regex below covers all
# legal cases without permitting absolute paths, traversal (..), or
# attacker-controlled URL segments.
_REPO_RE = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9._-]{0,99})/[A-Za-z0-9._-]{1,100}$")


def parse_repo(value: Any) -> tuple[str, str]:
    """Validate ``value`` as ``owner/name`` and return ``(owner, name)``.

    Raises:
        ToolError: On any non-conforming value.
    """
    if not isinstance(value, str) or not value:
        raise ToolError("INVALID_ARGUMENTS", "repo is required and must be a string")
    if value.startswith("/") or value.startswith("\\"):
        raise ToolError(
            "INVALID_ARGUMENTS",
            "repo must be 'owner/name' (no absolute paths)",
            details={"repo": value},
        )
    if ".." in value:
        raise ToolError(
            "INVALID_ARGUMENTS",
            "repo must be 'owner/name' (no traversal)",
            details={"repo": value},
        )
    if not _REPO_RE.match(value):
        raise ToolError(
            "INVALID_ARGUMENTS",
            "repo must match 'owner/name'",
            details={"repo": value},
        )
    owner, name = value.split("/", 1)
    return owner, name


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
            "auth_config": {"provider": "github"},
        }


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _do_get(
    ctx: ToolContext, path: str, *, params: dict[str, Any] | None = None
) -> Any:
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
            f"GitHub request failed: {exc}",
            status_code=502,
            details={"path": path},
        ) from exc
    return _decode_response(resp, path=path)


async def _do_post(ctx: ToolContext, path: str, *, body: dict[str, Any]) -> Any:
    try:
        resp = await ctx.http_client.post(
            ctx.url(path),
            json=body,
            headers=ctx.headers(),
            timeout=ctx.timeout,
        )
    except httpx.HTTPError as exc:
        raise ToolError(
            "TOOL_INVOCATION_FAILED",
            f"GitHub request failed: {exc}",
            status_code=502,
            details={"path": path},
        ) from exc
    return _decode_response(resp, path=path)


async def _do_patch(ctx: ToolContext, path: str, *, body: dict[str, Any]) -> Any:
    try:
        resp = await ctx.http_client.patch(
            ctx.url(path),
            json=body,
            headers=ctx.headers(),
            timeout=ctx.timeout,
        )
    except httpx.HTTPError as exc:
        raise ToolError(
            "TOOL_INVOCATION_FAILED",
            f"GitHub request failed: {exc}",
            status_code=502,
            details={"path": path},
        ) from exc
    return _decode_response(resp, path=path)


def _decode_response(resp: httpx.Response, *, path: str) -> Any:
    if resp.status_code == 401:
        raise Unauthorized("GitHub rejected the access token (401)")
    if resp.status_code == 404:
        raise ToolError(
            "TOOL_INVOCATION_FAILED",
            "GitHub resource not found",
            status_code=404,
            details={"path": path, "status_code": 404},
        )
    if resp.status_code >= 400:
        body_preview = resp.text[:300]
        raise ToolError(
            "TOOL_INVOCATION_FAILED",
            f"GitHub returned HTTP {resp.status_code}",
            status_code=502,
            details={"path": path, "status_code": resp.status_code, "body_preview": body_preview},
        )
    try:
        return resp.json()
    except ValueError as exc:
        raise ToolError(
            "TOOL_INVOCATION_FAILED",
            f"GitHub returned non-JSON: {exc}",
            status_code=502,
            details={"path": path},
        ) from exc


def _slim_issue(raw: dict[str, Any]) -> dict[str, Any]:
    """Subset the GitHub issue payload to fields agents actually use."""
    user = raw.get("user") or {}
    return {
        "number": raw.get("number"),
        "title": raw.get("title"),
        "body": raw.get("body"),
        "state": raw.get("state"),
        "url": raw.get("html_url"),
        "user": {"login": user.get("login"), "id": user.get("id")} if user else None,
        "labels": [
            (lbl.get("name") if isinstance(lbl, dict) else str(lbl))
            for lbl in (raw.get("labels") or [])
        ],
        "comments": raw.get("comments"),
        "created_at": raw.get("created_at"),
        "updated_at": raw.get("updated_at"),
        "closed_at": raw.get("closed_at"),
        "pull_request": bool(raw.get("pull_request")),
    }


def _slim_repo(raw: dict[str, Any]) -> dict[str, Any]:
    owner = raw.get("owner") or {}
    return {
        "id": raw.get("id"),
        "full_name": raw.get("full_name"),
        "private": raw.get("private"),
        "description": raw.get("description"),
        "html_url": raw.get("html_url"),
        "default_branch": raw.get("default_branch"),
        "open_issues_count": raw.get("open_issues_count"),
        "stargazers_count": raw.get("stargazers_count"),
        "language": raw.get("language"),
        "owner_login": owner.get("login"),
    }


# ---------------------------------------------------------------------------
# Tool: github.list_issues
# ---------------------------------------------------------------------------


async def _list_issues(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    owner, name = parse_repo(args.get("repo"))
    state = args.get("state", "open")
    if state not in {"open", "closed", "all"}:
        raise ToolError("INVALID_ARGUMENTS", "state must be open|closed|all")
    per_page = args.get("per_page", 30)
    if not isinstance(per_page, int) or isinstance(per_page, bool) or per_page < 1 or per_page > 100:
        raise ToolError("INVALID_ARGUMENTS", "per_page must be an integer 1..100")
    page = args.get("page", 1)
    if not isinstance(page, int) or isinstance(page, bool) or page < 1:
        raise ToolError("INVALID_ARGUMENTS", "page must be a positive integer")

    params: dict[str, Any] = {"state": state, "per_page": per_page, "page": page}
    if isinstance(args.get("labels"), list) and args["labels"]:
        params["labels"] = ",".join(str(label) for label in args["labels"])
    raw = await _do_get(ctx, f"/repos/{owner}/{name}/issues", params=params)
    if not isinstance(raw, list):
        raise ToolError("TOOL_INVOCATION_FAILED", "GitHub returned non-list", status_code=502)
    # Filter out PRs (the issues API includes them by default).
    issues = [_slim_issue(item) for item in raw if not item.get("pull_request")]
    return {"issues": issues, "count": len(issues)}


LIST_ISSUES = Tool(
    tool_id="github.list_issues",
    name="List GitHub issues",
    description="List issues for a GitHub repo (excludes pull requests by default).",
    input_schema={
        "type": "object",
        "required": ["repo"],
        "properties": {
            "repo": {"type": "string", "description": "owner/name"},
            "state": {"type": "string", "enum": ["open", "closed", "all"], "default": "open"},
            "per_page": {"type": "integer", "minimum": 1, "maximum": 100, "default": 30},
            "page": {"type": "integer", "minimum": 1, "default": 1},
            "labels": {"type": "array", "items": {"type": "string"}},
        },
        "additionalProperties": False,
    },
    output_schema={"type": "object"},
    idempotent=True,
    side_effects="read",
    cache_ttl_seconds=60,
    handler=_list_issues,
)


# ---------------------------------------------------------------------------
# Tool: github.get_issue
# ---------------------------------------------------------------------------


async def _get_issue(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    owner, name = parse_repo(args.get("repo"))
    number = args.get("number")
    if not isinstance(number, int) or isinstance(number, bool) or number < 1:
        raise ToolError("INVALID_ARGUMENTS", "number must be a positive integer")

    issue = await _do_get(ctx, f"/repos/{owner}/{name}/issues/{number}")
    if not isinstance(issue, dict):
        raise ToolError("TOOL_INVOCATION_FAILED", "GitHub returned non-object", status_code=502)
    comments_raw = await _do_get(
        ctx, f"/repos/{owner}/{name}/issues/{number}/comments", params={"per_page": 100}
    )
    comments: list[dict[str, Any]] = []
    if isinstance(comments_raw, list):
        for c in comments_raw:
            user = c.get("user") or {}
            comments.append(
                {
                    "id": c.get("id"),
                    "user": {"login": user.get("login")} if user else None,
                    "body": c.get("body"),
                    "created_at": c.get("created_at"),
                }
            )
    return {"issue": _slim_issue(issue), "comments": comments}


GET_ISSUE = Tool(
    tool_id="github.get_issue",
    name="Get a GitHub issue",
    description="Fetch a single issue and its comments.",
    input_schema={
        "type": "object",
        "required": ["repo", "number"],
        "properties": {
            "repo": {"type": "string", "description": "owner/name"},
            "number": {"type": "integer", "minimum": 1},
        },
        "additionalProperties": False,
    },
    output_schema={"type": "object"},
    idempotent=True,
    side_effects="read",
    cache_ttl_seconds=60,
    handler=_get_issue,
)


# ---------------------------------------------------------------------------
# Tool: github.create_issue
# ---------------------------------------------------------------------------


async def _create_issue(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    owner, name = parse_repo(args.get("repo"))
    title = args.get("title")
    if not isinstance(title, str) or not title.strip():
        raise ToolError("INVALID_ARGUMENTS", "title is required")
    body: dict[str, Any] = {"title": title.strip()}
    if isinstance(args.get("body"), str):
        body["body"] = args["body"]
    if isinstance(args.get("labels"), list):
        body["labels"] = [str(label) for label in args["labels"]]
    if isinstance(args.get("assignees"), list):
        body["assignees"] = [str(a) for a in args["assignees"]]
    raw = await _do_post(ctx, f"/repos/{owner}/{name}/issues", body=body)
    if not isinstance(raw, dict):
        raise ToolError("TOOL_INVOCATION_FAILED", "GitHub returned non-object", status_code=502)
    return {"issue": _slim_issue(raw)}


CREATE_ISSUE = Tool(
    tool_id="github.create_issue",
    name="Create a GitHub issue",
    description="Open a new issue in a repo.",
    input_schema={
        "type": "object",
        "required": ["repo", "title"],
        "properties": {
            "repo": {"type": "string", "description": "owner/name"},
            "title": {"type": "string"},
            "body": {"type": "string"},
            "labels": {"type": "array", "items": {"type": "string"}},
            "assignees": {"type": "array", "items": {"type": "string"}},
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
# Tool: github.update_issue
# ---------------------------------------------------------------------------


async def _update_issue(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    owner, name = parse_repo(args.get("repo"))
    number = args.get("number")
    if not isinstance(number, int) or isinstance(number, bool) or number < 1:
        raise ToolError("INVALID_ARGUMENTS", "number must be a positive integer")
    body: dict[str, Any] = {}
    if isinstance(args.get("title"), str):
        body["title"] = args["title"]
    if "body" in args and (isinstance(args["body"], str) or args["body"] is None):
        body["body"] = args["body"]
    if isinstance(args.get("state"), str):
        if args["state"] not in {"open", "closed"}:
            raise ToolError("INVALID_ARGUMENTS", "state must be open|closed")
        body["state"] = args["state"]
    if isinstance(args.get("labels"), list):
        body["labels"] = [str(label) for label in args["labels"]]
    if not body:
        raise ToolError(
            "INVALID_ARGUMENTS",
            "at least one field (title/body/state/labels) is required",
        )
    raw = await _do_patch(ctx, f"/repos/{owner}/{name}/issues/{number}", body=body)
    if not isinstance(raw, dict):
        raise ToolError("TOOL_INVOCATION_FAILED", "GitHub returned non-object", status_code=502)
    return {"issue": _slim_issue(raw)}


UPDATE_ISSUE = Tool(
    tool_id="github.update_issue",
    name="Update a GitHub issue",
    description="Edit an issue's title/body/state/labels.",
    input_schema={
        "type": "object",
        "required": ["repo", "number"],
        "properties": {
            "repo": {"type": "string"},
            "number": {"type": "integer", "minimum": 1},
            "title": {"type": "string"},
            "body": {"type": ["string", "null"]},
            "state": {"type": "string", "enum": ["open", "closed"]},
            "labels": {"type": "array", "items": {"type": "string"}},
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
# Tool: github.comment_on_issue
# ---------------------------------------------------------------------------


async def _comment_on_issue(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    owner, name = parse_repo(args.get("repo"))
    number = args.get("number")
    if not isinstance(number, int) or isinstance(number, bool) or number < 1:
        raise ToolError("INVALID_ARGUMENTS", "number must be a positive integer")
    body_text = args.get("body")
    if not isinstance(body_text, str) or not body_text.strip():
        raise ToolError("INVALID_ARGUMENTS", "body must be a non-empty string")
    raw = await _do_post(
        ctx, f"/repos/{owner}/{name}/issues/{number}/comments", body={"body": body_text}
    )
    if not isinstance(raw, dict):
        raise ToolError("TOOL_INVOCATION_FAILED", "GitHub returned non-object", status_code=502)
    user = raw.get("user") or {}
    return {
        "comment": {
            "id": raw.get("id"),
            "body": raw.get("body"),
            "user": {"login": user.get("login")} if user else None,
            "created_at": raw.get("created_at"),
            "url": raw.get("html_url"),
        }
    }


COMMENT_ON_ISSUE = Tool(
    tool_id="github.comment_on_issue",
    name="Comment on a GitHub issue",
    description="Post a comment on an issue.",
    input_schema={
        "type": "object",
        "required": ["repo", "number", "body"],
        "properties": {
            "repo": {"type": "string"},
            "number": {"type": "integer", "minimum": 1},
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
# Tool: github.get_repo
# ---------------------------------------------------------------------------


async def _get_repo(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    owner, name = parse_repo(args.get("repo"))
    raw = await _do_get(ctx, f"/repos/{owner}/{name}")
    if not isinstance(raw, dict):
        raise ToolError("TOOL_INVOCATION_FAILED", "GitHub returned non-object", status_code=502)
    return {"repo": _slim_repo(raw)}


GET_REPO = Tool(
    tool_id="github.get_repo",
    name="Get repo metadata",
    description="Fetch metadata for a single repo (description, default branch, counts, etc.).",
    input_schema={
        "type": "object",
        "required": ["repo"],
        "properties": {"repo": {"type": "string"}},
        "additionalProperties": False,
    },
    output_schema={"type": "object"},
    idempotent=True,
    side_effects="read",
    cache_ttl_seconds=300,
    handler=_get_repo,
)


# ---------------------------------------------------------------------------
# Tool: github.search_code
# ---------------------------------------------------------------------------


async def _search_code(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    query = args.get("query")
    if not isinstance(query, str) or not query.strip():
        raise ToolError("INVALID_ARGUMENTS", "query is required")
    repo = args.get("repo")
    full_query = query.strip()
    if isinstance(repo, str) and repo:
        # parse_repo enforces the shape; we then append it as a qualifier.
        owner, name = parse_repo(repo)
        full_query = f"{full_query} repo:{owner}/{name}"
    per_page = args.get("per_page", 10)
    if not isinstance(per_page, int) or isinstance(per_page, bool) or per_page < 1 or per_page > 100:
        raise ToolError("INVALID_ARGUMENTS", "per_page must be an integer 1..100")

    params = {"q": full_query, "per_page": per_page}
    raw = await _do_get(ctx, "/search/code", params=params)
    if not isinstance(raw, dict):
        raise ToolError("TOOL_INVOCATION_FAILED", "GitHub returned non-object", status_code=502)
    items = raw.get("items") or []
    results: list[dict[str, Any]] = []
    if isinstance(items, list):
        for item in items:
            repo_obj = item.get("repository") or {}
            results.append(
                {
                    "name": item.get("name"),
                    "path": item.get("path"),
                    "html_url": item.get("html_url"),
                    "repo": repo_obj.get("full_name"),
                }
            )
    return {"total_count": raw.get("total_count", 0), "items": results}


SEARCH_CODE = Tool(
    tool_id="github.search_code",
    name="Search code on GitHub",
    description="Search code (optionally scoped to a repo) using GitHub's code search API.",
    input_schema={
        "type": "object",
        "required": ["query"],
        "properties": {
            "query": {"type": "string"},
            "repo": {"type": "string", "description": "Optional 'owner/name' scope"},
            "per_page": {"type": "integer", "minimum": 1, "maximum": 100, "default": 10},
        },
        "additionalProperties": False,
    },
    output_schema={"type": "object"},
    idempotent=True,
    side_effects="read",
    cache_ttl_seconds=120,
    handler=_search_code,
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
    GET_REPO,
    SEARCH_CODE,
]


TOOL_REGISTRY: dict[str, Tool] = {tool.tool_id: tool for tool in TOOL_LIST}
