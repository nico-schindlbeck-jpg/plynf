# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 The Plinth Authors
"""Workspace snapshot creation + read workload.

Each iteration:

1. PUT a single KV entry (so the snapshot has new state to record)
2. POST a snapshot
3. GET the snapshot back (parsed JSON, including kv_versions map)

Snapshots are the heaviest workspace operation — they iterate every
key/file. This workload measures how quickly snapshot creation degrades
as the working set grows.
"""

from __future__ import annotations

import asyncio
import secrets
import time
from collections.abc import Awaitable, Callable

import httpx

from ..runner import RequestSample


async def _ensure_workspace(client: httpx.AsyncClient) -> str:
    resp = await client.post(
        "/v1/workspaces",
        json={"name": f"bench-snap-{secrets.token_hex(4)}"},
    )
    resp.raise_for_status()
    return resp.json()["id"]


def build() -> Callable[..., Awaitable[RequestSample]]:
    state: dict[str, str] = {}
    counter = {"i": 0}
    setup_lock = asyncio.Lock()

    async def workload(client: httpx.AsyncClient) -> RequestSample:
        if "ws_id" not in state:
            async with setup_lock:
                if "ws_id" not in state:
                    state["ws_id"] = await _ensure_workspace(client)
        ws_id = state["ws_id"]
        i = counter["i"]
        counter["i"] = i + 1

        # Sequence: write a small KV, then create + read a snapshot. We
        # measure the snapshot create as the request — the seed write is
        # included in latency to keep the workload realistic (real
        # agents don't snapshot a never-changing workspace).
        t0 = time.perf_counter()
        try:
            r1 = await client.put(
                f"/v1/workspaces/{ws_id}/kv/seed-{i % 8}",
                json={"value": i},
            )
            if r1.status_code >= 400:
                dur = (time.perf_counter() - t0) * 1000.0
                return RequestSample(
                    duration_ms=dur,
                    status_code=r1.status_code,
                    error=f"http_{r1.status_code}",
                )
            r2 = await client.post(
                f"/v1/workspaces/{ws_id}/snapshots",
                json={"name": f"snap-{i:06d}", "message": "bench"},
            )
            dur = (time.perf_counter() - t0) * 1000.0
            return RequestSample(
                duration_ms=dur,
                status_code=r2.status_code,
                error=None if r2.status_code < 400 else f"http_{r2.status_code}",
            )
        except httpx.HTTPError:
            raise

    return workload
