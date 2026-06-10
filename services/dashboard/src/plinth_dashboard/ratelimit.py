# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 The Plynf Authors
"""Tiny in-memory sliding-window rate limiter for the auth surface.

Protects POST /api/app/signup and /api/app/login against credential
stuffing and signup spam. Deliberately simple: per-process, per-key
(client IP) sliding window — enough for a single-instance control plane.
When the dashboard ever runs multi-instance, swap the store for Redis
behind the same interface.
"""

from __future__ import annotations

import time
from collections import defaultdict, deque
from dataclasses import dataclass, field


@dataclass
class SlidingWindowLimiter:
    """Allow at most ``limit`` events per ``window_s`` seconds per key."""

    limit: int = 10
    window_s: float = 60.0
    _events: dict[str, deque[float]] = field(default_factory=lambda: defaultdict(deque))

    def allow(self, key: str, *, now: float | None = None) -> bool:
        if self.limit <= 0:  # 0 = limiter disabled
            return True
        ts = time.monotonic() if now is None else now
        q = self._events[key]
        cutoff = ts - self.window_s
        while q and q[0] <= cutoff:
            q.popleft()
        if len(q) >= self.limit:
            return False
        q.append(ts)
        return True

    def retry_after_s(self, key: str, *, now: float | None = None) -> int:
        ts = time.monotonic() if now is None else now
        q = self._events[key]
        if not q:
            return 0
        return max(0, int(q[0] + self.window_s - ts) + 1)


def client_key(request) -> str:
    """Best-effort client identity: first X-Forwarded-For hop, else peer IP.

    Fly/Caddy terminate TLS in front of us and set X-Forwarded-For.
    """
    fwd = request.headers.get("x-forwarded-for", "")
    if fwd:
        return fwd.split(",")[0].strip()
    client = getattr(request, "client", None)
    return getattr(client, "host", None) or "unknown"


__all__ = ["SlidingWindowLimiter", "client_key"]
