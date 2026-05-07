# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 The Plinth Authors
"""Tool implementations for the Mock MCP Server.

Each tool exposes:

* a ``Tool`` registration record (the metadata returned from ``GET /tools``).
* an async ``invoke(args, ctx)`` callable that does the work.

Tools are registered in :data:`TOOL_REGISTRY` and dispatched by id from the
FastAPI route handler.

The set of tools is deliberately small and deterministic: it powers the
research-agent demo and any other examples that want a stable offline
endpoint to exercise tool calls against.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Awaitable, Callable, Literal
from urllib.parse import urlparse

import httpx
from ulid import ULID

from . import fixtures
from .logging_config import get_logger

logger = get_logger(__name__)


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class ToolError(Exception):
    """A tool-level error that maps to a Plinth error envelope.

    Attributes:
        code: Plinth error code (e.g. ``"INVALID_ARGUMENTS"``).
        message: Human-readable description.
        status_code: HTTP status to surface (default 400).
        details: Optional structured details dict.
    """

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


# ---------------------------------------------------------------------------
# Context passed to tool implementations
# ---------------------------------------------------------------------------


@dataclass
class ToolContext:
    """Per-request state injected into tool invocations.

    Attributes:
        fixtures_dir: Absolute filesystem root for ``fs.*`` tools.
        notes: In-process list of notes appended by ``notes.add``.
        http_client: Shared httpx client for outbound requests.
    """

    fixtures_dir: Path
    notes: list[dict[str, str]]
    http_client: httpx.AsyncClient


# ---------------------------------------------------------------------------
# Path safety
# ---------------------------------------------------------------------------


def safe_path(base: Path, rel: str) -> Path:
    """Resolve ``rel`` under ``base`` and reject path-traversal attempts.

    Args:
        base: The fixtures-root directory.
        rel: User-supplied relative path.

    Returns:
        The resolved absolute :class:`Path`.

    Raises:
        ToolError: If the resolved path escapes ``base``.
    """
    base_resolved = base.resolve()
    candidate = (base_resolved / rel).resolve()
    # Use is_relative_to via prefix string comparison for 3.9+ compat.
    base_str = str(base_resolved)
    candidate_str = str(candidate)
    if not (candidate_str == base_str or candidate_str.startswith(base_str + "/")):
        raise ToolError(
            code="INVALID_ARGUMENTS",
            message="path traversal blocked",
            details={"path": rel},
        )
    return candidate


# ---------------------------------------------------------------------------
# Tool registry record
# ---------------------------------------------------------------------------


ToolHandler = Callable[[dict[str, Any], ToolContext], Awaitable[dict[str, Any]]]


@dataclass(frozen=True)
class Tool:
    """Static description of a tool.

    The fields mirror :class:`plinth_gateway.models.ToolRegistration` so that
    a Plinth Gateway can register the mock tools verbatim.
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
        """Return the JSON dict representation used in ``GET /tools``."""
        return {
            "tool_id": self.tool_id,
            "name": self.name,
            "description": self.description,
            "input_schema": self.input_schema,
            "output_schema": self.output_schema,
            "idempotent": self.idempotent,
            "side_effects": self.side_effects,
            "cache_ttl_seconds": self.cache_ttl_seconds,
        }


# ---------------------------------------------------------------------------
# Tool: web.fetch
# ---------------------------------------------------------------------------


async def _web_fetch(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    """Fetch a URL and return its text content.

    Supports two URL schemes:

    * ``mock://...`` — returns canned fixture content. No I/O performed.
    * ``https://...`` (or ``http://``) — uses httpx with a 10-second timeout.

    Other schemes (``file://``, ``ftp://``, etc.) are rejected.
    """
    url = args.get("url")
    if not isinstance(url, str) or not url:
        raise ToolError("INVALID_ARGUMENTS", "url is required and must be a string")

    scheme = urlparse(url).scheme.lower()

    if scheme == "mock":
        entry = fixtures.lookup_url(url)
        if entry is None:
            raise ToolError(
                code="INVALID_ARGUMENTS",
                message=f"unknown mock url: {url}",
                status_code=404,
                details={"url": url},
            )
        return {
            "content": entry["content"],
            "status": 200,
            "content_type": "text/plain; charset=utf-8",
        }

    if scheme in {"http", "https"}:
        try:
            response = await ctx.http_client.get(url, timeout=10.0)
        except httpx.HTTPError as exc:
            raise ToolError(
                code="TOOL_INVOCATION_FAILED",
                message=f"http request failed: {exc}",
                status_code=500,
                details={"url": url},
            ) from exc
        return {
            "content": response.text,
            "status": response.status_code,
            "content_type": response.headers.get("content-type", "application/octet-stream"),
        }

    raise ToolError(
        code="INVALID_ARGUMENTS",
        message=f"unsupported url scheme: {scheme!r}",
        details={"url": url},
    )


WEB_FETCH = Tool(
    tool_id="web.fetch",
    name="Fetch a web page",
    description="Fetch a URL and return its text content. Supports mock:// URLs from fixtures.",
    input_schema={
        "type": "object",
        "required": ["url"],
        "properties": {
            "url": {"type": "string", "description": "The URL to fetch (mock:// or https://)"},
        },
        "additionalProperties": False,
    },
    output_schema={
        "type": "object",
        "required": ["content", "status", "content_type"],
        "properties": {
            "content": {"type": "string"},
            "status": {"type": "integer"},
            "content_type": {"type": "string"},
        },
    },
    idempotent=True,
    side_effects="read",
    cache_ttl_seconds=3600,
    handler=_web_fetch,
)


# ---------------------------------------------------------------------------
# Tool: web.search
# ---------------------------------------------------------------------------


def _snippet_from_content(content: str, length: int = 200) -> str:
    """Return the first ``length`` chars of ``content``, single-line."""
    flat = " ".join(content.split())
    return flat[:length]


async def _web_search(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    """Return canned search results matching a topic.

    The result list has up to ``k`` entries (default 5). Match logic:

    * exact case-insensitive match on a known topic key;
    * substring match in either direction;
    * fallback to the renewable-energy fixtures, which keeps the demo
      meaningful for arbitrary user queries.
    """
    query = args.get("query")
    if not isinstance(query, str) or not query.strip():
        raise ToolError("INVALID_ARGUMENTS", "query is required and must be a non-empty string")

    k_raw = args.get("k", 5)
    if not isinstance(k_raw, int) or isinstance(k_raw, bool) or k_raw <= 0:
        raise ToolError("INVALID_ARGUMENTS", "k must be a positive integer")
    k = min(k_raw, 5)  # we only have 5 sources per topic

    sources = fixtures.get_fixture_sources(query)
    selected = sources[:k]

    results = []
    for i, src in enumerate(selected, start=1):
        title = src.get("title") or f"Source {i}"
        results.append(
            {
                "title": f"Source {i}: {title}",
                "url": src["url"],
                "snippet": _snippet_from_content(src.get("content", src.get("snippet", ""))),
            }
        )
    return {"results": results}


WEB_SEARCH = Tool(
    tool_id="web.search",
    name="Search the web",
    description="Mock web search returning canned results from fixtures keyed off the query topic.",
    input_schema={
        "type": "object",
        "required": ["query"],
        "properties": {
            "query": {"type": "string"},
            "k": {"type": "integer", "minimum": 1, "default": 5},
        },
        "additionalProperties": False,
    },
    output_schema={
        "type": "object",
        "required": ["results"],
        "properties": {
            "results": {
                "type": "array",
                "items": {
                    "type": "object",
                    "required": ["title", "url", "snippet"],
                    "properties": {
                        "title": {"type": "string"},
                        "url": {"type": "string"},
                        "snippet": {"type": "string"},
                    },
                },
            },
        },
    },
    idempotent=True,
    side_effects="read",
    cache_ttl_seconds=3600,
    handler=_web_search,
)


# ---------------------------------------------------------------------------
# Tool: fs.read
# ---------------------------------------------------------------------------


async def _fs_read(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    """Read a UTF-8 text file relative to the fixtures root."""
    path = args.get("path")
    if not isinstance(path, str) or not path:
        raise ToolError("INVALID_ARGUMENTS", "path is required and must be a string")

    target = safe_path(ctx.fixtures_dir, path)
    if not target.exists() or not target.is_file():
        raise ToolError(
            code="FILE_NOT_FOUND",
            message=f"file not found: {path}",
            status_code=404,
            details={"path": path},
        )
    data = await asyncio.to_thread(target.read_bytes)
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ToolError(
            code="INVALID_ARGUMENTS",
            message="file is not valid UTF-8",
            details={"path": path},
        ) from exc
    return {"content": text, "size": len(data)}


FS_READ = Tool(
    tool_id="fs.read",
    name="Read a file",
    description="Read a text file from the mock fixtures directory (sandboxed; path traversal blocked).",
    input_schema={
        "type": "object",
        "required": ["path"],
        "properties": {"path": {"type": "string"}},
        "additionalProperties": False,
    },
    output_schema={
        "type": "object",
        "required": ["content", "size"],
        "properties": {
            "content": {"type": "string"},
            "size": {"type": "integer"},
        },
    },
    idempotent=True,
    side_effects="read",
    cache_ttl_seconds=60,
    handler=_fs_read,
)


# ---------------------------------------------------------------------------
# Tool: fs.write
# ---------------------------------------------------------------------------


async def _fs_write(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    """Write a UTF-8 string to a path under the fixtures root."""
    path = args.get("path")
    content = args.get("content")
    if not isinstance(path, str) or not path:
        raise ToolError("INVALID_ARGUMENTS", "path is required and must be a string")
    if not isinstance(content, str):
        raise ToolError("INVALID_ARGUMENTS", "content is required and must be a string")

    target = safe_path(ctx.fixtures_dir, path)
    target.parent.mkdir(parents=True, exist_ok=True)
    encoded = content.encode("utf-8")
    await asyncio.to_thread(target.write_bytes, encoded)
    return {"path": str(target.relative_to(ctx.fixtures_dir.resolve())), "bytes_written": len(encoded)}


FS_WRITE = Tool(
    tool_id="fs.write",
    name="Write a file",
    description="Write a text file inside the mock fixtures directory (sandboxed; path traversal blocked).",
    input_schema={
        "type": "object",
        "required": ["path", "content"],
        "properties": {
            "path": {"type": "string"},
            "content": {"type": "string"},
        },
        "additionalProperties": False,
    },
    output_schema={
        "type": "object",
        "required": ["path", "bytes_written"],
        "properties": {
            "path": {"type": "string"},
            "bytes_written": {"type": "integer"},
        },
    },
    idempotent=False,
    side_effects="write",
    cache_ttl_seconds=None,
    handler=_fs_write,
)


# ---------------------------------------------------------------------------
# Tool: notes.add / notes.list
# ---------------------------------------------------------------------------


async def _notes_add(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    """Append a note to the in-memory list."""
    title = args.get("title")
    body = args.get("body")
    if not isinstance(title, str) or not title:
        raise ToolError("INVALID_ARGUMENTS", "title is required and must be a non-empty string")
    if not isinstance(body, str):
        raise ToolError("INVALID_ARGUMENTS", "body is required and must be a string")

    note_id = f"note_{ULID()}"
    created_at = datetime.now(timezone.utc).isoformat()
    note = {
        "id": note_id,
        "title": title,
        "body": body,
        "created_at": created_at,
    }
    ctx.notes.append(note)
    return {"id": note_id, "created_at": created_at}


NOTES_ADD = Tool(
    tool_id="notes.add",
    name="Add a note",
    description="Append a note to the in-memory note store (per-process; cleared on restart).",
    input_schema={
        "type": "object",
        "required": ["title", "body"],
        "properties": {
            "title": {"type": "string"},
            "body": {"type": "string"},
        },
        "additionalProperties": False,
    },
    output_schema={
        "type": "object",
        "required": ["id", "created_at"],
        "properties": {
            "id": {"type": "string"},
            "created_at": {"type": "string"},
        },
    },
    idempotent=False,
    side_effects="write",
    cache_ttl_seconds=None,
    handler=_notes_add,
)


async def _notes_list(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    """Return all notes in the in-memory list."""
    # Return a shallow copy so the caller can't mutate our state.
    return {"notes": [dict(n) for n in ctx.notes]}


NOTES_LIST = Tool(
    tool_id="notes.list",
    name="List notes",
    description="Return all notes from the in-memory note store.",
    input_schema={
        "type": "object",
        "properties": {},
        "additionalProperties": False,
    },
    output_schema={
        "type": "object",
        "required": ["notes"],
        "properties": {
            "notes": {
                "type": "array",
                "items": {
                    "type": "object",
                    "required": ["id", "title", "body", "created_at"],
                    "properties": {
                        "id": {"type": "string"},
                        "title": {"type": "string"},
                        "body": {"type": "string"},
                        "created_at": {"type": "string"},
                    },
                },
            },
        },
    },
    idempotent=True,
    side_effects="read",
    cache_ttl_seconds=None,
    handler=_notes_list,
)


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------


TOOL_LIST: list[Tool] = [
    WEB_FETCH,
    WEB_SEARCH,
    FS_READ,
    FS_WRITE,
    NOTES_ADD,
    NOTES_LIST,
]


TOOL_REGISTRY: dict[str, Tool] = {tool.tool_id: tool for tool in TOOL_LIST}
