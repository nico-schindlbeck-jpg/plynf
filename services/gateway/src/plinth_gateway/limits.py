# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 The Plinth Authors
"""High-level rate limit + cost cap registry.

This module composes the two primitives:

* :class:`~plinth_gateway.rate_limit.RateLimiter` — token-bucket store
* :func:`~plinth_gateway.cost_caps.cost_used_in_window` — rolling-window sum

Re-exports :class:`TokenBucket` from :mod:`rate_limit` so existing callers /
tests continue to import it from this module unchanged.

The registry owns:

1. The ``agent_limits`` SQL table (per-agent overrides; defaults from settings).
2. The :class:`RateLimiter` instance (per-process bucket map).
3. The lookup helpers used by ``/v1/invoke`` and ``/v1/limits/{id}/status``.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timezone

from .cost_caps import calls_in_window_seconds, cost_used_in_window
from .db import Database
from .models import AgentLimits, AgentLimitsBody
from .rate_limit import RateLimiter, TokenBucket  # noqa: F401  (re-export)
from .settings import Settings

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _parse_ts(value: object) -> datetime:
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    return datetime.fromisoformat(str(value))


def _row_to_limits(row, agent_id: str) -> AgentLimits:
    return AgentLimits(
        agent_id=agent_id,
        rpm=int(row["rpm"]),
        burst=int(row["burst"]),
        cost_cap_usd_hour=float(row["cost_cap_usd_hour"]),
        cost_cap_usd_day=float(row["cost_cap_usd_day"]),
        updated_at=_parse_ts(row["updated_at"]),
    )


# ---------------------------------------------------------------------------
# LimitsRegistry
# ---------------------------------------------------------------------------


class LimitsRegistry:
    """Per-agent rate-limit + cost-cap registry.

    Composes :class:`RateLimiter` (in-memory token buckets) with the
    ``agent_limits`` table (DB-backed per-agent overrides) and the
    rolling-window cost queries against ``audit_events``.

    Exposes two enforcement helpers used by ``POST /v1/invoke``:
    :meth:`assert_within_rate` and :meth:`assert_within_cost_caps`.

    Args:
        db: gateway :class:`Database` handle.
        settings: live :class:`Settings` (provides defaults + the
            ``rate_limits_enabled`` switch).
        time_fn: optional monotonic clock for tests.
    """

    def __init__(
        self,
        db: Database,
        settings: Settings,
        *,
        time_fn: Callable[[], float] | None = None,
    ) -> None:
        self._db = db
        self._settings = settings
        self._limiter = RateLimiter(time_fn=time_fn)

    # ----- public access to the underlying limiter ------------------------

    @property
    def rate_limiter(self) -> RateLimiter:
        """Expose the rate-limiter — useful for status endpoints / tests."""
        return self._limiter

    # ----- defaults --------------------------------------------------------

    def _defaults_for(self, agent_id: str) -> AgentLimits:
        s = self._settings
        return AgentLimits(
            agent_id=agent_id,
            rpm=s.rate_limit_default_rpm,
            burst=s.rate_limit_default_burst,
            cost_cap_usd_hour=s.cost_cap_default_usd_hour,
            cost_cap_usd_day=s.cost_cap_default_usd_day,
            updated_at=_utcnow(),
        )

    # ----- DB-backed limits ------------------------------------------------

    async def _fetch_row(self, agent_id: str) -> AgentLimits | None:
        row = await self._db.fetchone(
            "SELECT rpm, burst, cost_cap_usd_hour, cost_cap_usd_day, updated_at "
            "FROM agent_limits WHERE agent_id = ?",
            (agent_id,),
        )
        if row is None:
            return None
        return _row_to_limits(row, agent_id)

    async def get_limits(self, agent_id: str) -> AgentLimits:
        """Return the agent's limits — DB row if present, otherwise defaults."""
        row = await self._fetch_row(agent_id)
        return row if row is not None else self._defaults_for(agent_id)

    async def set_limits(self, agent_id: str, body: AgentLimitsBody) -> AgentLimits:
        """Upsert per-agent overrides. Unset fields fall back to existing/default."""
        existing = await self._fetch_row(agent_id)
        base = existing or self._defaults_for(agent_id)
        new = AgentLimits(
            agent_id=agent_id,
            rpm=body.rpm if body.rpm is not None else base.rpm,
            burst=body.burst if body.burst is not None else base.burst,
            cost_cap_usd_hour=(
                body.cost_cap_usd_hour
                if body.cost_cap_usd_hour is not None
                else base.cost_cap_usd_hour
            ),
            cost_cap_usd_day=(
                body.cost_cap_usd_day
                if body.cost_cap_usd_day is not None
                else base.cost_cap_usd_day
            ),
            updated_at=_utcnow(),
        )
        await self._db.execute(
            """
            INSERT INTO agent_limits
              (agent_id, rpm, burst, cost_cap_usd_hour, cost_cap_usd_day, updated_at)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(agent_id) DO UPDATE SET
              rpm = excluded.rpm,
              burst = excluded.burst,
              cost_cap_usd_hour = excluded.cost_cap_usd_hour,
              cost_cap_usd_day = excluded.cost_cap_usd_day,
              updated_at = excluded.updated_at
            """,
            (
                agent_id,
                new.rpm,
                new.burst,
                new.cost_cap_usd_hour,
                new.cost_cap_usd_day,
                new.updated_at.isoformat(),
            ),
        )
        # Drop the in-memory bucket so the next call rebuilds at the new size.
        # Keeping the old bucket would silently honour stale rpm/burst.
        await self._limiter.reset(agent_id)
        return new

    async def delete_limits(self, agent_id: str) -> bool:
        """Remove an override row. Returns True if a row was removed."""
        existing = await self._fetch_row(agent_id)
        if existing is None:
            return False
        await self._db.execute(
            "DELETE FROM agent_limits WHERE agent_id = ?", (agent_id,)
        )
        await self._limiter.reset(agent_id)
        return True

    # ----- rate-limit check (delegates to RateLimiter) ---------------------

    async def check_rate(self, agent_id: str, n: int = 1) -> tuple[bool, float]:
        """Try to consume ``n`` tokens for ``agent_id``.

        Returns ``(ok, retry_after)``. On success ``retry_after`` is 0.0; on
        failure it's the wait-time in seconds.
        """
        if n != 1:
            # The RateLimiter API is one-token-at-a-time on purpose; the
            # gateway never bills more than one token per request. Keep this
            # method's signature backwards-compatible by routing through the
            # underlying bucket directly when the caller asks for n != 1.
            limits = await self.get_limits(agent_id)
            entry = await self._limiter._ensure_bucket(
                agent_id, limits.rpm, limits.burst
            )
            return entry.bucket.try_acquire(n)
        limits = await self.get_limits(agent_id)
        return await self._limiter.check(agent_id, limits.rpm, limits.burst)

    # ----- cost queries (DB-backed, rolling window) ------------------------

    async def cost_used(self, agent_id: str, window_hours: int) -> float:
        """Sum of ``cost_estimate_usd`` for non-cached events in the last N hours."""
        return await cost_used_in_window(self._db, agent_id, window_hours)

    async def rpm_used(self, agent_id: str) -> int:
        """Number of (non-cached) calls in the last 60 seconds — for status."""
        return await calls_in_window_seconds(self._db, agent_id, 60)

    # ----- combined enforcement helpers used by /v1/invoke -----------------

    async def assert_within_cost_caps(self, agent_id: str) -> None:
        """Raise :class:`CostCapExceeded` if either rolling window is over."""
        from .exceptions import CostCapExceeded

        limits = await self.get_limits(agent_id)
        if limits.cost_cap_usd_hour > 0:
            used_hour = await self.cost_used(agent_id, 1)
            if used_hour >= limits.cost_cap_usd_hour:
                raise CostCapExceeded(
                    reason="cost_hour",
                    used=used_hour,
                    cap=limits.cost_cap_usd_hour,
                )
        if limits.cost_cap_usd_day > 0:
            used_day = await self.cost_used(agent_id, 24)
            if used_day >= limits.cost_cap_usd_day:
                raise CostCapExceeded(
                    reason="cost_day",
                    used=used_day,
                    cap=limits.cost_cap_usd_day,
                )

    async def assert_within_rate(self, agent_id: str) -> None:
        """Raise :class:`RateLimited` on bucket-empty."""
        from .exceptions import RateLimited

        limits = await self.get_limits(agent_id)
        ok, retry_after = await self._limiter.check(
            agent_id, limits.rpm, limits.burst
        )
        if not ok:
            raise RateLimited(
                reason="rpm",
                retry_after=retry_after,
                current=limits.rpm,
                limit=limits.rpm,
            )
