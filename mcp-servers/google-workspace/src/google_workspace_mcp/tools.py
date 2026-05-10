# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 The Plinth Authors
"""Tool implementations for the Google Workspace MCP server.

Each tool is a small wrapper around a Google Workspace API endpoint. Every
tool reads the bearer token from the :class:`ToolContext` passed by the route
handler; the route handler in turn lifts it off the inbound ``Authorization``
header.

Implementation notes:

* **Drive** uses ``https://www.googleapis.com/drive/v3``.
* **Docs** uses ``https://docs.googleapis.com/v1`` (creation + structured edits).
* **Sheets** uses ``https://sheets.googleapis.com/v4``.
* **Calendar** uses ``https://www.googleapis.com/calendar/v3``.
* **Gmail** uses ``https://gmail.googleapis.com/gmail/v1`` and the
  ``format=metadata`` request shape so we never pull message bodies.
* Google Docs/Sheets exports go through Drive's ``files.export`` endpoint.
* IDs are validated as printable ASCII without path traversal characters.
"""

from __future__ import annotations

import base64
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
    def __init__(self, message: str = "missing or invalid Google access token") -> None:
        super().__init__("UNAUTHORIZED", message, status_code=401)


# ---------------------------------------------------------------------------
# Context
# ---------------------------------------------------------------------------


@dataclass
class ToolContext:
    """Per-request state injected into tool invocations."""

    access_token: str | None
    http_client: httpx.AsyncClient
    drive_base_url: str = "https://www.googleapis.com"
    docs_base_url: str = "https://docs.googleapis.com"
    sheets_base_url: str = "https://sheets.googleapis.com"
    gmail_base_url: str = "https://gmail.googleapis.com"
    calendar_base_url: str = "https://www.googleapis.com"
    timeout: float = 15.0

    def require_token(self) -> str:
        if not self.access_token:
            raise Unauthorized()
        return self.access_token

    def headers(self, *, json_body: bool = False) -> dict[str, str]:
        headers = {
            "Authorization": f"Bearer {self.require_token()}",
            "Accept": "application/json",
            "User-Agent": "plinth-google-workspace-mcp/1.1.0",
        }
        if json_body:
            headers["Content-Type"] = "application/json; charset=utf-8"
        return headers

    def drive_url(self, path: str) -> str:
        return f"{self.drive_base_url.rstrip('/')}{_lead(path)}"

    def docs_url(self, path: str) -> str:
        return f"{self.docs_base_url.rstrip('/')}{_lead(path)}"

    def sheets_url(self, path: str) -> str:
        return f"{self.sheets_base_url.rstrip('/')}{_lead(path)}"

    def gmail_url(self, path: str) -> str:
        return f"{self.gmail_base_url.rstrip('/')}{_lead(path)}"

    def calendar_url(self, path: str) -> str:
        return f"{self.calendar_base_url.rstrip('/')}{_lead(path)}"


def _lead(path: str) -> str:
    return path if path.startswith("/") else "/" + path


# ---------------------------------------------------------------------------
# ID + range validation
# ---------------------------------------------------------------------------


# Google Drive/Docs/Sheets file IDs are URL-safe base64-ish (alnum, -, _).
_GOOGLE_ID_RE = re.compile(r"^[A-Za-z0-9_-]{8,128}$")
# Calendar IDs may be email-like (primary, ``foo@group.calendar.google.com``).
_CAL_ID_RE = re.compile(r"^[A-Za-z0-9_.@-]{1,256}$")
# Gmail label IDs: alnum + underscore + dash, plus the well-known uppercase
# system labels (INBOX, SENT, etc.).
_LABEL_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


def parse_file_id(value: Any, *, name: str = "file_id") -> str:
    """Validate ``value`` as a Google file ID.

    Raises:
        ToolError: On any non-conforming value.
    """
    if not isinstance(value, str) or not value:
        raise ToolError("INVALID_ARGUMENTS", f"{name} is required and must be a string")
    if value.startswith("/") or value.startswith("\\"):
        raise ToolError(
            "INVALID_ARGUMENTS",
            f"{name} must not be an absolute path",
            details={name: value},
        )
    if ".." in value:
        raise ToolError(
            "INVALID_ARGUMENTS",
            f"{name} must not contain traversal segments",
            details={name: value},
        )
    if not _GOOGLE_ID_RE.match(value):
        raise ToolError(
            "INVALID_ARGUMENTS",
            f"{name} must be a Google file ID (alnum, '-', '_')",
            details={name: value},
        )
    return value


def parse_calendar_id(value: Any) -> str:
    """Validate a Google Calendar id (often ``primary`` or an email)."""
    if not isinstance(value, str) or not value:
        raise ToolError("INVALID_ARGUMENTS", "calendar_id must be a non-empty string")
    if value.startswith("/") or ".." in value:
        raise ToolError("INVALID_ARGUMENTS", "calendar_id must not contain path traversal")
    if not _CAL_ID_RE.match(value):
        raise ToolError(
            "INVALID_ARGUMENTS",
            "calendar_id contains disallowed characters",
            details={"calendar_id": value},
        )
    return value


def parse_label_id(value: Any) -> str:
    if not isinstance(value, str) or not value:
        raise ToolError("INVALID_ARGUMENTS", "label_id must be a non-empty string")
    if not _LABEL_ID_RE.match(value):
        raise ToolError(
            "INVALID_ARGUMENTS",
            "label_id contains disallowed characters",
            details={"label_id": value},
        )
    return value


# ---------------------------------------------------------------------------
# Tool record
# ---------------------------------------------------------------------------


ToolHandler = Callable[[dict[str, Any], ToolContext], Awaitable[dict[str, Any]]]


@dataclass(frozen=True)
class Tool:
    """Static description of a tool."""

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
            "auth_config": {"provider": "google"},
        }


# ---------------------------------------------------------------------------
# HTTP helpers
# ---------------------------------------------------------------------------


async def _do_get(
    ctx: ToolContext,
    url: str,
    *,
    params: dict[str, Any] | None = None,
    json_response: bool = True,
) -> Any:
    try:
        resp = await ctx.http_client.get(
            url,
            params=params,
            headers=ctx.headers(),
            timeout=ctx.timeout,
        )
    except httpx.HTTPError as exc:
        raise ToolError(
            "TOOL_INVOCATION_FAILED",
            f"Google request failed: {exc}",
            status_code=502,
            details={"url": url},
        ) from exc
    return _decode_response(resp, url=url, json_response=json_response)


async def _do_post(
    ctx: ToolContext, url: str, *, body: dict[str, Any] | None = None
) -> Any:
    try:
        resp = await ctx.http_client.post(
            url,
            json=body if body is not None else {},
            headers=ctx.headers(json_body=True),
            timeout=ctx.timeout,
        )
    except httpx.HTTPError as exc:
        raise ToolError(
            "TOOL_INVOCATION_FAILED",
            f"Google request failed: {exc}",
            status_code=502,
            details={"url": url},
        ) from exc
    return _decode_response(resp, url=url, json_response=True)


def _decode_response(
    resp: httpx.Response, *, url: str, json_response: bool = True
) -> Any:
    if resp.status_code == 401:
        raise Unauthorized("Google rejected the access token (401)")
    if resp.status_code == 404:
        raise ToolError(
            "TOOL_INVOCATION_FAILED",
            "Google resource not found",
            status_code=404,
            details={"url": url, "status_code": 404},
        )
    if resp.status_code >= 400:
        body_preview = resp.text[:300]
        raise ToolError(
            "TOOL_INVOCATION_FAILED",
            f"Google returned HTTP {resp.status_code}",
            status_code=502,
            details={"url": url, "status_code": resp.status_code, "body_preview": body_preview},
        )
    if not json_response:
        return resp.text
    try:
        return resp.json()
    except ValueError as exc:
        raise ToolError(
            "TOOL_INVOCATION_FAILED",
            f"Google returned non-JSON: {exc}",
            status_code=502,
            details={"url": url},
        ) from exc


# ---------------------------------------------------------------------------
# Tool: google.drive_search
# ---------------------------------------------------------------------------


async def _drive_search(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    query = args.get("query")
    if not isinstance(query, str) or not query.strip():
        raise ToolError("INVALID_ARGUMENTS", "query is required")
    page_size = args.get("page_size", 20)
    if not isinstance(page_size, int) or isinstance(page_size, bool) or page_size < 1 or page_size > 100:
        raise ToolError("INVALID_ARGUMENTS", "page_size must be an integer 1..100")

    # Drive's ``q`` syntax expects a fully-formed expression. Callers pass the
    # raw ``q`` string; we don't try to wrap them in ``name contains '...'``
    # because Drive's filter language is rich (e.g. ``mimeType=...``).
    params = {
        "q": query.strip(),
        "pageSize": page_size,
        "fields": "files(id,name,mimeType,webViewLink,modifiedTime),nextPageToken",
    }
    raw = await _do_get(ctx, ctx.drive_url("/drive/v3/files"), params=params)
    if not isinstance(raw, dict):
        raise ToolError("TOOL_INVOCATION_FAILED", "Google returned non-object", status_code=502)
    files = raw.get("files") or []
    out = [
        {
            "id": f.get("id"),
            "name": f.get("name"),
            "mimeType": f.get("mimeType"),
            "webViewLink": f.get("webViewLink"),
            "modifiedTime": f.get("modifiedTime"),
        }
        for f in files
        if isinstance(f, dict)
    ]
    return {"files": out, "count": len(out), "nextPageToken": raw.get("nextPageToken")}


DRIVE_SEARCH = Tool(
    tool_id="google.drive_search",
    name="Search Drive files",
    description="Search Drive files using the Drive ``q`` query syntax.",
    input_schema={
        "type": "object",
        "required": ["query"],
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
    handler=_drive_search,
)


# ---------------------------------------------------------------------------
# Tool: google.drive_read
# ---------------------------------------------------------------------------


# Default export mime types per Google native format.
_DEFAULT_EXPORT_FOR: dict[str, str] = {
    "application/vnd.google-apps.document": "text/plain",
    "application/vnd.google-apps.spreadsheet": "text/csv",
    "application/vnd.google-apps.presentation": "text/plain",
}


async def _drive_read(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    file_id = parse_file_id(args.get("file_id"))
    explicit_mime = args.get("mime_type")
    if explicit_mime is not None and not isinstance(explicit_mime, str):
        raise ToolError("INVALID_ARGUMENTS", "mime_type must be a string")

    # First fetch metadata so we know the file's name + mime type.
    meta = await _do_get(
        ctx,
        ctx.drive_url(f"/drive/v3/files/{file_id}"),
        params={"fields": "id,name,mimeType,modifiedTime"},
    )
    if not isinstance(meta, dict):
        raise ToolError("TOOL_INVOCATION_FAILED", "Google returned non-object", status_code=502)
    native_mime = meta.get("mimeType") or ""

    if native_mime.startswith("application/vnd.google-apps."):
        # Use the export endpoint for native Google Docs/Sheets/Slides.
        export_mime = explicit_mime or _DEFAULT_EXPORT_FOR.get(native_mime, "text/plain")
        text = await _do_get(
            ctx,
            ctx.drive_url(f"/drive/v3/files/{file_id}/export"),
            params={"mimeType": export_mime},
            json_response=False,
        )
        return {
            "content": text,
            "mimeType": export_mime,
            "name": meta.get("name"),
            "id": meta.get("id"),
        }
    # Non-native files: fetch raw bytes via ``alt=media``.
    text = await _do_get(
        ctx,
        ctx.drive_url(f"/drive/v3/files/{file_id}"),
        params={"alt": "media"},
        json_response=False,
    )
    return {
        "content": text,
        "mimeType": native_mime,
        "name": meta.get("name"),
        "id": meta.get("id"),
    }


DRIVE_READ = Tool(
    tool_id="google.drive_read",
    name="Read a Drive file",
    description="Read a file's content (export Docs/Sheets, raw bytes for others).",
    input_schema={
        "type": "object",
        "required": ["file_id"],
        "properties": {
            "file_id": {"type": "string"},
            "mime_type": {"type": "string", "description": "Override export mime"},
        },
        "additionalProperties": False,
    },
    output_schema={"type": "object"},
    idempotent=True,
    side_effects="read",
    cache_ttl_seconds=30,
    handler=_drive_read,
)


# ---------------------------------------------------------------------------
# Tool: google.docs_create
# ---------------------------------------------------------------------------


async def _docs_create(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    title = args.get("title")
    if not isinstance(title, str) or not title.strip():
        raise ToolError("INVALID_ARGUMENTS", "title is required")
    content = args.get("content")
    if content is not None and not isinstance(content, str):
        raise ToolError("INVALID_ARGUMENTS", "content must be a string")

    raw = await _do_post(
        ctx, ctx.docs_url("/v1/documents"), body={"title": title.strip()}
    )
    if not isinstance(raw, dict):
        raise ToolError("TOOL_INVOCATION_FAILED", "Google returned non-object", status_code=502)
    doc_id = raw.get("documentId")
    if not isinstance(doc_id, str):
        raise ToolError(
            "TOOL_INVOCATION_FAILED",
            "Google returned no documentId",
            status_code=502,
            details={"keys": sorted(raw.keys())},
        )

    # Optionally append the initial body.
    if isinstance(content, str) and content:
        await _do_post(
            ctx,
            ctx.docs_url(f"/v1/documents/{doc_id}:batchUpdate"),
            body={
                "requests": [
                    {
                        "insertText": {
                            "location": {"index": 1},
                            "text": content,
                        }
                    }
                ]
            },
        )
    return {
        "document_id": doc_id,
        "url": f"https://docs.google.com/document/d/{doc_id}/edit",
    }


DOCS_CREATE = Tool(
    tool_id="google.docs_create",
    name="Create a Google Doc",
    description="Create a new Google Doc, optionally inserting initial content.",
    input_schema={
        "type": "object",
        "required": ["title"],
        "properties": {
            "title": {"type": "string"},
            "content": {"type": "string"},
        },
        "additionalProperties": False,
    },
    output_schema={"type": "object"},
    idempotent=False,
    side_effects="write",
    cache_ttl_seconds=None,
    handler=_docs_create,
)


# ---------------------------------------------------------------------------
# Tool: google.docs_append
# ---------------------------------------------------------------------------


async def _docs_append(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    doc_id = parse_file_id(args.get("document_id"), name="document_id")
    content = args.get("content")
    if not isinstance(content, str) or not content:
        raise ToolError("INVALID_ARGUMENTS", "content is required and must be a non-empty string")

    # First fetch the doc to find the "end" index of the body — Google requires
    # an explicit insertion location.
    doc = await _do_get(ctx, ctx.docs_url(f"/v1/documents/{doc_id}"))
    if not isinstance(doc, dict):
        raise ToolError("TOOL_INVOCATION_FAILED", "Google returned non-object", status_code=502)
    body = doc.get("body") or {}
    elements = (body.get("content") or []) if isinstance(body, dict) else []
    end_index = 1
    if isinstance(elements, list):
        for el in elements:
            if isinstance(el, dict) and isinstance(el.get("endIndex"), int):
                end_index = max(end_index, el["endIndex"])
    # Insertion index is end_index - 1 (the trailing newline must remain last).
    insert_at = max(1, end_index - 1)

    raw = await _do_post(
        ctx,
        ctx.docs_url(f"/v1/documents/{doc_id}:batchUpdate"),
        body={
            "requests": [
                {
                    "insertText": {
                        "location": {"index": insert_at},
                        "text": content,
                    }
                }
            ]
        },
    )
    if not isinstance(raw, dict):
        raise ToolError("TOOL_INVOCATION_FAILED", "Google returned non-object", status_code=502)
    return {
        "document_id": doc_id,
        "updated_at": raw.get("writeControl", {}).get("requiredRevisionId")
        or raw.get("documentId"),
    }


DOCS_APPEND = Tool(
    tool_id="google.docs_append",
    name="Append text to a Google Doc",
    description="Append plain text to the end of a Google Doc.",
    input_schema={
        "type": "object",
        "required": ["document_id", "content"],
        "properties": {
            "document_id": {"type": "string"},
            "content": {"type": "string"},
        },
        "additionalProperties": False,
    },
    output_schema={"type": "object"},
    idempotent=False,
    side_effects="write",
    cache_ttl_seconds=None,
    handler=_docs_append,
)


# ---------------------------------------------------------------------------
# Tool: google.sheets_read
# ---------------------------------------------------------------------------


async def _sheets_read(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    spreadsheet_id = parse_file_id(args.get("spreadsheet_id"), name="spreadsheet_id")
    range_a1 = args.get("range")
    if not isinstance(range_a1, str) or not range_a1.strip():
        raise ToolError("INVALID_ARGUMENTS", "range is required")
    if ".." in range_a1 or "/" in range_a1:
        raise ToolError(
            "INVALID_ARGUMENTS",
            "range must be an A1 notation string (e.g. 'Sheet1!A1:B10')",
            details={"range": range_a1},
        )

    raw = await _do_get(
        ctx,
        ctx.sheets_url(f"/v4/spreadsheets/{spreadsheet_id}/values/{range_a1}"),
    )
    if not isinstance(raw, dict):
        raise ToolError("TOOL_INVOCATION_FAILED", "Google returned non-object", status_code=502)
    values = raw.get("values") or []
    return {
        "values": values if isinstance(values, list) else [],
        "range": raw.get("range") or range_a1,
        "majorDimension": raw.get("majorDimension"),
    }


SHEETS_READ = Tool(
    tool_id="google.sheets_read",
    name="Read a Sheets range",
    description="Read a range from a Google Sheet (A1 notation).",
    input_schema={
        "type": "object",
        "required": ["spreadsheet_id", "range"],
        "properties": {
            "spreadsheet_id": {"type": "string"},
            "range": {"type": "string"},
        },
        "additionalProperties": False,
    },
    output_schema={"type": "object"},
    idempotent=True,
    side_effects="read",
    cache_ttl_seconds=30,
    handler=_sheets_read,
)


# ---------------------------------------------------------------------------
# Tool: google.sheets_append_row
# ---------------------------------------------------------------------------


async def _sheets_append_row(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    spreadsheet_id = parse_file_id(args.get("spreadsheet_id"), name="spreadsheet_id")
    range_a1 = args.get("range")
    if not isinstance(range_a1, str) or not range_a1.strip():
        raise ToolError("INVALID_ARGUMENTS", "range is required")
    values = args.get("values")
    if not isinstance(values, list) or not values:
        raise ToolError(
            "INVALID_ARGUMENTS",
            "values is required and must be a non-empty list of cell values",
        )
    # Flatten / coerce to strings — Sheets accepts mixed types but agents
    # typically pass strings.
    row = [v if isinstance(v, (str, int, float, bool)) or v is None else str(v) for v in values]

    url = ctx.sheets_url(
        f"/v4/spreadsheets/{spreadsheet_id}/values/{range_a1}:append"
    )
    try:
        resp = await ctx.http_client.post(
            url,
            json={"values": [row]},
            headers=ctx.headers(json_body=True),
            params={"valueInputOption": "USER_ENTERED"},
            timeout=ctx.timeout,
        )
    except httpx.HTTPError as exc:
        raise ToolError(
            "TOOL_INVOCATION_FAILED",
            f"Google request failed: {exc}",
            status_code=502,
            details={"url": url},
        ) from exc
    raw = _decode_response(resp, url=url, json_response=True)
    if not isinstance(raw, dict):
        raise ToolError("TOOL_INVOCATION_FAILED", "Google returned non-object", status_code=502)
    updates = raw.get("updates") or {}
    return {"updates": updates if isinstance(updates, dict) else {}}


SHEETS_APPEND_ROW = Tool(
    tool_id="google.sheets_append_row",
    name="Append a row to a Sheet",
    description="Append a single row of values to a Google Sheet.",
    input_schema={
        "type": "object",
        "required": ["spreadsheet_id", "range", "values"],
        "properties": {
            "spreadsheet_id": {"type": "string"},
            "range": {"type": "string"},
            "values": {"type": "array"},
        },
        "additionalProperties": False,
    },
    output_schema={"type": "object"},
    idempotent=False,
    side_effects="write",
    cache_ttl_seconds=None,
    handler=_sheets_append_row,
)


# ---------------------------------------------------------------------------
# Tool: google.calendar_list_events
# ---------------------------------------------------------------------------


def _slim_event(raw: dict[str, Any]) -> dict[str, Any]:
    attendees = raw.get("attendees") or []
    if isinstance(attendees, list):
        att_out = [
            {
                "email": a.get("email"),
                "displayName": a.get("displayName"),
                "responseStatus": a.get("responseStatus"),
            }
            for a in attendees
            if isinstance(a, dict)
        ]
    else:
        att_out = []
    return {
        "id": raw.get("id"),
        "summary": raw.get("summary"),
        "description": raw.get("description"),
        "start": raw.get("start"),
        "end": raw.get("end"),
        "attendees": att_out,
        "htmlLink": raw.get("htmlLink"),
        "status": raw.get("status"),
    }


async def _calendar_list_events(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    calendar_id = parse_calendar_id(args.get("calendar_id", "primary"))
    max_results = args.get("max_results", 10)
    if not isinstance(max_results, int) or isinstance(max_results, bool) or max_results < 1 or max_results > 250:
        raise ToolError("INVALID_ARGUMENTS", "max_results must be an integer 1..250")

    params: dict[str, Any] = {
        "maxResults": max_results,
        "singleEvents": "true",
        "orderBy": "startTime",
    }
    if isinstance(args.get("time_min"), str) and args["time_min"]:
        params["timeMin"] = args["time_min"]
    if isinstance(args.get("time_max"), str) and args["time_max"]:
        params["timeMax"] = args["time_max"]

    raw = await _do_get(
        ctx,
        ctx.calendar_url(f"/calendar/v3/calendars/{calendar_id}/events"),
        params=params,
    )
    if not isinstance(raw, dict):
        raise ToolError("TOOL_INVOCATION_FAILED", "Google returned non-object", status_code=502)
    items = raw.get("items") or []
    events = [
        _slim_event(item) for item in items if isinstance(item, dict)
    ]
    return {"events": events, "count": len(events), "nextPageToken": raw.get("nextPageToken")}


CALENDAR_LIST_EVENTS = Tool(
    tool_id="google.calendar_list_events",
    name="List Calendar events",
    description="List upcoming events on a calendar (default: 'primary').",
    input_schema={
        "type": "object",
        "properties": {
            "calendar_id": {"type": "string", "default": "primary"},
            "max_results": {"type": "integer", "minimum": 1, "maximum": 250, "default": 10},
            "time_min": {"type": "string", "description": "RFC3339 lower bound"},
            "time_max": {"type": "string", "description": "RFC3339 upper bound"},
        },
        "additionalProperties": False,
    },
    output_schema={"type": "object"},
    idempotent=True,
    side_effects="read",
    cache_ttl_seconds=30,
    handler=_calendar_list_events,
)


# ---------------------------------------------------------------------------
# Tool: google.gmail_list_messages
# ---------------------------------------------------------------------------


def _decode_subject(headers: list[Any]) -> tuple[str | None, str | None, str | None]:
    """Pluck Subject/From/Date out of the message header array."""
    subject = sender = date = None
    if not isinstance(headers, list):
        return subject, sender, date
    for h in headers:
        if not isinstance(h, dict):
            continue
        name = (h.get("name") or "").lower()
        value = h.get("value")
        if name == "subject" and isinstance(value, str):
            subject = value
        elif name == "from" and isinstance(value, str):
            sender = value
        elif name == "date" and isinstance(value, str):
            date = value
    return subject, sender, date


async def _gmail_list_messages(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    label_ids = args.get("label_ids", ["INBOX"])
    if not isinstance(label_ids, list) or not label_ids:
        raise ToolError("INVALID_ARGUMENTS", "label_ids must be a non-empty list of strings")
    label_ids = [parse_label_id(label) for label in label_ids]

    max_results = args.get("max_results", 10)
    if not isinstance(max_results, int) or isinstance(max_results, bool) or max_results < 1 or max_results > 50:
        raise ToolError("INVALID_ARGUMENTS", "max_results must be an integer 1..50")

    params: dict[str, Any] = {"maxResults": max_results}
    # Gmail's labelIds expects repeated ``labelIds=<id>`` query params; httpx
    # serialises a list value that way.
    params["labelIds"] = label_ids
    if isinstance(args.get("query"), str) and args["query"].strip():
        params["q"] = args["query"].strip()

    listing = await _do_get(
        ctx,
        ctx.gmail_url("/gmail/v1/users/me/messages"),
        params=params,
    )
    if not isinstance(listing, dict):
        raise ToolError("TOOL_INVOCATION_FAILED", "Google returned non-object", status_code=502)
    raw_messages = listing.get("messages") or []
    if not isinstance(raw_messages, list):
        raw_messages = []

    out: list[dict[str, Any]] = []
    for entry in raw_messages:
        if not isinstance(entry, dict):
            continue
        msg_id = entry.get("id")
        if not isinstance(msg_id, str):
            continue
        # Fetch only header metadata — never body text for privacy/cost.
        msg = await _do_get(
            ctx,
            ctx.gmail_url(f"/gmail/v1/users/me/messages/{msg_id}"),
            params={
                "format": "metadata",
                "metadataHeaders": ["Subject", "From", "Date"],
            },
        )
        if not isinstance(msg, dict):
            continue
        payload = msg.get("payload") or {}
        headers = payload.get("headers") if isinstance(payload, dict) else []
        subject, sender, date = _decode_subject(headers if isinstance(headers, list) else [])
        out.append(
            {
                "id": msg.get("id"),
                "threadId": msg.get("threadId"),
                "snippet": msg.get("snippet"),
                "subject": subject,
                "from": sender,
                "date": date,
                "labelIds": msg.get("labelIds") or [],
            }
        )
    return {
        "messages": out,
        "count": len(out),
        "nextPageToken": listing.get("nextPageToken"),
    }


GMAIL_LIST_MESSAGES = Tool(
    tool_id="google.gmail_list_messages",
    name="List Gmail messages",
    description=(
        "List recent Gmail messages with header summary (subject/from/date). "
        "Body is intentionally not fetched."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "label_ids": {
                "type": "array",
                "items": {"type": "string"},
                "default": ["INBOX"],
            },
            "query": {"type": "string", "description": "Optional Gmail search query"},
            "max_results": {"type": "integer", "minimum": 1, "maximum": 50, "default": 10},
        },
        "additionalProperties": False,
    },
    output_schema={"type": "object"},
    idempotent=True,
    side_effects="read",
    cache_ttl_seconds=30,
    handler=_gmail_list_messages,
)


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------


TOOL_LIST: list[Tool] = [
    DRIVE_SEARCH,
    DRIVE_READ,
    DOCS_CREATE,
    DOCS_APPEND,
    SHEETS_READ,
    SHEETS_APPEND_ROW,
    CALENDAR_LIST_EVENTS,
    GMAIL_LIST_MESSAGES,
]


TOOL_REGISTRY: dict[str, Tool] = {tool.tool_id: tool for tool in TOOL_LIST}


# ``base64`` is imported for parity with future body-decoding helpers; keep
# the import alive so Ruff doesn't complain.
_ = base64
