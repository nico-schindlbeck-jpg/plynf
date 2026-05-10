# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 The Plinth Authors
"""v1.4 — Audit-log anomaly detection.

Pure-Python detectors run over the existing ``audit_events`` table. No
ML, no extra dependencies — just z-score against a 60-minute trailing
baseline plus a couple of structural checks (new tools, unusual
sequences). The detector is cheap enough to call inline on dashboard
refreshes; results are cached for 30 seconds so a busy SPA doesn't
hammer the DB.

Detector taxonomy
-----------------

* ``cost_spike``: per-agent per-minute cost vs trailing 60-min mean+std.
* ``rate_spike``: per-agent invocations/minute vs same trailing window.
* ``error_spike``: per-tool error rate vs the same window. Triggered by
  multiplicative blow-up (>5x baseline) AND a minimum of 5 errors so
  noise on a low-volume tool doesn't cry wolf.
* ``new_tool``: an agent invokes a tool_id never seen for that agent in
  the trailing 24h. Always ``info`` — it's just a signal.
* ``unusual_pattern``: per-agent per-minute, the canonical hash of the
  ordered tool_id sequence in that minute differs from any prior 1-min
  window in the trailing 24h. Always ``info``.

Tunables
--------

The constants below are deliberately conservative to keep noise low.
Raising :data:`Z_CRITICAL` (e.g. to 4) reduces critical alerts at the
cost of slower detection on real spikes; lowering :data:`MIN_ERRORS`
makes the error detector more eager.
"""

from __future__ import annotations

import asyncio
import hashlib
import math
import re
import time
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from ulid import ULID

from .db import Database
from .models import Anomaly, AnomalyReport

# ---------------------------------------------------------------------------
# Detector tunables — see module docstring.

#: z-score threshold for a "warning" anomaly (cost / rate detectors).
Z_WARNING: float = 2.0
#: z-score threshold for a "critical" anomaly (cost / rate detectors).
Z_CRITICAL: float = 3.0
#: Multiplicative threshold for the error_spike detector ("warning").
ERR_MULT_WARNING: float = 5.0
#: Multiplicative threshold for the error_spike detector ("critical").
ERR_MULT_CRITICAL: float = 10.0
#: Minimum errors in the focus window to even consider firing.
MIN_ERRORS: int = 5
#: Width of the focus window (the most-recent slice we evaluate).
FOCUS_MINUTES: int = 1
#: Width of the trailing baseline used for mean/stddev.
BASELINE_MINUTES: int = 60
#: Width of the "trailing 24h" used by structural detectors.
LOOKBACK_HOURS: int = 24
#: How long anomaly results are cached (seconds).
CACHE_TTL_SECONDS: float = 30.0


_WINDOW_RE = re.compile(r"^\s*(\d+)\s*([smhd])\s*$", re.IGNORECASE)
_UNIT_TO_SECONDS: dict[str, int] = {
    "s": 1,
    "m": 60,
    "h": 3600,
    "d": 86400,
}


def parse_window(value: str) -> timedelta:
    """Convert ``"1h"`` / ``"24h"`` / ``"7d"`` / ``"30d"`` / ``"30m"`` to ``timedelta``.

    Supports the same shape as the cost-by-agent endpoint plus seconds
    (``"60s"``) for tests. Raises :class:`ValueError` for any other
    shape so the API layer can surface a 400.
    """

    match = _WINDOW_RE.match(value or "")
    if not match:
        raise ValueError(
            f"Invalid window {value!r}. Expected '1h', '24h', '7d', '30d'."
        )
    n = int(match.group(1))
    unit = match.group(2).lower()
    if n <= 0:
        raise ValueError(f"Invalid window {value!r}: must be positive.")
    return timedelta(seconds=n * _UNIT_TO_SECONDS[unit])


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _new_anomaly_id() -> str:
    return f"anom_{ULID()}"


def _floor_minute(ts: datetime) -> datetime:
    """Round ``ts`` down to the start of its minute."""
    return ts.replace(second=0, microsecond=0)


def _parse_ts(value: object) -> datetime:
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    return datetime.fromisoformat(str(value).replace("Z", "+00:00"))


def _mean_stddev(samples: list[float]) -> tuple[float, float]:
    """Population mean + stddev of ``samples``. Returns (0, 0) if empty."""
    if not samples:
        return 0.0, 0.0
    n = len(samples)
    mean = sum(samples) / n
    var = sum((x - mean) ** 2 for x in samples) / n
    return mean, math.sqrt(var)


def _z_score(value: float, mean: float, stddev: float) -> float:
    """Return ``(value - mean) / stddev`` with a tiny epsilon guard.

    When stddev is 0 we return:
      * 0 if value == mean (no spike, just a flat baseline);
      * a large finite number proportional to how far value sits above
        mean. We use ``value / max(epsilon, mean)`` as a fallback so a
        first-ever non-zero sample on a flat-zero baseline still scores
        as a spike rather than infinity.
    """

    if stddev > 0:
        return (value - mean) / stddev
    # Flat baseline. If value matches mean, no anomaly.
    if abs(value - mean) < 1e-12:
        return 0.0
    # Express the magnitude in "units of mean" so a first-ever sample
    # still surfaces as something other than NaN/inf. 100 is a soft cap
    # — enough to clear Z_CRITICAL but not infinite.
    eps = max(1e-9, abs(mean))
    score = (value - mean) / eps
    return max(min(score, 100.0), -100.0)


def _severity_from_z(z: float) -> str | None:
    abs_z = abs(z)
    if abs_z >= Z_CRITICAL:
        return "critical"
    if abs_z >= Z_WARNING:
        return "warning"
    return None


@dataclass
class _MinuteRow:
    """A single per-minute aggregate over the audit log."""

    minute: datetime
    agent_id: str | None
    tenant_id: str
    tool_id: str
    invocations: int
    errors: int
    cost_usd: float


# ---------------------------------------------------------------------------
# In-process result cache. The detector can be expensive on a busy log;
# the dashboard hits us every 30s and a test rig may hit us repeatedly
# from inside the same process. The cache key is the full (window, type
# filter, severity, agent filter) tuple.


_CACHE_LOCK = asyncio.Lock()
_CACHE: dict[tuple, tuple[float, AnomalyReport]] = {}


def _cache_key(
    *,
    window: str,
    type_filter: str | None,
    min_severity: str,
    agent_id: str | None,
    tenant_id: str | None,
) -> tuple:
    return (window, type_filter or "", min_severity, agent_id or "", tenant_id or "")


def clear_cache() -> None:
    """Drop every cached anomaly report — used by tests."""
    _CACHE.clear()


_SEVERITY_ORDER = {"info": 0, "warning": 1, "critical": 2}


def _severity_at_least(have: str, want: str) -> bool:
    return _SEVERITY_ORDER.get(have, 0) >= _SEVERITY_ORDER.get(want, 0)


# ---------------------------------------------------------------------------
# Public detector entry point.


class AnomalyDetector:
    """Detect anomalies in the gateway audit log.

    Constructed with a :class:`Database`. ``detect()`` is the only
    public entry point — it loads the audit slice, runs every per-type
    detector, optionally caches the result, and returns an
    :class:`AnomalyReport`.
    """

    def __init__(self, db: Database) -> None:
        self._db = db

    # -- detector composition ------------------------------------------

    async def detect(
        self,
        *,
        window: str = "1h",
        min_severity: str = "info",
        type_filter: str | None = None,
        agent_id: str | None = None,
        tenant_id: str | None = None,
        use_cache: bool = True,
        now: datetime | None = None,
    ) -> AnomalyReport:
        """Run every detector + return the merged :class:`AnomalyReport`.

        Args:
            window: Output window (``"1h"`` / ``"24h"`` etc.). The
                detector uses a fixed 60-minute baseline regardless;
                ``window`` only governs how far back we look for the
                anomaly *focus*.
            min_severity: ``"info" | "warning" | "critical"``. Lower
                severities are dropped from the response.
            type_filter: Restrict to a single anomaly type, or ``None``
                for all.
            agent_id: Restrict the focus window to one agent.
            tenant_id: Restrict every query to one tenant.
            use_cache: Reuse a fresh cached report if available.
            now: Override the "current time" — used by tests so the
                detector behaves deterministically against fixture
                rows. Defaults to ``datetime.now(UTC)``.
        """

        # Normalise window ahead of the cache lookup so equivalent shapes
        # share a cache slot.
        window_td = parse_window(window)
        now = now or _utcnow()

        if min_severity not in _SEVERITY_ORDER:
            raise ValueError(
                f"Invalid min_severity {min_severity!r}. "
                "Expected info | warning | critical."
            )

        key = _cache_key(
            window=window,
            type_filter=type_filter,
            min_severity=min_severity,
            agent_id=agent_id,
            tenant_id=tenant_id,
        )

        if use_cache:
            async with _CACHE_LOCK:
                cached = _CACHE.get(key)
                if cached is not None:
                    cached_at, report = cached
                    if (time.time() - cached_at) < CACHE_TTL_SECONDS:
                        return report

        focus_start = now - window_td
        baseline_start = now - timedelta(minutes=BASELINE_MINUTES)
        lookback_start = now - timedelta(hours=LOOKBACK_HOURS)
        # We need the union: baseline + lookback covers all detectors.
        load_from = min(focus_start, baseline_start, lookback_start)

        rows = await self._load_minute_rows(
            since=load_from,
            tenant_id=tenant_id,
        )

        anomalies: list[Anomaly] = []
        anomalies.extend(
            self._cost_spike(
                rows,
                now=now,
                focus_start=focus_start,
                agent_filter=agent_id,
            )
        )
        anomalies.extend(
            self._rate_spike(
                rows,
                now=now,
                focus_start=focus_start,
                agent_filter=agent_id,
            )
        )
        anomalies.extend(
            self._error_spike(
                rows,
                now=now,
                focus_start=focus_start,
            )
        )
        anomalies.extend(
            self._new_tool(
                rows,
                now=now,
                focus_start=focus_start,
                lookback_start=lookback_start,
                agent_filter=agent_id,
            )
        )
        anomalies.extend(
            self._unusual_pattern(
                rows,
                now=now,
                focus_start=focus_start,
                lookback_start=lookback_start,
                agent_filter=agent_id,
            )
        )

        # Filter by type + severity.
        if type_filter is not None:
            anomalies = [a for a in anomalies if a.type == type_filter]
        anomalies = [
            a for a in anomalies if _severity_at_least(a.severity, min_severity)
        ]

        # Most-severe-first, then most-recent-first for ties.
        anomalies.sort(
            key=lambda a: (
                -_SEVERITY_ORDER.get(a.severity, 0),
                -abs(a.z_score),
                -a.detected_at.timestamp(),
            )
        )

        by_severity: dict[str, int] = {}
        for a in anomalies:
            by_severity[a.severity] = by_severity.get(a.severity, 0) + 1

        report = AnomalyReport(
            detected_at=now,
            window=window,
            anomalies=anomalies,
            total_anomalies=len(anomalies),
            by_severity=by_severity,
        )

        async with _CACHE_LOCK:
            _CACHE[key] = (time.time(), report)
        return report

    # -- audit log loader ----------------------------------------------

    async def _load_minute_rows(
        self,
        *,
        since: datetime,
        tenant_id: str | None,
    ) -> list[_MinuteRow]:
        """Aggregate raw audit events into per-minute buckets.

        Doing the aggregation client-side keeps the SQL portable and
        means each detector can re-key the same dataset rather than
        making N round-trips.
        """

        clauses: list[str] = ["timestamp >= ?"]
        params: list[Any] = [since.isoformat()]
        if tenant_id is not None:
            clauses.append("(tenant_id = ? OR (tenant_id IS NULL AND ? = 'default'))")
            params.append(tenant_id)
            params.append(tenant_id)
        where = "WHERE " + " AND ".join(clauses)

        raw_rows = await self._db.fetchall(
            f"SELECT timestamp, agent_id, tenant_id, tool_id, "
            f"       cost_estimate_usd, error "
            f"FROM audit_events {where} "
            f"ORDER BY timestamp ASC",
            tuple(params),
        )

        bucket: dict[
            tuple[datetime, str | None, str, str], _MinuteRow
        ] = {}
        for row in raw_rows:
            ts = _parse_ts(row["timestamp"])
            minute = _floor_minute(ts)
            agent_id = row["agent_id"]
            tenant = row["tenant_id"] or "default"
            tool_id = row["tool_id"]
            cost = float(row["cost_estimate_usd"] or 0.0)
            errored = row["error"] is not None
            key = (minute, agent_id, tenant, tool_id)
            entry = bucket.get(key)
            if entry is None:
                entry = _MinuteRow(
                    minute=minute,
                    agent_id=agent_id,
                    tenant_id=tenant,
                    tool_id=tool_id,
                    invocations=0,
                    errors=0,
                    cost_usd=0.0,
                )
                bucket[key] = entry
            entry.invocations += 1
            if errored:
                entry.errors += 1
            entry.cost_usd += cost
        return list(bucket.values())

    # -- detectors ------------------------------------------------------
    #
    # Each detector receives the full minute-row list and returns a
    # list[Anomaly]. They're written as instance methods rather than
    # free functions so the cache + DB stay one place — easier to test
    # in isolation by constructing a detector with an in-memory DB.

    def _cost_spike(
        self,
        rows: list[_MinuteRow],
        *,
        now: datetime,
        focus_start: datetime,
        agent_filter: str | None,
    ) -> list[Anomaly]:
        """Per-agent per-minute cost vs trailing 60-minute baseline.

        Folds rows by (minute, agent_id) so multi-tool minutes stack.
        Only the *most recent* :data:`FOCUS_MINUTES` minutes are
        evaluated — the rest of the requested window is reserved as
        baseline.
        """

        out: list[Anomaly] = []
        by_agent_minute: dict[str | None, dict[datetime, float]] = defaultdict(dict)
        tenant_for_agent: dict[str | None, str] = {}
        for r in rows:
            if agent_filter is not None and r.agent_id != agent_filter:
                continue
            d = by_agent_minute[r.agent_id]
            d[r.minute] = d.get(r.minute, 0.0) + r.cost_usd
            tenant_for_agent.setdefault(r.agent_id, r.tenant_id)

        # Focus = most recent FOCUS_MINUTES minutes that are also inside
        # the caller-requested focus window. This keeps the detector
        # tight enough that a steady, on-trend hourly background doesn't
        # parade as N anomalies.
        focus_floor = _floor_minute(
            max(focus_start, now - timedelta(minutes=FOCUS_MINUTES))
        )
        baseline_floor = _floor_minute(now - timedelta(minutes=BASELINE_MINUTES))

        for agent_id, minute_to_cost in by_agent_minute.items():
            # Per-agent baseline: every minute in the trailing 60 min
            # outside the focus window. We pad with zeros so a quiet
            # stretch is treated as "zero spend" rather than "no data".
            baseline_pairs = [
                (m, c) for m, c in minute_to_cost.items()
                if baseline_floor <= m < focus_floor
            ]
            baseline_samples = [c for _, c in baseline_pairs]
            baseline_window_minutes = max(
                1,
                int((focus_floor - baseline_floor).total_seconds() // 60),
            )
            pad = max(0, baseline_window_minutes - len(baseline_samples))
            baseline_samples = baseline_samples + [0.0] * pad
            mean, stddev = _mean_stddev(baseline_samples)

            for minute, cost in minute_to_cost.items():
                if minute < focus_floor:
                    continue
                z = _z_score(cost, mean, stddev)
                severity = _severity_from_z(z)
                if severity is None or cost <= mean:
                    continue
                out.append(
                    Anomaly(
                        id=_new_anomaly_id(),
                        type="cost_spike",
                        severity=severity,  # type: ignore[arg-type]
                        agent_id=agent_id,
                        tenant_id=tenant_for_agent.get(agent_id),
                        tool_id=None,
                        detected_at=now,
                        window_start=baseline_floor,
                        window_end=minute + timedelta(minutes=1),
                        description=(
                            f"agent {agent_id or '(unknown)'} cost "
                            f"${cost:.4f} in 1-minute window vs baseline "
                            f"${mean:.4f}±${stddev:.4f}"
                        ),
                        metric_name="cost_usd_per_minute",
                        metric_value=float(cost),
                        baseline_mean=float(mean),
                        baseline_stddev=float(stddev),
                        z_score=float(z),
                        raw_data={
                            "minute": minute.isoformat(),
                            "baseline_samples": [
                                round(s, 6) for s in baseline_samples
                            ],
                        },
                    )
                )
        return out

    def _rate_spike(
        self,
        rows: list[_MinuteRow],
        *,
        now: datetime,
        focus_start: datetime,
        agent_filter: str | None,
    ) -> list[Anomaly]:
        """Per-agent invocations/minute vs trailing 60-min baseline."""

        out: list[Anomaly] = []
        by_agent_minute: dict[str | None, dict[datetime, int]] = defaultdict(dict)
        tenant_for_agent: dict[str | None, str] = {}
        for r in rows:
            if agent_filter is not None and r.agent_id != agent_filter:
                continue
            d = by_agent_minute[r.agent_id]
            d[r.minute] = d.get(r.minute, 0) + r.invocations
            tenant_for_agent.setdefault(r.agent_id, r.tenant_id)

        focus_floor = _floor_minute(
            max(focus_start, now - timedelta(minutes=FOCUS_MINUTES))
        )
        baseline_floor = _floor_minute(now - timedelta(minutes=BASELINE_MINUTES))

        for agent_id, minute_to_count in by_agent_minute.items():
            baseline_samples = [
                float(c) for m, c in minute_to_count.items()
                if baseline_floor <= m < focus_floor
            ]
            baseline_window_minutes = max(
                1,
                int((focus_floor - baseline_floor).total_seconds() // 60),
            )
            pad = max(0, baseline_window_minutes - len(baseline_samples))
            baseline_samples = baseline_samples + [0.0] * pad
            mean, stddev = _mean_stddev(baseline_samples)

            for minute, count in minute_to_count.items():
                if minute < focus_floor:
                    continue
                z = _z_score(float(count), mean, stddev)
                severity = _severity_from_z(z)
                if severity is None or count <= mean:
                    continue
                out.append(
                    Anomaly(
                        id=_new_anomaly_id(),
                        type="rate_spike",
                        severity=severity,  # type: ignore[arg-type]
                        agent_id=agent_id,
                        tenant_id=tenant_for_agent.get(agent_id),
                        tool_id=None,
                        detected_at=now,
                        window_start=baseline_floor,
                        window_end=minute + timedelta(minutes=1),
                        description=(
                            f"agent {agent_id or '(unknown)'} ran "
                            f"{count} invocations/min vs baseline "
                            f"{mean:.2f}±{stddev:.2f}"
                        ),
                        metric_name="invocations_per_minute",
                        metric_value=float(count),
                        baseline_mean=float(mean),
                        baseline_stddev=float(stddev),
                        z_score=float(z),
                        raw_data={
                            "minute": minute.isoformat(),
                            "baseline_samples": baseline_samples,
                        },
                    )
                )
        return out

    def _error_spike(
        self,
        rows: list[_MinuteRow],
        *,
        now: datetime,
        focus_start: datetime,
    ) -> list[Anomaly]:
        """Per-tool error rate vs trailing 60-min baseline.

        Threshold is multiplicative + bounded. We compare the focus
        window's error count to the baseline minute average; the
        "minimum 5 errors" floor avoids spurious alerts on a low-traffic
        tool. As with cost/rate detectors, only the most recent
        :data:`FOCUS_MINUTES` are scored — the rest is baseline.
        """

        out: list[Anomaly] = []
        by_tool_minute: dict[str, dict[datetime, tuple[int, int]]] = defaultdict(dict)
        tenant_for_tool: dict[str, str] = {}
        for r in rows:
            d = by_tool_minute[r.tool_id]
            existing_inv, existing_err = d.get(r.minute, (0, 0))
            d[r.minute] = (existing_inv + r.invocations, existing_err + r.errors)
            tenant_for_tool.setdefault(r.tool_id, r.tenant_id)

        focus_floor = _floor_minute(
            max(focus_start, now - timedelta(minutes=FOCUS_MINUTES))
        )
        baseline_floor = _floor_minute(now - timedelta(minutes=BASELINE_MINUTES))

        for tool_id, minute_to_pair in by_tool_minute.items():
            baseline_errors = [
                float(e) for m, (_i, e) in minute_to_pair.items()
                if baseline_floor <= m < focus_floor
            ]
            baseline_window_minutes = max(
                1,
                int((focus_floor - baseline_floor).total_seconds() // 60),
            )
            pad = max(0, baseline_window_minutes - len(baseline_errors))
            baseline_errors = baseline_errors + [0.0] * pad
            mean, stddev = _mean_stddev(baseline_errors)

            for minute, (inv, err) in minute_to_pair.items():
                if minute < focus_floor:
                    continue
                if err < MIN_ERRORS:
                    continue
                ratio = (err / max(1e-9, mean)) if mean > 0 else float(err)
                severity: str | None = None
                if mean == 0 and err >= MIN_ERRORS:
                    severity = (
                        "critical" if err >= MIN_ERRORS * 2 else "warning"
                    )
                elif ratio >= ERR_MULT_CRITICAL:
                    severity = "critical"
                elif ratio >= ERR_MULT_WARNING:
                    severity = "warning"
                if severity is None:
                    continue
                z = _z_score(float(err), mean, stddev)
                out.append(
                    Anomaly(
                        id=_new_anomaly_id(),
                        type="error_spike",
                        severity=severity,  # type: ignore[arg-type]
                        agent_id=None,
                        tenant_id=tenant_for_tool.get(tool_id),
                        tool_id=tool_id,
                        detected_at=now,
                        window_start=baseline_floor,
                        window_end=minute + timedelta(minutes=1),
                        description=(
                            f"tool {tool_id} had {err} errors "
                            f"({ratio:.1f}x baseline mean {mean:.2f})"
                        ),
                        metric_name="errors_per_minute",
                        metric_value=float(err),
                        baseline_mean=float(mean),
                        baseline_stddev=float(stddev),
                        z_score=float(z),
                        raw_data={
                            "minute": minute.isoformat(),
                            "ratio": ratio,
                            "invocations_in_minute": inv,
                            "baseline_samples": baseline_errors,
                        },
                    )
                )
        return out

    def _new_tool(
        self,
        rows: list[_MinuteRow],
        *,
        now: datetime,
        focus_start: datetime,
        lookback_start: datetime,
        agent_filter: str | None,
    ) -> list[Anomaly]:
        """Agents using a tool_id never seen in their trailing 24h.

        We classify a tool as "new" if it appears in the focus window
        for an agent and does NOT appear in any earlier minute within
        the 24-hour lookback for that same agent. NULL agent_id rows
        are still considered (they all share the same "(unknown)"
        bucket) so an unauthenticated burst on a brand-new tool still
        surfaces. Only the most recent :data:`FOCUS_MINUTES` minutes
        count as "the focus window" — older "first uses" don't fire
        again on every refresh.
        """

        out: list[Anomaly] = []
        focus_floor = _floor_minute(
            max(focus_start, now - timedelta(minutes=FOCUS_MINUTES))
        )
        seen_in_lookback: dict[str | None, set[str]] = defaultdict(set)
        focus_uses: dict[
            tuple[str | None, str], tuple[datetime, str]
        ] = {}

        for r in rows:
            if agent_filter is not None and r.agent_id != agent_filter:
                continue
            if r.minute < lookback_start:
                continue
            if r.minute < focus_floor:
                seen_in_lookback[r.agent_id].add(r.tool_id)
                continue
            key = (r.agent_id, r.tool_id)
            existing = focus_uses.get(key)
            if existing is None or r.minute > existing[0]:
                focus_uses[key] = (r.minute, r.tenant_id)

        for (agent_id, tool_id), (minute, tenant) in focus_uses.items():
            if tool_id in seen_in_lookback.get(agent_id, set()):
                continue
            out.append(
                Anomaly(
                    id=_new_anomaly_id(),
                    type="new_tool",
                    severity="info",
                    agent_id=agent_id,
                    tenant_id=tenant,
                    tool_id=tool_id,
                    detected_at=now,
                    window_start=lookback_start,
                    window_end=now,
                    description=(
                        f"agent {agent_id or '(unknown)'} used tool "
                        f"{tool_id} for the first time in 24h"
                    ),
                    metric_name="new_tool_first_use",
                    metric_value=1.0,
                    baseline_mean=0.0,
                    baseline_stddev=0.0,
                    z_score=0.0,
                    raw_data={
                        "first_seen_at": minute.isoformat(),
                    },
                )
            )
        return out

    def _unusual_pattern(
        self,
        rows: list[_MinuteRow],
        *,
        now: datetime,
        focus_start: datetime,
        lookback_start: datetime,
        agent_filter: str | None,
    ) -> list[Anomaly]:
        """Per-agent per-minute tool sequence vs trailing 24h.

        Compute the canonical hash of the minute's ordered (alphabetised)
        tool_id list. If that hash hasn't appeared in any prior 1-min
        window for the same agent inside the lookback window, emit an
        ``info`` anomaly. Only the most recent :data:`FOCUS_MINUTES`
        minutes are evaluated; everything older counts as "seen".
        """

        out: list[Anomaly] = []
        focus_floor = _floor_minute(
            max(focus_start, now - timedelta(minutes=FOCUS_MINUTES))
        )
        by_agent_minute: dict[
            tuple[str | None, datetime], list[str]
        ] = defaultdict(list)
        tenant_for_agent: dict[str | None, str] = {}
        for r in rows:
            if agent_filter is not None and r.agent_id != agent_filter:
                continue
            if r.minute < lookback_start:
                continue
            by_agent_minute[(r.agent_id, r.minute)].append(r.tool_id)
            tenant_for_agent.setdefault(r.agent_id, r.tenant_id)

        seen_per_agent: dict[str | None, set[str]] = defaultdict(set)
        focus_per_agent: list[tuple[str | None, datetime, str, list[str]]] = []
        for (agent_id, minute), tools in by_agent_minute.items():
            tools_sorted = sorted(tools)
            digest = hashlib.sha256(
                "|".join(tools_sorted).encode("utf-8")
            ).hexdigest()
            if minute < focus_floor:
                seen_per_agent[agent_id].add(digest)
            else:
                focus_per_agent.append((agent_id, minute, digest, tools_sorted))

        for agent_id, minute, digest, tools_sorted in focus_per_agent:
            if digest in seen_per_agent.get(agent_id, set()):
                continue
            out.append(
                Anomaly(
                    id=_new_anomaly_id(),
                    type="unusual_pattern",
                    severity="info",
                    agent_id=agent_id,
                    tenant_id=tenant_for_agent.get(agent_id),
                    tool_id=None,
                    detected_at=now,
                    window_start=lookback_start,
                    window_end=now,
                    description=(
                        f"agent {agent_id or '(unknown)'} ran an unseen "
                        f"tool sequence in minute {minute.isoformat()}"
                    ),
                    metric_name="unique_tool_sequence",
                    metric_value=1.0,
                    baseline_mean=0.0,
                    baseline_stddev=0.0,
                    z_score=0.0,
                    raw_data={
                        "minute": minute.isoformat(),
                        "tools": tools_sorted,
                        "sequence_hash": digest,
                    },
                )
            )
        return out


__all__ = [
    "AnomalyDetector",
    "Z_WARNING",
    "Z_CRITICAL",
    "ERR_MULT_WARNING",
    "ERR_MULT_CRITICAL",
    "MIN_ERRORS",
    "FOCUS_MINUTES",
    "BASELINE_MINUTES",
    "LOOKBACK_HOURS",
    "CACHE_TTL_SECONDS",
    "parse_window",
    "clear_cache",
]
