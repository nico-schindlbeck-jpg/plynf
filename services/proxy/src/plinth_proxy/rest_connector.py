# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 The Plynf Authors
"""Generic REST connector — register arbitrary HTTP APIs as Plynf tools.

Lets a Pro/Enterprise operator point Plynf at any internal or third-party REST
API *without writing an MCP server*. Each tool maps to exactly one HTTP request:
the LLM-supplied arguments fill a URL-path template and/or the query string /
JSON body, and the raw JSON response then flows through the same
policy → shaping → savings pipeline as the built-in connectors. This is the
implementation behind the Pro tier's advertised ``allow_custom_rest_connectors``.

Security — an operator-defined ``base_url`` is *untrusted egress*, so every
request is run through an SSRF guard (:func:`assert_url_allowed`) that rejects
private / loopback / link-local / reserved / multicast addresses — including the
cloud metadata endpoint ``169.254.169.254`` — unless the spec explicitly opts in
with ``allow_private_hosts: true`` (the on-prem case where the target genuinely
*is* an internal host). Non-``http(s)`` schemes are always rejected and the
response body is capped at ``max_response_bytes``.

  .. note:: The guard resolves and validates the host before connecting; it does
     not yet pin the resolved IP, so a determined DNS-rebinding attacker could in
     principle race it. Pinning is a planned hardening. ``allow_private_hosts``
     should therefore only be enabled for hosts the operator controls.

Config shape (e.g. ``PLINTH_PROXY_REST_CONNECTORS`` as a JSON array)::

    [
      {
        "name": "inventory",
        "base_url": "https://api.acme.internal",
        "headers": {"Authorization": "Bearer ${INVENTORY_TOKEN}"},
        "timeout_s": 15,
        "endpoints": [
          {"tool": "get_sku",     "method": "GET",  "path": "/v1/skus/{sku_id}"},
          {"tool": "search_skus", "method": "GET",  "path": "/v1/skus", "query": ["q", "limit"]},
          {"tool": "create_note", "method": "POST", "path": "/v1/notes", "body": ["text"]}
        ]
      }
    ]

``${VAR}`` inside header values is expanded from the process environment so
secrets stay out of the config blob.
"""

from __future__ import annotations

import ipaddress
import json
import os
import re
import socket
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlsplit

import httpx

from .connectors import ConnectorCall

_PATH_PARAM_RE = re.compile(r"\{(\w+)\}")
_ENV_REF_RE = re.compile(r"\$\{(\w+)\}")
_BODY_METHODS = {"POST", "PUT", "PATCH"}


class RestConnectorError(ValueError):
    """Raised when a spec is malformed or a request cannot be built."""


class SSRFError(RestConnectorError):
    """Raised when a target URL resolves to a disallowed (non-public) address."""


# ---------------------------------------------------------------------------
# Spec model
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RestEndpoint:
    """One tool → one HTTP request mapping.

    ``query`` / ``body`` name the argument keys routed to the query string /
    JSON body. When omitted, routing is inferred from the method: GET/DELETE/
    HEAD send remaining args as query params, body methods send them as JSON.
    Path-template params (``{name}``) are always consumed by the URL first.
    """

    tool: str
    method: str = "GET"
    path: str = "/"
    query: tuple[str, ...] | None = None
    body: tuple[str, ...] | None = None


@dataclass(frozen=True)
class RestConnectorSpec:
    name: str
    base_url: str
    endpoints: tuple[RestEndpoint, ...] = ()
    headers: Mapping[str, str] = field(default_factory=dict)
    timeout_s: float = 30.0
    allow_private_hosts: bool = False
    max_response_bytes: int = 1_000_000


# ---------------------------------------------------------------------------
# Config parsing
# ---------------------------------------------------------------------------


def _expand_env(value: str) -> str:
    """Replace ``${VAR}`` with ``os.environ['VAR']`` (empty string if unset)."""
    return _ENV_REF_RE.sub(lambda m: os.environ.get(m.group(1), ""), value)


def spec_from_dict(d: Mapping[str, Any]) -> RestConnectorSpec:
    """Parse a single connector spec from a plain dict (JSON-shaped)."""
    if not d.get("name"):
        raise RestConnectorError("REST connector spec missing 'name'")
    if not d.get("base_url"):
        raise RestConnectorError(f"REST connector {d.get('name')!r} missing 'base_url'")

    endpoints: list[RestEndpoint] = []
    for e in d.get("endpoints") or []:
        if not e.get("tool"):
            raise RestConnectorError(
                f"REST connector {d['name']!r} has an endpoint without a 'tool' name"
            )
        endpoints.append(
            RestEndpoint(
                tool=str(e["tool"]),
                method=str(e.get("method", "GET")).upper(),
                path=str(e.get("path", "/")),
                query=tuple(e["query"]) if e.get("query") is not None else None,
                body=tuple(e["body"]) if e.get("body") is not None else None,
            )
        )

    headers = {
        str(k): _expand_env(str(v)) for k, v in (d.get("headers") or {}).items()
    }
    return RestConnectorSpec(
        name=str(d["name"]),
        base_url=str(d["base_url"]),
        endpoints=tuple(endpoints),
        headers=headers,
        timeout_s=float(d.get("timeout_s", 30.0)),
        allow_private_hosts=bool(d.get("allow_private_hosts", False)),
        max_response_bytes=int(d.get("max_response_bytes", 1_000_000)),
    )


def specs_from_json(raw: str) -> list[RestConnectorSpec]:
    """Parse a JSON array (or single object) of connector specs."""
    data = json.loads(raw)
    if isinstance(data, dict):
        data = [data]
    if not isinstance(data, list):
        raise RestConnectorError("REST connector config must be a JSON array or object")
    return [spec_from_dict(d) for d in data]


# ---------------------------------------------------------------------------
# SSRF guard
# ---------------------------------------------------------------------------


def _is_disallowed_ip(ip: str) -> bool:
    """True if ``ip`` falls in a range we must never let an LLM tool reach."""
    addr = ipaddress.ip_address(ip)
    return (
        addr.is_private
        or addr.is_loopback
        or addr.is_link_local  # incl. 169.254.169.254 cloud-metadata
        or addr.is_reserved
        or addr.is_multicast
        or addr.is_unspecified
    )


def assert_url_allowed(url: str, allow_private: bool = False) -> None:
    """Reject non-public targets unless explicitly opted in.

    Raises :class:`SSRFError` for non-http(s) schemes, unresolvable hosts, or
    any host that resolves (wholly or partly) to a non-public address.
    """
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https"):
        raise SSRFError(f"scheme not allowed: {parts.scheme or '(none)'!r}")
    host = parts.hostname
    if not host:
        raise SSRFError("URL has no host")
    if allow_private:
        return

    port = parts.port or (443 if parts.scheme == "https" else 80)
    try:
        infos = socket.getaddrinfo(host, port, proto=socket.IPPROTO_TCP)
    except socket.gaierror as e:  # pragma: no cover - network-dependent
        raise SSRFError(f"cannot resolve host: {host}") from e

    # Block if ANY resolved address is non-public (defeats a hostname that
    # resolves to one public and one private record).
    for info in infos:
        ip = info[4][0]
        if _is_disallowed_ip(ip):
            raise SSRFError(f"host {host} resolves to a non-public address: {ip}")


# ---------------------------------------------------------------------------
# Request building + dispatch
# ---------------------------------------------------------------------------


def render_request(
    spec: RestConnectorSpec, ep: RestEndpoint, args: Mapping[str, Any]
) -> tuple[str, dict[str, Any], dict[str, Any]]:
    """Return ``(url, query_params, json_body)`` for a tool call.

    Pure and side-effect-free so it can be unit-tested without egress.
    """
    args = dict(args or {})

    path_params = set(_PATH_PARAM_RE.findall(ep.path))
    missing = [p for p in path_params if p not in args]
    if missing:
        raise RestConnectorError(
            f"tool {ep.tool!r} missing required path param(s): {', '.join(sorted(missing))}"
        )
    path = ep.path.format(**{k: args[k] for k in path_params})
    url = spec.base_url.rstrip("/") + "/" + path.lstrip("/")

    remaining = {k: v for k, v in args.items() if k not in path_params}
    method = ep.method.upper()

    if ep.query is not None:
        query = {k: remaining[k] for k in ep.query if k in remaining}
    elif method not in _BODY_METHODS:
        query = dict(remaining)
    else:
        query = {}

    if ep.body is not None:
        body = {k: remaining[k] for k in ep.body if k in remaining}
    elif method in _BODY_METHODS:
        # Default body = everything not already routed to the query string.
        body = {k: v for k, v in remaining.items() if k not in query}
    else:
        body = {}

    return url, query, body


def build_rest_connector(
    spec: RestConnectorSpec,
) -> tuple[dict[str, str], Callable[[ConnectorCall], Awaitable[Any]]]:
    """Compile a spec into ``(tool→connector map, async handler)``.

    The handler is what :class:`~plinth_proxy.connectors.ConnectorRegistry`
    dispatches to; it builds the request, runs the SSRF guard, performs the
    HTTP call, and returns parsed JSON (or a small text wrapper for non-JSON
    bodies). Exceptions propagate to the proxy, which turns them into an error
    tool message rather than failing the whole completion.
    """
    by_tool = {ep.tool: ep for ep in spec.endpoints}
    tool_to_connector = {ep.tool: spec.name for ep in spec.endpoints}

    async def handler(call: ConnectorCall) -> Any:
        ep = by_tool.get(call.tool)
        if ep is None:
            raise RestConnectorError(
                f"connector {spec.name!r} has no endpoint for tool {call.tool!r}"
            )
        url, query, body = render_request(spec, ep, call.args)
        assert_url_allowed(url, spec.allow_private_hosts)

        headers = dict(spec.headers) or None
        async with httpx.AsyncClient(timeout=spec.timeout_s) as client:
            resp = await client.request(
                ep.method,
                url,
                params=query or None,
                json=body or None,
                headers=headers,
            )

        raw = resp.content[: spec.max_response_bytes]
        try:
            parsed = json.loads(raw)
        except (json.JSONDecodeError, UnicodeDecodeError):
            return {
                "_http_status": resp.status_code,
                "body": raw.decode("utf-8", errors="replace"),
            }
        # Surface a non-2xx status alongside the parsed body so the agent (and
        # the savings pipeline) can see what came back.
        if resp.status_code >= 400 and isinstance(parsed, dict):
            parsed = {"_http_status": resp.status_code, **parsed}
        return parsed

    return tool_to_connector, handler


__all__ = [
    "RestConnectorError",
    "RestConnectorSpec",
    "RestEndpoint",
    "SSRFError",
    "assert_url_allowed",
    "build_rest_connector",
    "render_request",
    "spec_from_dict",
    "specs_from_json",
]
