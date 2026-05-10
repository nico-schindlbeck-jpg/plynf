# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 The Plinth Authors
"""Tool implementations for the Asana MCP server.

Asana's REST API wraps every response in ``{"data": ...}`` (single resource)
or ``{"data": [...]}`` (collections). Tools here flatten that out so agent
code sees flat dicts/lists. IDs (``gid`` in Asana lingo) are returned as
strings — Asana's are always digit strings up to 16 chars.
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
    def __init__(self, message: str = "missing or invalid Asana access token") -> None:
        super().__init__("UNAUTHORIZED", message, status_code=401)


# ---------------------------------------------------------------------------
# Context
# ---------------------------------------------------------------------------


@dataclass
class ToolContext:
    """Per-request state injected into tool invocations."""

    access_token: str | None
    http_client: httpx.AsyncClient
    api_base_url: str = "https://app.asana.com/api/1.0"
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
            "User-Agent": "plinth-asana-mcp/1.5.0",
        }

    def url(self, path: str) -> str:
        if not path.startswith("/"):
            path = "/" + path
        return f"{self.api_base_url.rstrip('/')}{path}"


# ---------------------------------------------------------------------------
# Validation helpers
# ---------------------------------------------------------------------------


# Asana ``gid`` values are unsigned integers (digit strings, up to 16 digits).
_GID_RE = re.compile(r"^\d{1,20}$")


def parse_gid(value: Any, *, name: str = "gid") -> str:
    if isinstance(value, bool) or not isinstance(value, (str, int)):
        raise ToolError("INVALID_ARGUMENTS", f"{name} is required")
    s = str(value).strip()
    if not s or "/" in s or "\\" in s or ".." in s:
        raise ToolError(
            "INVALID_ARGUMENTS",
            f"{name} must be an Asana gid (numeric string)",
            details={name: value},
        )
    if not _GID_RE.match(s):
        raise ToolError(
            "INVALID_ARGUMENTS",
            f"{name} must be a numeric Asana gid",
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
            "auth_config": {"provider": "asana"},
        }


# ---------------------------------------------------------------------------
# HTTP helpers
# ---------------------------------------------------------------------------


async def _do_get(ctx: ToolContext, path: str, *, params: dict[str, Any] | None = None) -> Any:
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
            f"Asana request failed: {exc}",
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
            f"Asana request failed: {exc}",
            status_code=502,
            details={"path": path},
        ) from exc
    return _decode_response(resp, path=path)


async def _do_put(ctx: ToolContext, path: str, *, body: dict[str, Any]) -> Any:
    try:
        resp = await ctx.http_client.put(
            ctx.url(path),
            json=body,
            headers=ctx.headers(),
            timeout=ctx.timeout,
        )
    except httpx.HTTPError as exc:
        raise ToolError(
            "TOOL_INVOCATION_FAILED",
            f"Asana request failed: {exc}",
            status_code=502,
            details={"path": path},
        ) from exc
    return _decode_response(resp, path=path)


def _decode_response(resp: httpx.Response, *, path: str) -> Any:
    if resp.status_code == 401:
        raise Unauthorized("Asana rejected the access token (401)")
    if resp.status_code == 403:
        raise ToolError(
            "TOOL_INVOCATION_FAILED",
            "Asana permission denied (403)",
            status_code=403,
            details={"path": path, "status_code": 403},
        )
    if resp.status_code == 404:
        raise ToolError(
            "TOOL_INVOCATION_FAILED",
            "Asana resource not found",
            status_code=404,
            details={"path": path, "status_code": 404},
        )
    if resp.status_code == 204:
        return None
    if resp.status_code >= 400:
        body_preview = resp.text[:300]
        raise ToolError(
            "TOOL_INVOCATION_FAILED",
            f"Asana returned HTTP {resp.status_code}",
            status_code=502,
            details={"path": path, "status_code": resp.status_code, "body_preview": body_preview},
        )
    if not resp.text:
        return None
    try:
        return resp.json()
    except ValueError as exc:
        raise ToolError(
            "TOOL_INVOCATION_FAILED",
            f"Asana returned non-JSON: {exc}",
            status_code=502,
            details={"path": path},
        ) from exc


def _unwrap_data(raw: Any) -> Any:
    """Return ``raw["data"]`` when wrapped, else ``raw``.

    Asana wraps all responses; tests may sometimes pass non-wrapped fixtures.
    """
    if isinstance(raw, dict) and "data" in raw:
        return raw["data"]
    return raw


# ---------------------------------------------------------------------------
# Slim helpers
# ---------------------------------------------------------------------------


def _slim_workspace(raw: dict[str, Any]) -> dict[str, Any]:
    return {
        "gid": str(raw.get("gid")) if raw.get("gid") is not None else None,
        "name": raw.get("name"),
        "resource_type": raw.get("resource_type"),
    }


def _slim_project(raw: dict[str, Any]) -> dict[str, Any]:
    return {
        "gid": str(raw.get("gid")) if raw.get("gid") is not None else None,
        "name": raw.get("name"),
        "resource_type": raw.get("resource_type"),
        "archived": bool(raw.get("archived", False)),
        "workspace": (
            (raw.get("workspace") or {}).get("gid")
            if isinstance(raw.get("workspace"), dict)
            else None
        ),
    }


def _slim_task(raw: dict[str, Any]) -> dict[str, Any]:
    assignee = raw.get("assignee") if isinstance(raw.get("assignee"), dict) else None
    return {
        "gid": str(raw.get("gid")) if raw.get("gid") is not None else None,
        "name": raw.get("name"),
        "completed": bool(raw.get("completed", False)),
        "assignee": (assignee.get("name") if isinstance(assignee, dict) else None),
        "due_on": raw.get("due_on"),
        "notes": raw.get("notes"),
        "resource_type": raw.get("resource_type"),
        "permalink_url": raw.get("permalink_url"),
    }


# ---------------------------------------------------------------------------
# Tool: asana.list_workspaces
# ---------------------------------------------------------------------------


async def _list_workspaces(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    raw = await _do_get(ctx, "/workspaces")
    data = _unwrap_data(raw)
    items = data if isinstance(data, list) else []
    workspaces = [_slim_workspace(item) for item in items if isinstance(item, dict)]
    return {"workspaces": workspaces, "count": len(workspaces)}


LIST_WORKSPACES = Tool(
    tool_id="asana.list_workspaces",
    name="List Asana workspaces",
    description="List all workspaces accessible to the authenticated user.",
    input_schema={
        "type": "object",
        "properties": {},
        "additionalProperties": False,
    },
    output_schema={"type": "object"},
    idempotent=True,
    side_effects="read",
    cache_ttl_seconds=300,
    handler=_list_workspaces,
)


# ---------------------------------------------------------------------------
# Tool: asana.list_projects
# ---------------------------------------------------------------------------


async def _list_projects(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    workspace_gid = parse_gid(args.get("workspace_gid"), name="workspace_gid")
    archived = args.get("archived")
    if archived is not None and not isinstance(archived, bool):
        raise ToolError("INVALID_ARGUMENTS", "archived must be a boolean")

    params: dict[str, Any] = {"workspace": workspace_gid}
    if archived is not None:
        params["archived"] = "true" if archived else "false"
    raw = await _do_get(ctx, "/projects", params=params)
    data = _unwrap_data(raw)
    items = data if isinstance(data, list) else []
    projects = [_slim_project(item) for item in items if isinstance(item, dict)]
    return {"projects": projects, "count": len(projects)}


LIST_PROJECTS = Tool(
    tool_id="asana.list_projects",
    name="List Asana projects",
    description="List projects in a workspace (optionally filter by archived).",
    input_schema={
        "type": "object",
        "required": ["workspace_gid"],
        "properties": {
            "workspace_gid": {"type": "string"},
            "archived": {"type": "boolean"},
        },
        "additionalProperties": False,
    },
    output_schema={"type": "object"},
    idempotent=True,
    side_effects="read",
    cache_ttl_seconds=60,
    handler=_list_projects,
)


# ---------------------------------------------------------------------------
# Tool: asana.list_tasks
# ---------------------------------------------------------------------------


async def _list_tasks(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    project_gid = parse_gid(args.get("project_gid"), name="project_gid")
    completed_since = args.get("completed_since")
    if completed_since is not None and not isinstance(completed_since, str):
        raise ToolError("INVALID_ARGUMENTS", "completed_since must be a string")
    limit = args.get("limit", 50)
    if not isinstance(limit, int) or isinstance(limit, bool) or limit < 1 or limit > 100:
        raise ToolError("INVALID_ARGUMENTS", "limit must be an integer 1..100")

    params: dict[str, Any] = {"limit": limit}
    if completed_since:
        params["completed_since"] = completed_since
    raw = await _do_get(ctx, f"/projects/{project_gid}/tasks", params=params)
    data = _unwrap_data(raw)
    items = data if isinstance(data, list) else []
    tasks = [_slim_task(item) for item in items if isinstance(item, dict)]
    return {"tasks": tasks, "count": len(tasks)}


LIST_TASKS = Tool(
    tool_id="asana.list_tasks",
    name="List tasks in an Asana project",
    description="List tasks in a project (with optional completed_since filter).",
    input_schema={
        "type": "object",
        "required": ["project_gid"],
        "properties": {
            "project_gid": {"type": "string"},
            "completed_since": {"type": "string"},
            "limit": {"type": "integer", "minimum": 1, "maximum": 100, "default": 50},
        },
        "additionalProperties": False,
    },
    output_schema={"type": "object"},
    idempotent=True,
    side_effects="read",
    cache_ttl_seconds=30,
    handler=_list_tasks,
)


# ---------------------------------------------------------------------------
# Tool: asana.get_task
# ---------------------------------------------------------------------------


async def _get_task(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    task_gid = parse_gid(args.get("task_gid"), name="task_gid")
    raw = await _do_get(ctx, f"/tasks/{task_gid}")
    data = _unwrap_data(raw)
    if not isinstance(data, dict):
        raise ToolError(
            "TOOL_INVOCATION_FAILED",
            "Asana returned non-object",
            status_code=502,
        )
    slim = _slim_task(data)
    # Include a fuller view for clients that want raw membership/projects.
    slim["projects"] = [
        {"gid": str(p.get("gid")) if p.get("gid") is not None else None, "name": p.get("name")}
        for p in (data.get("projects") or [])
        if isinstance(p, dict)
    ]
    return slim


GET_TASK = Tool(
    tool_id="asana.get_task",
    name="Get an Asana task",
    description="Fetch a single Asana task with its slim fields + projects.",
    input_schema={
        "type": "object",
        "required": ["task_gid"],
        "properties": {
            "task_gid": {"type": "string"},
        },
        "additionalProperties": False,
    },
    output_schema={"type": "object"},
    idempotent=True,
    side_effects="read",
    cache_ttl_seconds=15,
    handler=_get_task,
)


# ---------------------------------------------------------------------------
# Tool: asana.create_task
# ---------------------------------------------------------------------------


async def _create_task(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    name = args.get("name")
    if not isinstance(name, str) or not name.strip():
        raise ToolError("INVALID_ARGUMENTS", "name is required")

    workspace_gid = args.get("workspace_gid")
    project_gids = args.get("project_gids")
    if workspace_gid is None and not project_gids:
        raise ToolError(
            "INVALID_ARGUMENTS",
            "either workspace_gid or project_gids is required",
        )
    if workspace_gid is not None and project_gids:
        raise ToolError(
            "INVALID_ARGUMENTS",
            "specify only one of workspace_gid or project_gids",
        )

    data: dict[str, Any] = {"name": name.strip()}
    if workspace_gid is not None:
        data["workspace"] = parse_gid(workspace_gid, name="workspace_gid")
    if project_gids is not None:
        if not isinstance(project_gids, list) or not project_gids:
            raise ToolError(
                "INVALID_ARGUMENTS",
                "project_gids must be a non-empty list",
            )
        data["projects"] = [parse_gid(gid, name="project_gids") for gid in project_gids]
    if "notes" in args:
        notes = args.get("notes")
        if not isinstance(notes, str):
            raise ToolError("INVALID_ARGUMENTS", "notes must be a string")
        data["notes"] = notes
    if "assignee" in args:
        assignee = args.get("assignee")
        if not isinstance(assignee, str) or not assignee:
            raise ToolError("INVALID_ARGUMENTS", "assignee must be a string")
        data["assignee"] = assignee
    if "due_on" in args:
        due_on = args.get("due_on")
        if not isinstance(due_on, str) or not due_on:
            raise ToolError("INVALID_ARGUMENTS", "due_on must be a string (YYYY-MM-DD)")
        data["due_on"] = due_on

    raw = await _do_post(ctx, "/tasks", body={"data": data})
    payload = _unwrap_data(raw)
    if not isinstance(payload, dict):
        raise ToolError(
            "TOOL_INVOCATION_FAILED",
            "Asana returned non-object",
            status_code=502,
        )
    return _slim_task(payload)


CREATE_TASK = Tool(
    tool_id="asana.create_task",
    name="Create an Asana task",
    description="Create a task either in a workspace or attached to project_gids.",
    input_schema={
        "type": "object",
        "required": ["name"],
        "properties": {
            "name": {"type": "string"},
            "workspace_gid": {"type": "string"},
            "project_gids": {"type": "array", "items": {"type": "string"}},
            "notes": {"type": "string"},
            "assignee": {"type": "string"},
            "due_on": {"type": "string"},
        },
        "additionalProperties": False,
    },
    output_schema={"type": "object"},
    idempotent=False,
    side_effects="write",
    cache_ttl_seconds=None,
    handler=_create_task,
)


# ---------------------------------------------------------------------------
# Tool: asana.update_task
# ---------------------------------------------------------------------------


async def _update_task(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    task_gid = parse_gid(args.get("task_gid"), name="task_gid")
    data: dict[str, Any] = {}
    if "name" in args:
        name = args.get("name")
        if not isinstance(name, str) or not name.strip():
            raise ToolError("INVALID_ARGUMENTS", "name must be a non-empty string")
        data["name"] = name.strip()
    if "notes" in args:
        notes = args.get("notes")
        if not isinstance(notes, str):
            raise ToolError("INVALID_ARGUMENTS", "notes must be a string")
        data["notes"] = notes
    if "completed" in args:
        completed = args.get("completed")
        if not isinstance(completed, bool):
            raise ToolError("INVALID_ARGUMENTS", "completed must be a boolean")
        data["completed"] = completed
    if "assignee" in args:
        assignee = args.get("assignee")
        if not isinstance(assignee, str):
            raise ToolError("INVALID_ARGUMENTS", "assignee must be a string")
        data["assignee"] = assignee
    if "due_on" in args:
        due_on = args.get("due_on")
        if due_on is not None and not isinstance(due_on, str):
            raise ToolError("INVALID_ARGUMENTS", "due_on must be a string or null")
        data["due_on"] = due_on
    if not data:
        raise ToolError(
            "INVALID_ARGUMENTS",
            "at least one of name/notes/completed/assignee/due_on is required",
        )

    raw = await _do_put(ctx, f"/tasks/{task_gid}", body={"data": data})
    payload = _unwrap_data(raw)
    if not isinstance(payload, dict):
        raise ToolError(
            "TOOL_INVOCATION_FAILED",
            "Asana returned non-object",
            status_code=502,
        )
    return _slim_task(payload)


UPDATE_TASK = Tool(
    tool_id="asana.update_task",
    name="Update an Asana task",
    description="Edit fields on an existing task.",
    input_schema={
        "type": "object",
        "required": ["task_gid"],
        "properties": {
            "task_gid": {"type": "string"},
            "name": {"type": "string"},
            "notes": {"type": "string"},
            "completed": {"type": "boolean"},
            "assignee": {"type": "string"},
            "due_on": {"type": "string"},
        },
        "additionalProperties": False,
    },
    output_schema={"type": "object"},
    idempotent=False,
    side_effects="write",
    cache_ttl_seconds=None,
    handler=_update_task,
)


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------


TOOL_LIST: list[Tool] = [
    LIST_WORKSPACES,
    LIST_PROJECTS,
    LIST_TASKS,
    GET_TASK,
    CREATE_TASK,
    UPDATE_TASK,
]


TOOL_REGISTRY: dict[str, Tool] = {tool.tool_id: tool for tool in TOOL_LIST}
