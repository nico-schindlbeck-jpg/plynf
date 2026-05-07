# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 The Plinth Authors
"""Load-shedding middleware for the workspace service.

Tracks inflight requests + a bounded queue. When both are saturated, the
middleware short-circuits with a 503 + ``Retry-After`` response so the
service degrades gracefully under overload instead of cascading.

The shedder is opt-in via :class:`Settings` (default disabled) so v0.4
deployments are unaffected. ``/healthz`` is always allowed regardless of
shed state — it's the LB's signal.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from fastapi import Request
from fastapi.responses import JSONResponse


class OverloadedError(Exception):
    """Raised when the load-shedder cannot accept a new request."""

    def __init__(self, retry_after: int = 1) -> None:
        super().__init__("service overloaded")
        self.retry_after = retry_after


class LoadShedder:
    """Per-process inflight + queue tracker.

    The contract:

    * ``acquire()`` is an async context manager that admits a request,
      tracking it as either "inflight" (under the inflight cap) or
      "queued" (admitted but waiting). When both are at capacity it
      raises :class:`OverloadedError`.
    * The book-keeping is pure counters — admitted requests aren't
      actually parked here. The queue cap exists only to provide a hard
      memory bound: the request count above the inflight limit cannot
      exceed ``max_queue``.
    * ``stats`` returns a snapshot of current counters; the admin
      endpoint surfaces this.

    The implementation is intentionally minimal:
    * No fairness guarantees — a saturated shedder rejects.
    * No per-tenant accounting — that's a higher-layer concern.

    Thread-safety: an asyncio.Lock guards the counters. Releases happen
    in the context manager's ``finally`` block so an exception raised
    inside the request handler still decrements the counter.
    """

    def __init__(
        self,
        *,
        max_inflight: int = 200,
        max_queue: int = 1000,
        retry_after_seconds: int = 1,
        enabled: bool = False,
    ) -> None:
        self.max_inflight = max_inflight
        self.max_queue = max_queue
        self.retry_after_seconds = retry_after_seconds
        self.enabled = enabled
        self._inflight = 0
        self._queued = 0
        self._shed_count = 0
        self._lock = asyncio.Lock()

    @asynccontextmanager
    async def acquire(self) -> AsyncIterator[None]:
        """Admit one request; raise :class:`OverloadedError` if at capacity."""

        async with self._lock:
            if (
                self._inflight >= self.max_inflight
                and self._queued >= self.max_queue
            ):
                self._shed_count += 1
                raise OverloadedError(retry_after=self.retry_after_seconds)

            counted_as_queued = self._inflight >= self.max_inflight
            if counted_as_queued:
                self._queued += 1
            else:
                self._inflight += 1

        try:
            yield
        finally:
            async with self._lock:
                if counted_as_queued:
                    # Defensive: clamp at zero in case of buggy callers.
                    if self._queued > 0:
                        self._queued -= 1
                elif self._inflight > 0:
                    self._inflight -= 1

    @property
    def stats(self) -> dict[str, Any]:
        """Snapshot of current counters (cheap, no lock)."""

        return {
            "inflight": self._inflight,
            "queued": self._queued,
            "shed_count": self._shed_count,
            "max_inflight": self.max_inflight,
            "max_queue": self.max_queue,
            "enabled": self.enabled,
        }


async def load_shed_middleware(request: Request, call_next):
    """ASGI middleware: gate every non-health request through the shedder.

    Wired into ``create_app`` after the auth/audit middleware so a 503
    here is observed by the same logging/error envelope path the rest of
    the API uses.
    """

    shedder: LoadShedder = request.app.state.load_shedder

    # Healthz must remain available even when we're shedding — it's the
    # signal the LB / orchestrator uses to decide to take this instance
    # out of rotation.
    if not shedder.enabled or request.url.path == "/healthz":
        return await call_next(request)

    try:
        async with shedder.acquire():
            return await call_next(request)
    except OverloadedError as exc:
        return JSONResponse(
            status_code=503,
            headers={"Retry-After": str(exc.retry_after)},
            content={
                "error": {
                    "code": "OVERLOADED",
                    "message": "Service overloaded; please retry",
                    "details": {"retry_after_seconds": exc.retry_after},
                }
            },
        )


__all__ = ["LoadShedder", "OverloadedError", "load_shed_middleware"]
