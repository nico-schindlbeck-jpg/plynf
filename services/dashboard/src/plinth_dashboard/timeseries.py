# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 The Plinth Authors
"""Dashboard time-series builder.

The SPA's overview page renders 4 small SVG line charts: cost, p99 latency,
error rate, cache hit ratio. Each is fed by ``GET /api/timeseries`` which
aggregates the gateway's audit log (``GET /v1/audit?limit=...``) into N
time-buckets.

Why aggregate in the dashboard process rather than in the gateway?

* The gateway already exposes the raw audit list — no additional
  contract surface needed.
* Bucketing logic lives close to the SPA so the developer can iterate on
  the chart shape without redeploying the gateway.
* Once Prometheus is wired up the SPA can switch to PromQL for the same
  curves; until then this file is the bridge.

Public surface:

* :func:`build_timeseries` — takes a list of audit events + the requested
  metric/window/buckets and returns a JSON-serialisable payload matching
  the canonical shape documented in ``docs/observability.md``.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any


# How many ``GET /v1/audit`` events we ever pull when computing a window.
# 24h * 60 invocations/min ≈ 1.4M is theoretically possible — operators
# would scrape Prometheus for that. The dashboard is for small-to-medium
# deployments; the gateway's ``audit`` endpoint also caps at 10000 and
# we let it return whatever it has.
_DEFAULT_FETCH_LIMIT = 10000

# Windows we accept on the wire. Anything else → InvalidArguments. Keys
# match the contract; values are the (timedelta, default-bucket-count) pair.
_WINDOWS: dict[str, tuple[timedelta, int]] = {
    "1h": (timedelta(hours=1), 60),  # 1-minute buckets
    "24h": (timedelta(hours=24), 24),  # 1-hour buckets
    "7d": (timedelta(days=7), 168),  # 1-hour buckets
}

_METRICS = {
    "cost": "USD",
    "latency_p99": "ms",
    "error_rate": "percent",
    "cache_hit_ratio": "percent",
    "active_workers": "count",
    "tool_calls_per_minute": "count",
}


class InvalidWindowError(ValueError):
    """Raised when ``window=`` doesn't match the documented set."""


class InvalidMetricError(ValueError):
    """Raised when ``metric=`` doesn't match the documented set."""


def _parse_dt(value: Any) -> datetime | None:
    """Parse an ISO-8601 timestamp tolerant of trailing ``Z``."""
    if value is None:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    s = str(value).replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _percentile(values: list[float], percentile: float) -> float:
    """Compute the ``percentile``-th percentile of a value list (linear interpolation).

    For an empty list returns 0.0. ``percentile`` is given in 0-100. We
    implement this by hand to avoid pulling numpy into the dashboard runtime.
    """

    if not values:
        return 0.0
    sorted_vals = sorted(values)
    if percentile <= 0:
        return float(sorted_vals[0])
    if percentile >= 100:
        return float(sorted_vals[-1])
    k = (len(sorted_vals) - 1) * (percentile / 100.0)
    f = int(k)
    c = min(f + 1, len(sorted_vals) - 1)
    if f == c:
        return float(sorted_vals[f])
    return float(sorted_vals[f] + (sorted_vals[c] - sorted_vals[f]) * (k - f))


def build_timeseries(
    events: list[dict[str, Any]],
    *,
    metric: str,
    window: str = "24h",
    buckets: int | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Aggregate ``events`` into a time-series payload.

    Args:
        events: Raw audit events from ``GET /v1/audit``. Each event must
            carry ``timestamp`` (ISO-8601) and the metric-specific fields:
            ``cost_estimate_usd`` for cost, ``duration_ms`` for latency,
            ``error`` (truthy) for error rate, ``cached`` for cache hit
            ratio.
        metric: One of ``cost | latency_p99 | error_rate | cache_hit_ratio
            | active_workers | tool_calls_per_minute``.
        window: One of ``1h | 24h | 7d``.
        buckets: Optional bucket count override; defaults to the window's
            canonical value (60 for 1h, 24 for 24h, 168 for 7d).
        now: Inject for tests; defaults to ``datetime.now(timezone.utc)``.

    Returns:
        A dict matching the contract shape::

            {
              "metric": "...",
              "window": "...",
              "buckets": N,
              "points": [{"t": "...", "value": ...}, ...],
              "unit": "...",
              "summary": {"min": ..., "max": ..., "avg": ...}
            }

    Raises:
        InvalidMetricError: ``metric`` is not in the known set.
        InvalidWindowError: ``window`` is not in the known set.
    """

    if metric not in _METRICS:
        raise InvalidMetricError(f"unknown metric {metric!r}")
    if window not in _WINDOWS:
        raise InvalidWindowError(f"unknown window {window!r}")

    delta, default_buckets = _WINDOWS[window]
    n_buckets = int(buckets) if buckets and buckets > 0 else default_buckets
    n_buckets = max(1, min(n_buckets, 1000))

    now = now or datetime.now(timezone.utc)
    # Floor to the second so bucket math is deterministic.
    now = now.replace(microsecond=0)
    start = now - delta
    bucket_size = delta / n_buckets

    # Group event timestamps into bucket index 0..N-1.
    grouped: list[list[dict[str, Any]]] = [[] for _ in range(n_buckets)]
    for evt in events or []:
        ts = _parse_dt(evt.get("timestamp"))
        if ts is None or ts < start or ts > now:
            continue
        offset = (ts - start).total_seconds()
        bucket_seconds = bucket_size.total_seconds() or 1.0
        idx = int(offset / bucket_seconds)
        if idx >= n_buckets:
            idx = n_buckets - 1
        if idx < 0:
            idx = 0
        grouped[idx].append(evt)

    # Reduce each bucket to a scalar according to the metric.
    points: list[dict[str, Any]] = []
    values_for_summary: list[float] = []
    for i in range(n_buckets):
        bucket_events = grouped[i]
        # Bucket midpoint timestamp keeps the curve centred on its bucket.
        # The contract examples use the bucket *start*; we follow that for
        # the time being so dashboards can label cleanly with hours.
        t = start + bucket_size * i
        value = _bucket_value(metric, bucket_events)
        points.append(
            {"t": t.isoformat().replace("+00:00", "Z"), "value": _round_value(value)}
        )
        values_for_summary.append(value)

    summary = _summarise(values_for_summary)

    return {
        "metric": metric,
        "window": window,
        "buckets": n_buckets,
        "points": points,
        "unit": _METRICS[metric],
        "summary": summary,
    }


def _bucket_value(metric: str, events: list[dict[str, Any]]) -> float:
    """Compute a single value for a single bucket."""

    if not events:
        return 0.0

    if metric == "cost":
        return float(sum(float(e.get("cost_estimate_usd") or 0.0) for e in events))
    if metric == "tool_calls_per_minute":
        return float(len(events))
    if metric == "latency_p99":
        return _percentile(
            [float(e.get("duration_ms") or 0) for e in events],
            99.0,
        )
    if metric == "error_rate":
        errors = sum(1 for e in events if e.get("error"))
        return 100.0 * errors / float(len(events))
    if metric == "cache_hit_ratio":
        cached = sum(1 for e in events if bool(e.get("cached")))
        return 100.0 * cached / float(len(events))
    if metric == "active_workers":
        # Approximate from distinct ``agent.id``/``agent_id`` values seen
        # in the bucket. The gateway's audit log is the closest signal we
        # have to "active workers" — Prometheus would give a real gauge
        # but until then this is the best dashboard view.
        agents = set()
        for evt in events:
            agent = evt.get("agent_id") or evt.get("agent.id")
            if agent:
                agents.add(str(agent))
        return float(len(agents))
    return 0.0


def _round_value(v: float) -> float:
    """Round to 6 decimals so JSON stays small + diff-friendly."""
    return round(float(v), 6)


def _summarise(values: list[float]) -> dict[str, float]:
    """Min/max/avg over the bucket values.

    Empty / all-zero windows still produce a stable shape so the SPA can
    safely access ``summary.min`` etc.
    """
    if not values:
        return {"min": 0.0, "max": 0.0, "avg": 0.0}
    n = len(values)
    return {
        "min": _round_value(min(values)),
        "max": _round_value(max(values)),
        "avg": _round_value(sum(values) / n),
    }


__all__ = [
    "InvalidMetricError",
    "InvalidWindowError",
    "build_timeseries",
]
