# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 The Plinth Authors
"""Token-bucket rate limiter.

This module owns the per-agent rate-limit primitive, independent of the cost
cap (which lives in :mod:`cost_caps`). It is split from the higher-level
:mod:`limits` registry so the math is easy to reason about and unit-test in
isolation.

Two pieces:

* :class:`TokenBucket` — pure in-memory bucket with lazy refill. ``try_acquire``
  returns ``(allowed, retry_after_seconds)``.
* :class:`RateLimiter` — per-process map of agent_id → bucket. Buckets are
  created lazily and rebuilt when the agent's limits change.

The bucket map is in-memory; it does **not** survive a process restart. A
companion ``rate_limit_snapshots`` SQL table is provided for future cross-
restart persistence (currently unused — see :func:`snapshot_to_db` /
:func:`restore_from_db`). For v0.2 the gateway is single-node, so in-memory
state is acceptable.

The clock is injectable (``time_fn``) so tests can advance it deterministically.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone

from .db import Database

# ---------------------------------------------------------------------------
# TokenBucket
# ---------------------------------------------------------------------------


class TokenBucket:
    """Classic token bucket.

    Starts full at ``capacity`` tokens. Each :meth:`try_acquire` first refills
    the bucket lazily by ``elapsed * rate`` (capped at ``capacity``) and then
    either consumes ``n`` tokens or rejects the request.

    Args:
        rate_per_second: refill rate in tokens / second. Pre-compute from
            ``rpm`` as ``rpm / 60.0`` at the call site so this class stays
            unit-agnostic. Must be ``>= 0``; ``0`` means no refill (bucket only
            ever holds ``capacity`` tokens).
        capacity: maximum bucket size (the burst). Must be ``>= 0``.
        time_fn: monotonic clock; defaults to ``time.monotonic``. Tests inject
            a fake clock to make refill math deterministic.
    """

    __slots__ = ("rate", "capacity", "tokens", "last_refill", "_time_fn")

    def __init__(
        self,
        rate_per_second: float,
        capacity: int,
        *,
        time_fn: Callable[[], float] | None = None,
    ) -> None:
        if capacity < 0:
            raise ValueError("capacity must be >= 0")
        if rate_per_second < 0:
            raise ValueError("rate_per_second must be >= 0")
        self.rate = float(rate_per_second)
        self.capacity = int(capacity)
        self.tokens = float(capacity)
        self._time_fn = time_fn or time.monotonic
        self.last_refill = self._time_fn()

    def _refill(self) -> None:
        now = self._time_fn()
        elapsed = now - self.last_refill
        if elapsed > 0:
            # Cap refill at capacity — never overflow the bucket.
            self.tokens = min(self.capacity, self.tokens + elapsed * self.rate)
            self.last_refill = now

    def try_acquire(self, n: int = 1) -> tuple[bool, float]:
        """Try to consume ``n`` tokens.

        Returns ``(True, 0.0)`` on success; otherwise ``(False, retry_after)``
        where ``retry_after`` is seconds to wait until ``n`` tokens are
        available. ``retry_after`` is ``inf`` if ``rate == 0`` and the bucket
        can't satisfy the request.
        """
        if n <= 0:
            return True, 0.0
        if n > self.capacity:
            # The bucket can never hold this many tokens — never satisfiable
            # without changing capacity. Surface a useful retry estimate.
            if self.rate <= 0:
                return False, float("inf")
            return False, n / self.rate
        self._refill()
        if self.tokens >= n:
            self.tokens -= n
            return True, 0.0
        if self.rate <= 0:
            return False, float("inf")
        deficit = n - self.tokens
        return False, deficit / self.rate

    def snapshot_tokens(self) -> float:
        """Refill, then return the current token count.

        Used by the ``/v1/limits/{id}/status`` endpoint. Returning a refilled
        value (rather than the stale ``self.tokens``) means the user sees the
        current bucket level rather than the last-call value.
        """
        self._refill()
        return self.tokens


# ---------------------------------------------------------------------------
# RateLimiter
# ---------------------------------------------------------------------------


@dataclass
class _BucketEntry:
    bucket: TokenBucket
    rpm: int
    burst: int


class RateLimiter:
    """Per-process in-memory token-bucket store, keyed by ``agent_id``.

    Storage is **not** distributed: a multi-node deployment would see each
    node's buckets fill independently. For v0.2 the gateway is single-node so
    this is acceptable. To migrate later, replace this class with a backend
    that uses Redis ``INCRBY`` + a TTL or the equivalent atomic primitive.

    The store guards mutations with an :class:`asyncio.Lock` because buckets
    are dict-mapped and FastAPI handlers run on the same event loop.

    Args:
        time_fn: optional monotonic-clock override (tests).
    """

    def __init__(self, *, time_fn: Callable[[], float] | None = None) -> None:
        self._time_fn = time_fn
        self._buckets: dict[str, _BucketEntry] = {}
        self._lock = asyncio.Lock()

    @staticmethod
    def _rate_from_rpm(rpm: int) -> float:
        return rpm / 60.0

    async def _ensure_bucket(self, agent_id: str, rpm: int, burst: int) -> _BucketEntry:
        """Get-or-create a bucket entry, rebuilding if rpm/burst changed."""
        async with self._lock:
            entry = self._buckets.get(agent_id)
            if entry is not None and entry.rpm == rpm and entry.burst == burst:
                return entry
            # Either no bucket yet, or limits changed — drop the old one and
            # build a fresh bucket. We don't try to preserve token level when
            # capacity changes; safer to start full so newly-tightened limits
            # don't immediately starve the agent.
            bucket = TokenBucket(
                rate_per_second=self._rate_from_rpm(rpm),
                capacity=burst,
                time_fn=self._time_fn,
            )
            entry = _BucketEntry(bucket=bucket, rpm=rpm, burst=burst)
            self._buckets[agent_id] = entry
            return entry

    async def check(self, agent_id: str, rpm: int, burst: int) -> tuple[bool, float]:
        """Try to consume one token for ``agent_id``.

        Returns ``(allowed, retry_after_seconds)``. The bucket is created
        lazily if it doesn't exist; if the supplied ``rpm`` or ``burst`` differ
        from the live bucket's values, the bucket is rebuilt.
        """
        entry = await self._ensure_bucket(agent_id, rpm, burst)
        return entry.bucket.try_acquire(1)

    async def get_bucket(self, agent_id: str) -> _BucketEntry | None:
        """Return the live bucket entry for ``agent_id`` (or ``None``).

        Used by the status endpoint — it wants to read ``snapshot_tokens()``
        without consuming.
        """
        async with self._lock:
            return self._buckets.get(agent_id)

    async def reset(self, agent_id: str) -> None:
        """Drop ``agent_id``'s bucket so the next ``check`` rebuilds it.

        Called by the limits registry on ``set_limits`` / ``delete_limits`` so
        new rpm/burst values are honoured immediately rather than on the next
        rpm change.
        """
        async with self._lock:
            self._buckets.pop(agent_id, None)

    async def reset_all(self) -> None:
        """Clear every bucket. Mostly useful in tests."""
        async with self._lock:
            self._buckets.clear()


# ---------------------------------------------------------------------------
# Snapshot persistence (optional — for graceful restart)
# ---------------------------------------------------------------------------


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


async def snapshot_to_db(db: Database, limiter: RateLimiter) -> int:
    """Persist every live bucket's current token level to ``rate_limit_snapshots``.

    Returns the number of rows written. Currently called by no production
    path; provided so future graceful-shutdown handlers can persist state and
    survive restarts. Unit-tested for correctness.
    """
    written = 0
    async with limiter._lock:
        items = list(limiter._buckets.items())
    for agent_id, entry in items:
        tokens = entry.bucket.snapshot_tokens()
        await db.execute(
            """
            INSERT INTO rate_limit_snapshots (agent_id, tokens, last_refill)
            VALUES (?, ?, ?)
            ON CONFLICT(agent_id) DO UPDATE SET
              tokens = excluded.tokens,
              last_refill = excluded.last_refill
            """,
            (agent_id, float(tokens), _utcnow_iso()),
        )
        written += 1
    return written


async def restore_from_db(db: Database) -> dict[str, float]:
    """Read every persisted bucket level. Returns ``{agent_id: tokens}``.

    The caller is expected to seed buckets at the right level on startup.
    """
    rows = await db.fetchall(
        "SELECT agent_id, tokens FROM rate_limit_snapshots", ()
    )
    return {r["agent_id"]: float(r["tokens"]) for r in rows}
