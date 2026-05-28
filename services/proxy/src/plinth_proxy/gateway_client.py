# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 The Plynf Authors
"""MCP-Gateway-backed connector registry.

In demo / offline mode the proxy uses ``make_mock_registry`` (file-based
fixtures). In production it uses :class:`GatewayConnectorRegistry`, which
POSTs to the existing ``services/gateway`` (``POST /v1/invoke``).

The gateway handles:
  - OAuth token resolution and refresh
  - Tool-level rate limits and cost caps
  - Per-tenant quotas
  - Audit logging
  - Cache + idempotency

Plynf-proxy layers on top:
  - Response shaping (policy engine)
  - Token counting / savings measurement
  - Tool-call interception inside the LLM round-trip

This split keeps the gateway responsible for tool *execution* and the proxy
responsible for tool *response shape*. No double-billing of tools, no shared
state, just two HTTP hops.
"""

from __future__ import annotations

import logging
from typing import Any

import httpx

from .connectors import TOOL_TO_CONNECTOR, ConnectorCall, ConnectorRegistry

log = logging.getLogger("plinth.proxy.gateway")


class GatewayInvocationError(RuntimeError):
    """Raised when the gateway returns a non-2xx for a tool invocation."""

    def __init__(self, status: int, body: str) -> None:
        super().__init__(f"gateway returned {status}: {body[:300]}")
        self.status = status
        self.body = body


class GatewayClient:
    """Thin async client around POST /v1/invoke.

    Keeps a single :class:`httpx.AsyncClient` for connection reuse. Passes the
    caller's bearer token through so the gateway's tenant-scoping and audit
    log attribute the call to the right tenant.
    """

    def __init__(
        self,
        base_url: str,
        *,
        timeout_s: float = 30.0,
        default_auth_header: str | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self._timeout = timeout_s
        self._default_auth = default_auth_header

    async def invoke(
        self,
        tool_id: str,
        arguments: dict[str, Any],
        *,
        agent_id: str | None = None,
        workspace_id: str | None = None,
        auth_header: str | None = None,
    ) -> Any:
        """Return the raw ``result`` field from the gateway's response."""
        url = f"{self.base_url}/v1/invoke"
        payload = {
            "tool_id": tool_id,
            "arguments": arguments,
            "agent_id": agent_id,
            "workspace_id": workspace_id,
            "cache": True,
        }
        headers: dict[str, str] = {"Content-Type": "application/json"}
        token = auth_header or self._default_auth
        if token:
            headers["Authorization"] = token

        async with httpx.AsyncClient(timeout=self._timeout) as client:
            resp = await client.post(url, json=payload, headers=headers)

        if resp.status_code >= 400:
            log.warning(
                "gateway invoke failed: tool_id=%s status=%d body=%s",
                tool_id,
                resp.status_code,
                resp.text[:200],
            )
            raise GatewayInvocationError(resp.status_code, resp.text)

        body = resp.json()
        return body.get("result")


def make_gateway_registry(
    client: GatewayClient,
    *,
    auth_header_provider: callable | None = None,
) -> ConnectorRegistry:
    """Build a :class:`ConnectorRegistry` that delegates to the MCP gateway.

    ``auth_header_provider`` is an optional callable returning the bearer
    token to forward to the gateway. The proxy's request-scoped auth wiring
    plugs into this so each tool invocation runs under the *caller's* tenant
    context, not the proxy's service account.
    """
    registry = ConnectorRegistry()

    async def _async_handler(call: ConnectorCall) -> Any:
        auth = auth_header_provider() if auth_header_provider is not None else None
        return await client.invoke(call.tool, call.args, auth_header=auth)

    # ConnectorRegistry.execute is sync today; wrap the async coroutine in a
    # sync shim that the proxy's async handler awaits via asyncio.
    # We register the coroutine factory directly — the proxy detects async
    # handlers and awaits them.
    for connector in set(TOOL_TO_CONNECTOR.values()):
        registry.register(connector, _async_handler)  # type: ignore[arg-type]
    return registry


__all__ = [
    "GatewayClient",
    "GatewayInvocationError",
    "make_gateway_registry",
]
