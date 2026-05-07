# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 The Plinth Authors
"""Gateway /v1/invoke workload — cache-hit dominated.

Pre-registers an idempotent tool with a long TTL, then invokes it with
the SAME arguments on every request. The first request misses; every
subsequent one is a cache hit. Hits should be sub-millisecond + zero
backend traffic — this measures the gateway's hot path.

Note: the gateway runs an agent-id rate-limiter by default. We pass
``agent_id=None`` so anonymous calls bypass enforcement, mirroring how
the bench harness is intended to be run with
``PLINTH_RATE_LIMITS_ENABLED=false`` set on the gateway anyway.
"""

from __future__ import annotations

import asyncio
import secrets
import time
from collections.abc import Awaitable, Callable

import httpx

from ..runner import RequestSample


def _tool_payload() -> dict:
    """A tool that points at the mock-mcp's web.fetch fixture."""

    return {
        "tool_id": f"bench.cached.{secrets.token_hex(4)}",
        "name": "Bench Cached Tool",
        "description": "Idempotent fetch for cache-hit benchmarking.",
        "transport": "http",
        "endpoint": "http://localhost:7423/invoke/web.fetch",
        "input_schema": {
            "type": "object",
            "properties": {"url": {"type": "string"}},
        },
        "output_schema": {"type": "object"},
        "idempotent": True,
        "side_effects": "read",
        "cache_ttl_seconds": 3600,  # long → reads stay cached
        "auth_method": "none",
        "auth_config": {},
    }


def build() -> Callable[..., Awaitable[RequestSample]]:
    state: dict[str, str] = {}
    setup_lock = asyncio.Lock()

    async def ensure_tool(client: httpx.AsyncClient) -> str:
        payload = _tool_payload()
        resp = await client.post("/v1/tools/register", json=payload)
        if resp.status_code >= 400:
            resp.raise_for_status()
        return payload["tool_id"]

    async def workload(client: httpx.AsyncClient) -> RequestSample:
        if "tool_id" not in state:
            async with setup_lock:
                if "tool_id" not in state:
                    state["tool_id"] = await ensure_tool(client)
        tool_id = state["tool_id"]

        t0 = time.perf_counter()
        resp = await client.post(
            "/v1/invoke",
            json={
                "tool_id": tool_id,
                # Use one of the bundled mock-mcp fixture URLs so the
                # backend call resolves offline.
                "arguments": {"url": "mock://renewable-energy-1"},
                "cache": True,
            },
        )
        dur = (time.perf_counter() - t0) * 1000.0
        return RequestSample(
            duration_ms=dur,
            status_code=resp.status_code,
            error=None if resp.status_code < 400 else f"http_{resp.status_code}",
        )

    return workload
