# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 The Plinth Authors
"""Workspace KV PUT/GET workload.

Pre-creates one workspace, then alternates PUT and GET on a small set of
keys (so we hit the version-history code path and a steady working set).
"""

from __future__ import annotations

import asyncio
import secrets
import time
from collections.abc import Awaitable, Callable

import httpx

from ..runner import RequestSample

# A small per-process pool keeps the SQL engine warm without ballooning
# the working set in the workspace store.
KEY_POOL = [f"k{i:02d}" for i in range(16)]


async def _setup(client: httpx.AsyncClient) -> str:
    """Create a fresh workspace + seed every key so GETs can succeed."""

    resp = await client.post(
        "/v1/workspaces",
        json={"name": f"bench-{secrets.token_hex(4)}"},
    )
    resp.raise_for_status()
    ws_id = resp.json()["id"]
    # Seed each key once so the GET path doesn't 404 in steady-state.
    for k in KEY_POOL:
        seed = await client.put(
            f"/v1/workspaces/{ws_id}/kv/{k}",
            json={"value": {"seeded": True}},
        )
        seed.raise_for_status()
    return ws_id


def build() -> Callable[..., Awaitable[RequestSample]]:
    """Return a workload coroutine factory.

    On the first invocation we lazily create + seed a workspace; each
    subsequent call alternates PUT and GET against keys that exist.
    """

    state: dict[str, str] = {}
    counter = {"i": 0}
    setup_lock = asyncio.Lock()

    async def workload(client: httpx.AsyncClient) -> RequestSample:
        if "ws_id" not in state:
            # Guard against the runner dispatching N concurrent setup
            # calls during the first second of the ramp. Lock-and-check
            # keeps it to a single seed pass.
            async with setup_lock:
                if "ws_id" not in state:
                    state["ws_id"] = await _setup(client)
        ws_id = state["ws_id"]
        i = counter["i"]
        counter["i"] = i + 1
        key = KEY_POOL[i % len(KEY_POOL)]
        do_put = (i % 2) == 0

        t0 = time.perf_counter()
        if do_put:
            resp = await client.put(
                f"/v1/workspaces/{ws_id}/kv/{key}",
                json={"value": {"counter": i}},
            )
        else:
            resp = await client.get(f"/v1/workspaces/{ws_id}/kv/{key}")
        dur = (time.perf_counter() - t0) * 1000.0
        return RequestSample(
            duration_ms=dur,
            status_code=resp.status_code,
            error=None if resp.status_code < 400 else f"http_{resp.status_code}",
        )

    return workload
