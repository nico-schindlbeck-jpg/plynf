# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 The Plinth Authors
"""Identity token-issue workload.

Each request hits ``POST /v1/tokens`` to mint a fresh capability token.
This measures HMAC/RSA signing throughput on the identity service.
"""

from __future__ import annotations

import secrets
import time
from collections.abc import Awaitable, Callable

import httpx

from ..runner import RequestSample


def build() -> Callable[..., Awaitable[RequestSample]]:
    counter = {"i": 0}

    async def workload(client: httpx.AsyncClient) -> RequestSample:
        i = counter["i"]
        counter["i"] = i + 1

        body = {
            "agent_id": f"bench-agent-{secrets.token_hex(2)}",
            "tenant_id": "default",
            "scopes": ["tool:web.fetch:read", f"workspace:ws_{i:06d}:write"],
            "ttl_seconds": 3600,
        }
        t0 = time.perf_counter()
        resp = await client.post("/v1/tokens", json=body)
        dur = (time.perf_counter() - t0) * 1000.0
        return RequestSample(
            duration_ms=dur,
            status_code=resp.status_code,
            error=None if resp.status_code < 400 else f"http_{resp.status_code}",
        )

    return workload
