# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 The Plinth Authors
"""Tool implementations for the Salesforce MCP server.

Salesforce's REST API lives under each org's ``instance_url`` (e.g.
``https://acme.my.salesforce.com``). The Plinth gateway captures that URL
at OAuth callback time (it's in the token-exchange response body), persists
it in ``connection.metadata.instance_url``, and forwards it on every proxied
invoke as ``X-Plinth-OAuth-InstanceUrl``. This module reads it off the
inbound request and uses it as the per-call API base, layered with
``/services/data/{api_version}/...``.

Standard responses:
* ``GET .../sobjects/{Type}/{Id}`` returns the full record
* ``POST .../sobjects/{Type}`` returns ``{id, success, errors}``
* ``PATCH .../sobjects/{Type}/{Id}`` returns 204
* ``DELETE .../sobjects/{Type}/{Id}`` returns 204
* ``GET .../query/?q=...`` returns ``{totalSize, done, records}``
* ``GET .../sobjects`` / ``.../sobjects/{Type}/describe`` returns the schema
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Literal
from urllib.parse import urlparse

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
    def __init__(self, message: str = "missing or invalid Salesforce access token") -> None:
        super().__init__("UNAUTHORIZED", message, status_code=401)


class InstanceUrlMissing(ToolError):
    def __init__(self) -> None:
        super().__init__(
            "SALESFORCE_INSTANCE_URL_MISSING",
            "X-Plinth-OAuth-InstanceUrl header is required (gateway must inject it)",
            status_code=400,
        )


# ---------------------------------------------------------------------------
# Context
# ---------------------------------------------------------------------------


# Allow only HTTPS hosts on common Salesforce domains. Tests can extend by
# overriding the ``allowed_host_suffixes`` set on :class:`ToolContext`.
_DEFAULT_ALLOWED_HOST_SUFFIXES: tuple[str, ...] = (
    ".salesforce.com",
    ".force.com",
    ".my.salesforce.com",
    ".cloudforce.com",
    ".salesforce.test",  # for tests
)


@dataclass
class ToolContext:
    """Per-request state injected into tool invocations."""

    access_token: str | None
    instance_url: str | None
    http_client: httpx.AsyncClient
    api_version: str = "v60.0"
    timeout: float = 15.0
    allowed_host_suffixes: tuple[str, ...] = _DEFAULT_ALLOWED_HOST_SUFFIXES

    def require_token(self) -> str:
        if not self.access_token:
            raise Unauthorized()
        return self.access_token

    def require_instance_url(self) -> str:
        raw = (self.instance_url or "").strip()
        if not raw:
            raise InstanceUrlMissing()
        # Validate the instance URL: must be HTTPS and host must end in one of
        # the allowed Salesforce domains. This stops a malicious header from
        # redirecting the call to an attacker-controlled host.
        try:
            parsed = urlparse(raw)
        except ValueError as exc:
            raise ToolError(
                "SALESFORCE_INSTANCE_URL_INVALID",
                f"instance_url is malformed: {exc}",
                status_code=400,
            ) from exc
        if parsed.scheme != "https":
            raise ToolError(
                "SALESFORCE_INSTANCE_URL_INVALID",
                "instance_url must be https",
                status_code=400,
                details={"scheme": parsed.scheme},
            )
        host = (parsed.hostname or "").lower()
        if not any(host.endswith(s) for s in self.allowed_host_suffixes):
            raise ToolError(
                "SALESFORCE_INSTANCE_URL_INVALID",
                "instance_url host is not a known Salesforce domain",
                status_code=400,
                details={"host": host},
            )
        return raw.rstrip("/")

    def headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self.require_token()}",
            "Accept": "application/json",
            "Content-Type": "application/json",
            "User-Agent": "plinth-salesforce-mcp/1.5.0",
        }

    def url(self, path: str) -> str:
        if not path.startswith("/"):
            path = "/" + path
        return f"{self.require_instance_url()}/services/data/{self.api_version}{path}"


# ---------------------------------------------------------------------------
# Validation helpers
# ---------------------------------------------------------------------------


# Salesforce SObject type names: alphanumeric + ``__c`` for custom objects.
_SOBJECT_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]*$")
# Salesforce IDs are 15- or 18-char alphanumerics.
_SF_ID_RE = re.compile(r"^[A-Za-z0-9]{15}([A-Za-z0-9]{3})?$")


def parse_sobject_type(value: Any, *, name: str = "object_type") -> str:
    if not isinstance(value, str) or not value.strip():
        raise ToolError("INVALID_ARGUMENTS", f"{name} is required")
    s = value.strip()
    if "/" in s or "\\" in s or ".." in s:
        raise ToolError(
            "INVALID_ARGUMENTS",
            f"{name} must be a Salesforce SObject type",
            details={name: value},
        )
    if not _SOBJECT_RE.match(s):
        raise ToolError(
            "INVALID_ARGUMENTS",
            f"{name} must look like 'Lead' or 'My_Custom__c'",
            details={name: value},
        )
    return s


def parse_record_id(value: Any, *, name: str = "record_id") -> str:
    if not isinstance(value, str) or not value:
        raise ToolError("INVALID_ARGUMENTS", f"{name} is required")
    s = value.strip()
    if "/" in s or "\\" in s or ".." in s:
        raise ToolError(
            "INVALID_ARGUMENTS",
            f"{name} must be a Salesforce record id",
            details={name: value},
        )
    if not _SF_ID_RE.match(s):
        raise ToolError(
            "INVALID_ARGUMENTS",
            f"{name} must be a 15- or 18-char alphanumeric record id",
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
            "auth_config": {"provider": "salesforce"},
        }


# ---------------------------------------------------------------------------
# HTTP helpers
# ---------------------------------------------------------------------------


async def _do(method: str, ctx: ToolContext, url: str, *, params=None, body=None) -> Any:
    try:
        resp = await ctx.http_client.request(
            method,
            url,
            params=params,
            json=body if body is not None else None,
            headers=ctx.headers(),
            timeout=ctx.timeout,
        )
    except httpx.HTTPError as exc:
        raise ToolError(
            "TOOL_INVOCATION_FAILED",
            f"Salesforce request failed: {exc}",
            status_code=502,
            details={"url": url},
        ) from exc
    return _decode_response(resp, url=url)


def _decode_response(resp: httpx.Response, *, url: str) -> Any:
    if resp.status_code == 401:
        raise Unauthorized("Salesforce rejected the access token (401)")
    if resp.status_code == 403:
        raise ToolError(
            "TOOL_INVOCATION_FAILED",
            "Salesforce permission denied (403)",
            status_code=403,
            details={"url": url, "status_code": 403},
        )
    if resp.status_code == 404:
        raise ToolError(
            "TOOL_INVOCATION_FAILED",
            "Salesforce resource not found",
            status_code=404,
            details={"url": url, "status_code": 404},
        )
    if resp.status_code == 204:
        return None
    if resp.status_code >= 400:
        body_preview = resp.text[:300]
        # Salesforce returns an array of error objects on 400 — surface them.
        try:
            errs = resp.json()
        except ValueError:
            errs = None
        details: dict[str, Any] = {
            "url": url,
            "status_code": resp.status_code,
            "body_preview": body_preview,
        }
        if isinstance(errs, list):
            details["errors"] = errs
        raise ToolError(
            "TOOL_INVOCATION_FAILED",
            f"Salesforce returned HTTP {resp.status_code}",
            status_code=502,
            details=details,
        )
    if not resp.text:
        return None
    try:
        return resp.json()
    except ValueError as exc:
        raise ToolError(
            "TOOL_INVOCATION_FAILED",
            f"Salesforce returned non-JSON: {exc}",
            status_code=502,
            details={"url": url},
        ) from exc


# ---------------------------------------------------------------------------
# Tool: salesforce.soql_query
# ---------------------------------------------------------------------------


async def _soql_query(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    soql = args.get("soql")
    if not isinstance(soql, str) or not soql.strip():
        raise ToolError("INVALID_ARGUMENTS", "soql is required and must be a non-empty string")
    raw = await _do("GET", ctx, ctx.url("/query"), params={"q": soql})
    if not isinstance(raw, dict):
        raise ToolError(
            "TOOL_INVOCATION_FAILED",
            "Salesforce returned non-object",
            status_code=502,
        )
    records = raw.get("records") or []
    return {
        "records": records if isinstance(records, list) else [],
        "total_size": int(raw.get("totalSize") or 0),
        "done": bool(raw.get("done", True)),
        "next_records_url": raw.get("nextRecordsUrl"),
    }


SOQL_QUERY = Tool(
    tool_id="salesforce.soql_query",
    name="Run a SOQL query",
    description="Execute a SOQL query and return the records.",
    input_schema={
        "type": "object",
        "required": ["soql"],
        "properties": {
            "soql": {"type": "string"},
        },
        "additionalProperties": False,
    },
    output_schema={"type": "object"},
    idempotent=True,
    side_effects="read",
    cache_ttl_seconds=15,
    handler=_soql_query,
)


# ---------------------------------------------------------------------------
# Tool: salesforce.get_record
# ---------------------------------------------------------------------------


async def _get_record(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    object_type = parse_sobject_type(args.get("object_type"), name="object_type")
    record_id = parse_record_id(args.get("record_id"), name="record_id")
    params: dict[str, Any] = {}
    if "fields" in args:
        fields = args.get("fields")
        if not isinstance(fields, list) or not all(isinstance(f, str) and f for f in fields):
            raise ToolError("INVALID_ARGUMENTS", "fields must be a list of strings")
        if fields:
            params["fields"] = ",".join(fields)
    raw = await _do(
        "GET",
        ctx,
        ctx.url(f"/sobjects/{object_type}/{record_id}"),
        params=params or None,
    )
    if not isinstance(raw, dict):
        raise ToolError(
            "TOOL_INVOCATION_FAILED",
            "Salesforce returned non-object",
            status_code=502,
        )
    return raw


GET_RECORD = Tool(
    tool_id="salesforce.get_record",
    name="Get a Salesforce record",
    description="Fetch a single record by SObject type + id.",
    input_schema={
        "type": "object",
        "required": ["object_type", "record_id"],
        "properties": {
            "object_type": {"type": "string"},
            "record_id": {"type": "string"},
            "fields": {"type": "array", "items": {"type": "string"}},
        },
        "additionalProperties": False,
    },
    output_schema={"type": "object"},
    idempotent=True,
    side_effects="read",
    cache_ttl_seconds=15,
    handler=_get_record,
)


# ---------------------------------------------------------------------------
# Tool: salesforce.create_record
# ---------------------------------------------------------------------------


async def _create_record(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    object_type = parse_sobject_type(args.get("object_type"), name="object_type")
    fields = args.get("fields")
    if not isinstance(fields, dict) or not fields:
        raise ToolError("INVALID_ARGUMENTS", "fields must be a non-empty object")
    raw = await _do("POST", ctx, ctx.url(f"/sobjects/{object_type}"), body=fields)
    if not isinstance(raw, dict):
        raise ToolError(
            "TOOL_INVOCATION_FAILED",
            "Salesforce returned non-object",
            status_code=502,
        )
    return {
        "id": raw.get("id"),
        "success": bool(raw.get("success", True)),
        "errors": raw.get("errors") or [],
        "object_type": object_type,
    }


CREATE_RECORD = Tool(
    tool_id="salesforce.create_record",
    name="Create a Salesforce record",
    description="Create a record on the given SObject (Lead, Contact, Opportunity, ...).",
    input_schema={
        "type": "object",
        "required": ["object_type", "fields"],
        "properties": {
            "object_type": {"type": "string"},
            "fields": {"type": "object"},
        },
        "additionalProperties": False,
    },
    output_schema={"type": "object"},
    idempotent=False,
    side_effects="write",
    cache_ttl_seconds=None,
    handler=_create_record,
)


# ---------------------------------------------------------------------------
# Tool: salesforce.update_record
# ---------------------------------------------------------------------------


async def _update_record(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    object_type = parse_sobject_type(args.get("object_type"), name="object_type")
    record_id = parse_record_id(args.get("record_id"), name="record_id")
    fields = args.get("fields")
    if not isinstance(fields, dict) or not fields:
        raise ToolError("INVALID_ARGUMENTS", "fields must be a non-empty object")
    await _do(
        "PATCH",
        ctx,
        ctx.url(f"/sobjects/{object_type}/{record_id}"),
        body=fields,
    )
    return {
        "id": record_id,
        "object_type": object_type,
        "updated": True,
    }


UPDATE_RECORD = Tool(
    tool_id="salesforce.update_record",
    name="Update a Salesforce record",
    description="PATCH fields onto an existing record.",
    input_schema={
        "type": "object",
        "required": ["object_type", "record_id", "fields"],
        "properties": {
            "object_type": {"type": "string"},
            "record_id": {"type": "string"},
            "fields": {"type": "object"},
        },
        "additionalProperties": False,
    },
    output_schema={"type": "object"},
    idempotent=False,
    side_effects="write",
    cache_ttl_seconds=None,
    handler=_update_record,
)


# ---------------------------------------------------------------------------
# Tool: salesforce.delete_record
# ---------------------------------------------------------------------------


async def _delete_record(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    object_type = parse_sobject_type(args.get("object_type"), name="object_type")
    record_id = parse_record_id(args.get("record_id"), name="record_id")
    await _do("DELETE", ctx, ctx.url(f"/sobjects/{object_type}/{record_id}"))
    return {
        "id": record_id,
        "object_type": object_type,
        "deleted": True,
    }


DELETE_RECORD = Tool(
    tool_id="salesforce.delete_record",
    name="Delete a Salesforce record",
    description="Delete a record by SObject type + id.",
    input_schema={
        "type": "object",
        "required": ["object_type", "record_id"],
        "properties": {
            "object_type": {"type": "string"},
            "record_id": {"type": "string"},
        },
        "additionalProperties": False,
    },
    output_schema={"type": "object"},
    idempotent=False,
    side_effects="write",
    cache_ttl_seconds=None,
    handler=_delete_record,
)


# ---------------------------------------------------------------------------
# Tool: salesforce.list_objects
# ---------------------------------------------------------------------------


async def _list_objects(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    raw = await _do("GET", ctx, ctx.url("/sobjects"))
    if not isinstance(raw, dict):
        raise ToolError(
            "TOOL_INVOCATION_FAILED",
            "Salesforce returned non-object",
            status_code=502,
        )
    items = raw.get("sobjects") or []
    objects: list[dict[str, Any]] = []
    if isinstance(items, list):
        for item in items:
            if not isinstance(item, dict):
                continue
            objects.append(
                {
                    "name": item.get("name"),
                    "label": item.get("label"),
                    "label_plural": item.get("labelPlural"),
                    "custom": bool(item.get("custom", False)),
                    "createable": bool(item.get("createable", False)),
                    "updateable": bool(item.get("updateable", False)),
                    "deletable": bool(item.get("deletable", False)),
                    "queryable": bool(item.get("queryable", True)),
                }
            )
    return {"objects": objects, "count": len(objects)}


LIST_OBJECTS = Tool(
    tool_id="salesforce.list_objects",
    name="List Salesforce objects",
    description="List all SObject types accessible to the connected user.",
    input_schema={
        "type": "object",
        "properties": {},
        "additionalProperties": False,
    },
    output_schema={"type": "object"},
    idempotent=True,
    side_effects="read",
    cache_ttl_seconds=300,
    handler=_list_objects,
)


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------


TOOL_LIST: list[Tool] = [
    SOQL_QUERY,
    GET_RECORD,
    CREATE_RECORD,
    UPDATE_RECORD,
    DELETE_RECORD,
    LIST_OBJECTS,
]


TOOL_REGISTRY: dict[str, Tool] = {tool.tool_id: tool for tool in TOOL_LIST}
