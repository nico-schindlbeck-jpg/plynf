# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 The Plinth Authors
"""Tool implementations for the Notion MCP server.

Each tool is a small wrapper around a Notion REST endpoint. Every tool reads
the bearer token from the :class:`ToolContext` passed by the route handler;
the route handler in turn lifts it off the inbound ``Authorization`` header.

Notion's API has a few quirks we handle uniformly here:

* All endpoints require a ``Notion-Version`` header pinning the API version.
* ``POST /v1/search`` is the canonical search endpoint (no GET equivalent).
* ``POST /v1/databases/{id}/query`` accepts filter/sort objects in the body.
* ``PATCH /v1/blocks/{id}/children`` appends blocks to a parent (page or block).
* Page IDs may include hyphens (UUIDs); we accept either format.
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
    def __init__(self, message: str = "missing or invalid Notion access token") -> None:
        super().__init__("UNAUTHORIZED", message, status_code=401)


# ---------------------------------------------------------------------------
# Context
# ---------------------------------------------------------------------------


@dataclass
class ToolContext:
    """Per-request state injected into tool invocations.

    Attributes:
        access_token: The Notion bearer token. Validated non-empty at the
            route layer; tools that need it should use :meth:`require_token`.
        http_client: Shared httpx async client.
        api_base_url: Base of the Notion REST API (overridable for tests).
        api_version: Value sent as ``Notion-Version``.
        timeout: Per-request timeout in seconds.
    """

    access_token: str | None
    http_client: httpx.AsyncClient
    api_base_url: str = "https://api.notion.com"
    api_version: str = "2022-06-28"
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
            "Notion-Version": self.api_version,
            "User-Agent": "plinth-notion-mcp/1.1.0",
        }

    def url(self, path: str) -> str:
        if not path.startswith("/"):
            path = "/" + path
        return f"{self.api_base_url.rstrip('/')}{path}"


# ---------------------------------------------------------------------------
# ID validation
# ---------------------------------------------------------------------------


# Notion IDs are UUIDs (32 hex chars, optional hyphens).
_NOTION_ID_RE = re.compile(r"^[0-9a-fA-F]{32}$|^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")


def parse_notion_id(value: Any, *, name: str = "id") -> str:
    """Validate ``value`` as a Notion UUID and return it normalised.

    Accepts both 32-char hex (no hyphens) and the standard 8-4-4-4-12 form.

    Raises:
        ToolError: On any non-conforming value.
    """
    if not isinstance(value, str) or not value:
        raise ToolError("INVALID_ARGUMENTS", f"{name} is required and must be a string")
    if value.startswith("/") or value.startswith("\\"):
        raise ToolError(
            "INVALID_ARGUMENTS",
            f"{name} must be a Notion UUID (no absolute paths)",
            details={name: value},
        )
    if ".." in value:
        raise ToolError(
            "INVALID_ARGUMENTS",
            f"{name} must be a Notion UUID (no traversal)",
            details={name: value},
        )
    if not _NOTION_ID_RE.match(value):
        raise ToolError(
            "INVALID_ARGUMENTS",
            f"{name} must be a Notion UUID (32 hex chars, optional hyphens)",
            details={name: value},
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
            "auth_config": {"provider": "notion"},
        }


# ---------------------------------------------------------------------------
# HTTP helpers
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
            f"Notion request failed: {exc}",
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
            f"Notion request failed: {exc}",
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
            f"Notion request failed: {exc}",
            status_code=502,
            details={"path": path},
        ) from exc
    return _decode_response(resp, path=path)


def _decode_response(resp: httpx.Response, *, path: str) -> Any:
    if resp.status_code == 401:
        raise Unauthorized("Notion rejected the access token (401)")
    if resp.status_code == 404:
        raise ToolError(
            "TOOL_INVOCATION_FAILED",
            "Notion resource not found",
            status_code=404,
            details={"path": path, "status_code": 404},
        )
    if resp.status_code >= 400:
        body_preview = resp.text[:300]
        raise ToolError(
            "TOOL_INVOCATION_FAILED",
            f"Notion returned HTTP {resp.status_code}",
            status_code=502,
            details={"path": path, "status_code": resp.status_code, "body_preview": body_preview},
        )
    try:
        return resp.json()
    except ValueError as exc:
        raise ToolError(
            "TOOL_INVOCATION_FAILED",
            f"Notion returned non-JSON: {exc}",
            status_code=502,
            details={"path": path},
        ) from exc


# ---------------------------------------------------------------------------
# Slim helpers — keep payloads agent-friendly.
# ---------------------------------------------------------------------------


def _extract_title(properties: dict[str, Any] | None) -> str | None:
    """Pull a human-readable title out of a Notion page/database properties dict.

    Notion's title property is special: it lives under whichever key the
    workspace named "title" (often ``Name`` or ``title``). We scan all
    properties looking for the first ``"type": "title"`` entry, then concat
    its rich-text fragments.
    """
    if not isinstance(properties, dict):
        return None
    for value in properties.values():
        if not isinstance(value, dict):
            continue
        if value.get("type") != "title":
            continue
        rich = value.get("title")
        if isinstance(rich, list):
            return "".join(
                (item.get("plain_text") or "")
                for item in rich
                if isinstance(item, dict)
            ) or None
    return None


def _extract_database_title(raw: dict[str, Any]) -> str | None:
    """Database titles live at the top level (not in properties)."""
    rich = raw.get("title")
    if isinstance(rich, list):
        return "".join(
            (item.get("plain_text") or "")
            for item in rich
            if isinstance(item, dict)
        ) or None
    return None


def _slim_search_result(raw: dict[str, Any]) -> dict[str, Any]:
    """Return an agent-friendly shape for a single search hit."""
    obj_type = raw.get("object")  # "page" | "database"
    title: str | None = None
    if obj_type == "database":
        title = _extract_database_title(raw)
    else:
        title = _extract_title(raw.get("properties"))
    return {
        "id": raw.get("id"),
        "title": title,
        "url": raw.get("url"),
        "last_edited_time": raw.get("last_edited_time"),
        "type": obj_type,
    }


def _slim_database(raw: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": raw.get("id"),
        "title": _extract_database_title(raw),
        "url": raw.get("url"),
        "last_edited_time": raw.get("last_edited_time"),
    }


# ---------------------------------------------------------------------------
# Tool: notion.search
# ---------------------------------------------------------------------------


async def _search(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    query = args.get("query")
    if query is not None and not isinstance(query, str):
        raise ToolError("INVALID_ARGUMENTS", "query must be a string")
    page_size = args.get("page_size", 20)
    if not isinstance(page_size, int) or isinstance(page_size, bool) or page_size < 1 or page_size > 100:
        raise ToolError("INVALID_ARGUMENTS", "page_size must be an integer 1..100")

    body: dict[str, Any] = {"page_size": page_size}
    if isinstance(query, str) and query.strip():
        body["query"] = query.strip()
    raw = await _do_post(ctx, "/v1/search", body=body)
    if not isinstance(raw, dict):
        raise ToolError("TOOL_INVOCATION_FAILED", "Notion returned non-object", status_code=502)
    items = raw.get("results") or []
    results = [
        _slim_search_result(item) for item in items if isinstance(item, dict)
    ]
    return {"results": results, "count": len(results)}


SEARCH = Tool(
    tool_id="notion.search",
    name="Search Notion workspace",
    description="Search across pages and databases in the connected workspace.",
    input_schema={
        "type": "object",
        "properties": {
            "query": {"type": "string"},
            "page_size": {"type": "integer", "minimum": 1, "maximum": 100, "default": 20},
        },
        "additionalProperties": False,
    },
    output_schema={"type": "object"},
    idempotent=True,
    side_effects="read",
    cache_ttl_seconds=60,
    handler=_search,
)


# ---------------------------------------------------------------------------
# Tool: notion.get_page
# ---------------------------------------------------------------------------


async def _get_page(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    page_id = parse_notion_id(args.get("page_id"), name="page_id")

    page = await _do_get(ctx, f"/v1/pages/{page_id}")
    if not isinstance(page, dict):
        raise ToolError("TOOL_INVOCATION_FAILED", "Notion returned non-object", status_code=502)
    # Fetch top-level block children for content. Caller can paginate further
    # via append_block / direct API if they need more than 100 blocks.
    children = await _do_get(
        ctx, f"/v1/blocks/{page_id}/children", params={"page_size": 100}
    )
    content: list[dict[str, Any]] = []
    if isinstance(children, dict):
        items = children.get("results") or []
        if isinstance(items, list):
            for item in items:
                if isinstance(item, dict):
                    content.append(
                        {
                            "id": item.get("id"),
                            "type": item.get("type"),
                            "has_children": item.get("has_children"),
                        }
                    )
    return {
        "id": page.get("id"),
        "title": _extract_title(page.get("properties")),
        "properties": page.get("properties") or {},
        "content": content,
        "url": page.get("url"),
        "archived": page.get("archived", False),
        "created_time": page.get("created_time"),
        "last_edited_time": page.get("last_edited_time"),
        "parent": page.get("parent"),
    }


GET_PAGE = Tool(
    tool_id="notion.get_page",
    name="Get a Notion page",
    description="Fetch a single page (properties + top-level block children).",
    input_schema={
        "type": "object",
        "required": ["page_id"],
        "properties": {
            "page_id": {"type": "string", "description": "Notion UUID"},
        },
        "additionalProperties": False,
    },
    output_schema={"type": "object"},
    idempotent=True,
    side_effects="read",
    cache_ttl_seconds=30,
    handler=_get_page,
)


# ---------------------------------------------------------------------------
# Tool: notion.create_page
# ---------------------------------------------------------------------------


async def _create_page(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    title = args.get("title")
    if not isinstance(title, str) or not title.strip():
        raise ToolError("INVALID_ARGUMENTS", "title is required")

    parent_database_id = args.get("parent_database_id")
    parent_page_id = args.get("parent_page_id")
    if parent_database_id is None and parent_page_id is None:
        raise ToolError(
            "INVALID_ARGUMENTS",
            "either parent_database_id or parent_page_id is required",
        )
    if parent_database_id is not None and parent_page_id is not None:
        raise ToolError(
            "INVALID_ARGUMENTS",
            "specify only one of parent_database_id or parent_page_id",
        )

    parent: dict[str, Any]
    properties: dict[str, Any]
    if parent_database_id is not None:
        db_id = parse_notion_id(parent_database_id, name="parent_database_id")
        parent = {"database_id": db_id}
        # Caller-supplied properties override the title default.
        properties = dict(args.get("properties") or {})
        # Title key in a database is typically called "Name" or "title". We
        # default to a key called "title" with rich-text from the title arg —
        # callers with a non-default title prop name should pass full
        # ``properties`` and we'll merge.
        if not properties:
            properties = {
                "title": {
                    "title": [{"type": "text", "text": {"content": title.strip()}}]
                }
            }
    else:
        page_id = parse_notion_id(parent_page_id, name="parent_page_id")
        parent = {"page_id": page_id}
        # When the parent is a page, the "properties" field accepts only a
        # ``title`` rich-text array.
        properties = {
            "title": {
                "title": [{"type": "text", "text": {"content": title.strip()}}]
            }
        }

    body: dict[str, Any] = {"parent": parent, "properties": properties}
    if "content" in args:
        content = args.get("content")
        if not isinstance(content, list):
            raise ToolError("INVALID_ARGUMENTS", "content must be a list of blocks")
        body["children"] = content

    raw = await _do_post(ctx, "/v1/pages", body=body)
    if not isinstance(raw, dict):
        raise ToolError("TOOL_INVOCATION_FAILED", "Notion returned non-object", status_code=502)
    return {"id": raw.get("id"), "url": raw.get("url")}


CREATE_PAGE = Tool(
    tool_id="notion.create_page",
    name="Create a Notion page",
    description="Create a page either as a database row (parent_database_id) or page child (parent_page_id).",
    input_schema={
        "type": "object",
        "required": ["title"],
        "properties": {
            "parent_database_id": {"type": "string"},
            "parent_page_id": {"type": "string"},
            "title": {"type": "string"},
            "properties": {"type": "object"},
            "content": {"type": "array"},
        },
        "additionalProperties": False,
    },
    output_schema={"type": "object"},
    idempotent=False,
    side_effects="write",
    cache_ttl_seconds=None,
    handler=_create_page,
)


# ---------------------------------------------------------------------------
# Tool: notion.update_page
# ---------------------------------------------------------------------------


async def _update_page(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    page_id = parse_notion_id(args.get("page_id"), name="page_id")
    body: dict[str, Any] = {}
    if "properties" in args:
        properties = args.get("properties")
        if not isinstance(properties, dict):
            raise ToolError("INVALID_ARGUMENTS", "properties must be an object")
        body["properties"] = properties
    if "archived" in args:
        archived = args.get("archived")
        if not isinstance(archived, bool):
            raise ToolError("INVALID_ARGUMENTS", "archived must be a boolean")
        body["archived"] = archived
    if not body:
        raise ToolError(
            "INVALID_ARGUMENTS",
            "at least one of properties/archived is required",
        )

    raw = await _do_patch(ctx, f"/v1/pages/{page_id}", body=body)
    if not isinstance(raw, dict):
        raise ToolError("TOOL_INVOCATION_FAILED", "Notion returned non-object", status_code=502)
    return {
        "id": raw.get("id"),
        "updated_at": raw.get("last_edited_time"),
        "archived": raw.get("archived", False),
    }


UPDATE_PAGE = Tool(
    tool_id="notion.update_page",
    name="Update a Notion page",
    description="Update properties or archive flag on an existing page.",
    input_schema={
        "type": "object",
        "required": ["page_id"],
        "properties": {
            "page_id": {"type": "string"},
            "properties": {"type": "object"},
            "archived": {"type": "boolean"},
        },
        "additionalProperties": False,
    },
    output_schema={"type": "object"},
    idempotent=False,
    side_effects="write",
    cache_ttl_seconds=None,
    handler=_update_page,
)


# ---------------------------------------------------------------------------
# Tool: notion.append_block
# ---------------------------------------------------------------------------


async def _append_block(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    page_id = parse_notion_id(args.get("page_id"), name="page_id")
    blocks = args.get("blocks")
    if not isinstance(blocks, list) or not blocks:
        raise ToolError("INVALID_ARGUMENTS", "blocks must be a non-empty list")

    raw = await _do_patch(
        ctx, f"/v1/blocks/{page_id}/children", body={"children": blocks}
    )
    if not isinstance(raw, dict):
        raise ToolError("TOOL_INVOCATION_FAILED", "Notion returned non-object", status_code=502)
    appended = raw.get("results") or []
    return {"appended": len(appended) if isinstance(appended, list) else 0}


APPEND_BLOCK = Tool(
    tool_id="notion.append_block",
    name="Append blocks to a Notion page",
    description="Append one or more blocks (children) to a page or block.",
    input_schema={
        "type": "object",
        "required": ["page_id", "blocks"],
        "properties": {
            "page_id": {"type": "string"},
            "blocks": {"type": "array"},
        },
        "additionalProperties": False,
    },
    output_schema={"type": "object"},
    idempotent=False,
    side_effects="write",
    cache_ttl_seconds=None,
    handler=_append_block,
)


# ---------------------------------------------------------------------------
# Tool: notion.list_databases
# ---------------------------------------------------------------------------


async def _list_databases(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    # Notion's recommended way to enumerate databases is search-with-filter.
    body = {"filter": {"property": "object", "value": "database"}, "page_size": 100}
    raw = await _do_post(ctx, "/v1/search", body=body)
    if not isinstance(raw, dict):
        raise ToolError("TOOL_INVOCATION_FAILED", "Notion returned non-object", status_code=502)
    items = raw.get("results") or []
    databases = [
        _slim_database(item)
        for item in items
        if isinstance(item, dict) and item.get("object") == "database"
    ]
    return {"databases": databases, "count": len(databases)}


LIST_DATABASES = Tool(
    tool_id="notion.list_databases",
    name="List Notion databases",
    description="List databases accessible to the connected integration.",
    input_schema={
        "type": "object",
        "properties": {},
        "additionalProperties": False,
    },
    output_schema={"type": "object"},
    idempotent=True,
    side_effects="read",
    cache_ttl_seconds=60,
    handler=_list_databases,
)


# ---------------------------------------------------------------------------
# Tool: notion.query_database
# ---------------------------------------------------------------------------


async def _query_database(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    database_id = parse_notion_id(args.get("database_id"), name="database_id")
    page_size = args.get("page_size", 20)
    if not isinstance(page_size, int) or isinstance(page_size, bool) or page_size < 1 or page_size > 100:
        raise ToolError("INVALID_ARGUMENTS", "page_size must be an integer 1..100")

    body: dict[str, Any] = {"page_size": page_size}
    if "filter" in args:
        filter_obj = args.get("filter")
        if not isinstance(filter_obj, dict):
            raise ToolError("INVALID_ARGUMENTS", "filter must be an object")
        body["filter"] = filter_obj
    if "sorts" in args:
        sorts = args.get("sorts")
        if not isinstance(sorts, list):
            raise ToolError("INVALID_ARGUMENTS", "sorts must be a list")
        body["sorts"] = sorts
    if "start_cursor" in args:
        cursor = args.get("start_cursor")
        if cursor is not None and not isinstance(cursor, str):
            raise ToolError("INVALID_ARGUMENTS", "start_cursor must be a string")
        if cursor:
            body["start_cursor"] = cursor

    raw = await _do_post(ctx, f"/v1/databases/{database_id}/query", body=body)
    if not isinstance(raw, dict):
        raise ToolError("TOOL_INVOCATION_FAILED", "Notion returned non-object", status_code=502)
    items = raw.get("results") or []
    results = [
        {
            "id": item.get("id"),
            "title": _extract_title(item.get("properties")),
            "url": item.get("url"),
            "properties": item.get("properties") or {},
            "last_edited_time": item.get("last_edited_time"),
        }
        for item in items
        if isinstance(item, dict)
    ]
    return {
        "results": results,
        "has_more": bool(raw.get("has_more")),
        "next_cursor": raw.get("next_cursor"),
    }


QUERY_DATABASE = Tool(
    tool_id="notion.query_database",
    name="Query a Notion database",
    description="Query a database with optional filter + sorts; returns rows.",
    input_schema={
        "type": "object",
        "required": ["database_id"],
        "properties": {
            "database_id": {"type": "string"},
            "filter": {"type": "object"},
            "sorts": {"type": "array"},
            "start_cursor": {"type": "string"},
            "page_size": {"type": "integer", "minimum": 1, "maximum": 100, "default": 20},
        },
        "additionalProperties": False,
    },
    output_schema={"type": "object"},
    idempotent=True,
    side_effects="read",
    cache_ttl_seconds=30,
    handler=_query_database,
)


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------


TOOL_LIST: list[Tool] = [
    SEARCH,
    GET_PAGE,
    CREATE_PAGE,
    UPDATE_PAGE,
    APPEND_BLOCK,
    LIST_DATABASES,
    QUERY_DATABASE,
]


TOOL_REGISTRY: dict[str, Tool] = {tool.tool_id: tool for tool in TOOL_LIST}
