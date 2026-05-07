# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 The Plinth Authors
"""Workspace files PUT/GET workload.

Writes ~4 KB blobs (representative of agent intermediate artefacts) and
reads them back, exercising the blob store + content-addressed paths.
"""

from __future__ import annotations

import asyncio
import secrets
import time
from collections.abc import Awaitable, Callable

import httpx

from ..runner import RequestSample

PATH_POOL = [f"notes/note-{i:02d}.md" for i in range(8)]
BLOB = b"# bench artefact\n" + (b"abcdef0123456789\n" * 250)  # ~4 KB


async def _setup(client: httpx.AsyncClient) -> str:
    resp = await client.post(
        "/v1/workspaces",
        json={"name": f"bench-files-{secrets.token_hex(4)}"},
    )
    resp.raise_for_status()
    ws_id = resp.json()["id"]
    # Seed each path so meta GETs in steady-state don't 404.
    for path in PATH_POOL:
        seed = await client.put(
            f"/v1/workspaces/{ws_id}/files/{path}",
            content=BLOB,
            headers={"content-type": "text/markdown"},
        )
        seed.raise_for_status()
    return ws_id


def build() -> Callable[..., Awaitable[RequestSample]]:
    state: dict[str, str] = {}
    counter = {"i": 0}
    setup_lock = asyncio.Lock()

    async def workload(client: httpx.AsyncClient) -> RequestSample:
        if "ws_id" not in state:
            async with setup_lock:
                if "ws_id" not in state:
                    state["ws_id"] = await _setup(client)
        ws_id = state["ws_id"]
        i = counter["i"]
        counter["i"] = i + 1
        path = PATH_POOL[i % len(PATH_POOL)]
        do_put = (i % 2) == 0

        t0 = time.perf_counter()
        if do_put:
            resp = await client.put(
                f"/v1/workspaces/{ws_id}/files/{path}",
                content=BLOB,
                headers={"content-type": "text/markdown"},
            )
        else:
            resp = await client.get(f"/v1/workspaces/{ws_id}/files/{path}/meta")
        dur = (time.perf_counter() - t0) * 1000.0
        return RequestSample(
            duration_ms=dur,
            status_code=resp.status_code,
            error=None if resp.status_code < 400 else f"http_{resp.status_code}",
        )

    return workload
