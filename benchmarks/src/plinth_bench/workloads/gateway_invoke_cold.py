# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 The Plinth Authors
"""Gateway /v1/invoke workload — cache-miss dominated.

Each request varies the arguments so the gateway never returns from
cache. Backend traffic is sent to mock-mcp's ``web.fetch`` (which has a
``mock://`` fixture so it stays offline). This measures the proxy + auth +
audit + DB-write hot path, not the cache.
"""

from __future__ import annotations

import asyncio
import secrets
import time
from collections.abc import Awaitable, Callable

import httpx

from ..runner import RequestSample


def _tool_payload() -> dict:
    return {
        "tool_id": f"bench.cold.{secrets.token_hex(4)}",
        "name": "Bench Cold Tool",
        "description": "Non-cached fetch for cache-miss benchmarking.",
        "transport": "http",
        "endpoint": "http://localhost:7423/invoke/web.fetch",
        "input_schema": {
            "type": "object",
            "properties": {"url": {"type": "string"}},
        },
        "output_schema": {"type": "object"},
        "idempotent": False,           # short-circuits cache eligibility
        "side_effects": "read",
        "cache_ttl_seconds": None,
        "auth_method": "none",
        "auth_config": {},
    }


def build() -> Callable[..., Awaitable[RequestSample]]:
    state: dict[str, str] = {}
    counter = {"i": 0}
    setup_lock = asyncio.Lock()

    async def ensure_tool(client: httpx.AsyncClient) -> str:
        payload = _tool_payload()
        resp = await client.post("/v1/tools/register", json=payload)
        resp.raise_for_status()
        return payload["tool_id"]

    async def workload(client: httpx.AsyncClient) -> RequestSample:
        if "tool_id" not in state:
            async with setup_lock:
                if "tool_id" not in state:
                    state["tool_id"] = await ensure_tool(client)
        tool_id = state["tool_id"]
        i = counter["i"]
        counter["i"] = i + 1

        t0 = time.perf_counter()
        # Cycle a small set of valid fixture URLs so each request can be
        # served offline. ``cache=False`` plus ``idempotent=False`` on the
        # tool means every call goes through the proxy regardless of the
        # arguments hash — this is the cache-miss hot path we want to
        # measure.
        fixture_urls = [
            "mock://renewable-energy-1",
            "mock://renewable-energy-2",
            "mock://ai-agents-1",
            "mock://climate-policy-1",
            "mock://climate-policy-2",
        ]
        url = fixture_urls[i % len(fixture_urls)]
        resp = await client.post(
            "/v1/invoke",
            json={
                "tool_id": tool_id,
                "arguments": {"url": url},
                "cache": False,
            },
        )
        dur = (time.perf_counter() - t0) * 1000.0
        return RequestSample(
            duration_ms=dur,
            status_code=resp.status_code,
            error=None if resp.status_code < 400 else f"http_{resp.status_code}",
        )

    return workload
