# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 The Plinth Authors
"""Rolling-window cost tracker.

The cost cap is computed from the audit trail rather than a separate counter:
``cost_used_in_window`` sums ``cost_estimate_usd`` from ``audit_events`` for an
agent over the last N hours. This means the cost cap stays accurate even after
a process restart — there is no in-memory state to lose.

Cached invocations record a cost of $0 in the audit log (see ``pricing.py``).
We additionally filter ``cached = 0`` in the SQL so any pricing changes don't
silently start charging cached calls.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from .db import Database


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


async def cost_used_in_window(
    db: Database,
    agent_id: str,
    hours: int,
    *,
    include_cached: bool = False,
) -> float:
    """Return the total ``cost_estimate_usd`` charged to ``agent_id`` in the last ``hours``.

    Args:
        db: gateway :class:`Database` handle.
        agent_id: the agent whose cost is being summed.
        hours: window size, in hours (e.g. 1 for the hour cap, 24 for the day cap).
        include_cached: if ``True``, include cached calls in the sum. Defaults
            to ``False`` — cached calls cost $0 and shouldn't move the cap.

    Returns:
        sum of ``cost_estimate_usd`` for matching events, in USD.
    """
    if hours <= 0:
        return 0.0
    cutoff = _utcnow() - timedelta(hours=hours)
    if include_cached:
        sql = (
            "SELECT COALESCE(SUM(cost_estimate_usd), 0) AS s FROM audit_events "
            "WHERE agent_id = ? AND timestamp >= ?"
        )
    else:
        sql = (
            "SELECT COALESCE(SUM(cost_estimate_usd), 0) AS s FROM audit_events "
            "WHERE agent_id = ? AND timestamp >= ? AND cached = 0"
        )
    row = await db.fetchone(sql, (agent_id, cutoff.isoformat()))
    if row is None or row["s"] is None:
        return 0.0
    return float(row["s"])


async def calls_in_window_seconds(
    db: Database,
    agent_id: str,
    seconds: int,
) -> int:
    """Count audit events for ``agent_id`` in the last ``seconds`` seconds.

    Used by the status endpoint to report ``rpm_used_in_window`` (calls / 60s).
    """
    if seconds <= 0:
        return 0
    cutoff = _utcnow() - timedelta(seconds=seconds)
    row = await db.fetchone(
        "SELECT COUNT(*) AS c FROM audit_events "
        "WHERE agent_id = ? AND timestamp >= ?",
        (agent_id, cutoff.isoformat()),
    )
    return int(row["c"]) if row is not None else 0


class CostCapTracker:
    """Thin wrapper that keeps the cap thresholds + the DB handle together.

    Use it when you'd otherwise pass ``(db, hour_cap, day_cap)`` everywhere.
    The status/enforcement code in :mod:`limits` instantiates a tracker per
    request to keep the call sites short.
    """

    def __init__(
        self,
        db: Database,
        *,
        hour_cap_usd: float,
        day_cap_usd: float,
    ) -> None:
        self._db = db
        self.hour_cap_usd = float(hour_cap_usd)
        self.day_cap_usd = float(day_cap_usd)

    async def used_hour(self, agent_id: str) -> float:
        return await cost_used_in_window(self._db, agent_id, 1)

    async def used_day(self, agent_id: str) -> float:
        return await cost_used_in_window(self._db, agent_id, 24)

    async def check(self, agent_id: str) -> tuple[str | None, float, float]:
        """Return ``(violated_window, used, cap)``.

        ``violated_window`` is ``"cost_hour"``, ``"cost_day"`` or ``None``.
        Caps of ``0`` (or negative) are treated as "disabled" — never raise.
        """
        if self.hour_cap_usd > 0:
            used = await self.used_hour(agent_id)
            if used >= self.hour_cap_usd:
                return "cost_hour", used, self.hour_cap_usd
        if self.day_cap_usd > 0:
            used = await self.used_day(agent_id)
            if used >= self.day_cap_usd:
                return "cost_day", used, self.day_cap_usd
        return None, 0.0, 0.0
