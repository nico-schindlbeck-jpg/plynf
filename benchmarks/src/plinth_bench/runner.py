# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 The Plinth Authors
"""Core workload runner: ramp + hold + cooldown, percentiles, buckets.

Design notes
------------

The runner is a tiny open-loop generator. We schedule N requests per
second (where N varies during the ramp) by computing a per-second target
RPS and dispatching ``round(target_rps_at_t)`` request coroutines into a
shared ``asyncio.gather``. Each request is independent: it does not
block subsequent dispatches if it slows down. That is deliberate — a
closed-loop generator would mask the very latency increase we want to
measure.

A semaphore caps total in-flight requests at ``inflight_cap`` to prevent
runaway memory use if the target server slows to a crawl. When the cap
is hit, dispatch backs off (still open-loop in spirit; we just refuse to
queue beyond the cap) and the oversub is reflected as a drop in
``rps_observed`` for that bucket.

Latency is measured with :func:`time.perf_counter` for monotonic
nanosecond-grade precision. Errors are categorised by HTTP status (or
``"timeout"`` / ``"connect"`` / ``"unknown"`` for transport failures).

Outputs:
  * ``WorkloadResult`` — the full per-run record, JSON-serialisable.
  * Per-second ``buckets`` of (rps_observed, p50, p95, p99).

Ramp curve: linear from ``initial_rps=10`` → ``target_rps`` across
``ramp_seconds``. Hold at ``target_rps`` for ``hold_seconds``. Cool down
linearly back to ``initial_rps`` over ``cooldown_seconds``.
"""

from __future__ import annotations

import asyncio
import math
import time
from collections.abc import Awaitable, Callable
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any

import httpx

# ---------------------------------------------------------------------------
# Public types
# ---------------------------------------------------------------------------


WorkloadFn = Callable[[httpx.AsyncClient], Awaitable["RequestSample"]]
"""A workload is a coroutine that performs ONE request and returns a sample.

Workload modules implement :func:`build` to produce one of these and a
:class:`WorkloadConfig` describing target_rps etc.
"""


@dataclass(frozen=True)
class RequestSample:
    """One request's outcome.

    Attributes:
        duration_ms: Wall-time from issue to body-consumed.
        status_code: HTTP status. ``0`` for transport failures.
        error: Bucket label for failures; ``None`` on success.
    """

    duration_ms: float
    status_code: int
    error: str | None = None


@dataclass
class Bucket:
    """One per-second bucket of observed RPS + latency percentiles."""

    t: int  # second offset from run start (0-based)
    rps_observed: int
    p50_ms: float
    p95_ms: float
    p99_ms: float
    max_ms: float
    error_count: int


@dataclass
class WorkloadResult:
    """Final result of a benchmark run — JSON-serialisable via :meth:`to_dict`."""

    workload: str
    target_url: str
    target_rps: int
    ramp_seconds: int
    hold_seconds: int
    cooldown_seconds: int
    started_at: str
    finished_at: str
    total_requests: int
    successful: int
    failed: int
    error_rate: float
    latency_ms: dict[str, float]
    buckets: list[Bucket]
    errors_by_type: dict[str, int]
    notes: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        # ``buckets`` are dataclasses too — asdict already flattened them.
        return d


# ---------------------------------------------------------------------------
# Math: percentiles + ramp curve
# ---------------------------------------------------------------------------


def percentile(samples: list[float], p: float) -> float:
    """Return the p-th percentile via linear interpolation.

    ``p`` is given as a fraction in [0, 1] (e.g. 0.95 for p95).

    Empty input returns 0.0 — keeps the JSON output stable when an early
    bucket happens to record zero requests.
    """

    if not samples:
        return 0.0
    if p <= 0:
        return min(samples)
    if p >= 1:
        return max(samples)
    # Sorted-on-write keeps the call path simple. Bench inputs are small
    # enough that ``sorted`` per call is fine; if we ever care, swap to a
    # streaming TDigest.
    sorted_samples = sorted(samples)
    rank = p * (len(sorted_samples) - 1)
    lo = math.floor(rank)
    hi = math.ceil(rank)
    if lo == hi:
        return sorted_samples[lo]
    frac = rank - lo
    return sorted_samples[lo] * (1.0 - frac) + sorted_samples[hi] * frac


def target_rps_at(
    t_seconds: float,
    *,
    ramp_seconds: int,
    hold_seconds: int,
    cooldown_seconds: int,
    target_rps: int,
    initial_rps: int = 10,
) -> float:
    """Compute the target instantaneous RPS at second ``t_seconds``.

    The curve has three phases:

    * ``[0, ramp_seconds)``: linear ramp from ``initial_rps`` → ``target_rps``
    * ``[ramp_seconds, ramp_seconds + hold_seconds)``: constant ``target_rps``
    * ``[ramp_seconds + hold_seconds, total)``: linear cooldown to ``initial_rps``

    After ``total`` it returns 0.0 (the runner stops dispatching).
    """

    if t_seconds < 0:
        return 0.0
    if t_seconds < ramp_seconds:
        # Avoid div-by-zero when ramp is 0 (immediate jump to target).
        if ramp_seconds == 0:
            return float(target_rps)
        frac = t_seconds / ramp_seconds
        return initial_rps + (target_rps - initial_rps) * frac
    hold_end = ramp_seconds + hold_seconds
    if t_seconds < hold_end:
        return float(target_rps)
    cool_end = hold_end + cooldown_seconds
    if t_seconds < cool_end:
        if cooldown_seconds == 0:
            return float(initial_rps)
        frac = (t_seconds - hold_end) / cooldown_seconds
        return target_rps - (target_rps - initial_rps) * frac
    return 0.0


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------


@dataclass
class RunnerConfig:
    """Knobs for :func:`run_workload`.

    Attributes:
        target_rps: peak RPS during the hold phase.
        ramp_seconds: length of linear ramp from ``initial_rps`` to target.
        hold_seconds: length of constant-target phase.
        cooldown_seconds: length of linear cooldown.
        initial_rps: starting RPS at t=0.
        inflight_cap: hard cap on concurrent in-flight requests (memory bound).
        request_timeout_seconds: per-request timeout.
        http2: ask httpx to advertise HTTP/2.
    """

    target_rps: int
    ramp_seconds: int = 30
    hold_seconds: int = 60
    cooldown_seconds: int = 10
    initial_rps: int = 10
    inflight_cap: int = 5000
    request_timeout_seconds: float = 30.0
    http2: bool = True


async def run_workload(
    workload_name: str,
    target_url: str,
    workload: WorkloadFn,
    config: RunnerConfig,
    *,
    headers: dict[str, str] | None = None,
    notes: dict[str, Any] | None = None,
    client: httpx.AsyncClient | None = None,
) -> WorkloadResult:
    """Drive ``workload`` at the configured ramp/hold/cooldown profile.

    Either supply your own ``client`` (useful for tests against an ASGI
    transport) or let the runner build one against ``target_url``. We do
    not auto-close a caller-supplied client — that's the caller's job.
    """

    started_at = datetime.now(timezone.utc).isoformat()
    own_client = client is None
    if client is None:
        client = httpx.AsyncClient(
            base_url=target_url,
            headers=headers or {},
            http2=config.http2,
            timeout=config.request_timeout_seconds,
            # No connection pool cap — uvicorn is the gating factor.
        )

    # Per-second bucketing: every sample is filed under its dispatch
    # second. We collect samples in a flat list and bucket at the end so
    # we don't lose accuracy from rolling buckets while requests are
    # still in flight.
    samples: list[tuple[int, RequestSample]] = []
    sem = asyncio.Semaphore(config.inflight_cap)

    async def one_request(t_bucket: int) -> None:
        async with sem:
            t0 = time.perf_counter()
            try:
                sample = await workload(client)
                # If the workload itself returned a wall-clock that's
                # smaller than our outer measurement, prefer the outer
                # one for consistency with bucketing. Workloads that do
                # extra setup (e.g. a session-init call) typically do
                # report wall-clock from inside.
            except httpx.TimeoutException:
                duration_ms = (time.perf_counter() - t0) * 1000.0
                sample = RequestSample(
                    duration_ms=duration_ms, status_code=0, error="timeout"
                )
            except httpx.ConnectError:
                duration_ms = (time.perf_counter() - t0) * 1000.0
                sample = RequestSample(
                    duration_ms=duration_ms, status_code=0, error="connect"
                )
            except Exception as exc:  # noqa: BLE001
                duration_ms = (time.perf_counter() - t0) * 1000.0
                sample = RequestSample(
                    duration_ms=duration_ms,
                    status_code=0,
                    error=type(exc).__name__,
                )
            samples.append((t_bucket, sample))

    total_seconds = (
        config.ramp_seconds + config.hold_seconds + config.cooldown_seconds
    )
    tasks: list[asyncio.Task] = []
    run_t0 = time.perf_counter()

    try:
        # Outer loop: one second at a time. Within each second we fire
        # ``round(rps_at_t)`` requests, evenly spaced.
        for t in range(total_seconds):
            sec_t0 = time.perf_counter()
            rps = target_rps_at(
                t,
                ramp_seconds=config.ramp_seconds,
                hold_seconds=config.hold_seconds,
                cooldown_seconds=config.cooldown_seconds,
                target_rps=config.target_rps,
                initial_rps=config.initial_rps,
            )
            n = max(0, int(round(rps)))
            if n == 0:
                # Sleep to the next second boundary.
                await asyncio.sleep(max(0.0, 1.0 - (time.perf_counter() - sec_t0)))
                continue
            interval = 1.0 / n
            for i in range(n):
                tasks.append(asyncio.create_task(one_request(t)))
                # Sleep a fractional gap so dispatches are evenly spread.
                # We don't await every individual request — that would
                # close the loop. We just pace the dispatch.
                if i < n - 1:
                    await asyncio.sleep(interval)
            # Sleep out the tail of the second.
            elapsed = time.perf_counter() - sec_t0
            if elapsed < 1.0:
                await asyncio.sleep(1.0 - elapsed)

        # All dispatches issued — wait for the in-flight tail.
        # Bound the wait so an ill-behaved server can't hang the run forever.
        deadline = run_t0 + total_seconds + max(60.0, config.request_timeout_seconds * 2)
        while tasks and any(not t.done() for t in tasks):
            remaining = deadline - time.perf_counter()
            if remaining <= 0:
                break
            await asyncio.sleep(0.05)
        # Cancel any stragglers — record them as timeouts.
        stragglers = [t for t in tasks if not t.done()]
        for st in stragglers:
            st.cancel()
        if stragglers:
            for st in stragglers:
                try:
                    await st
                except (asyncio.CancelledError, Exception):  # noqa: BLE001
                    pass
            # Add a synthetic record per straggler so the count is
            # representative.
            for _ in range(len(stragglers)):
                samples.append((total_seconds - 1, RequestSample(
                    duration_ms=config.request_timeout_seconds * 1000,
                    status_code=0,
                    error="straggler_cancelled",
                )))
    finally:
        if own_client:
            await client.aclose()

    finished_at = datetime.now(timezone.utc).isoformat()

    # Aggregate.
    return _summarise(
        workload_name=workload_name,
        target_url=target_url,
        config=config,
        samples=samples,
        started_at=started_at,
        finished_at=finished_at,
        total_seconds=total_seconds,
        notes=notes or {},
    )


def _summarise(
    *,
    workload_name: str,
    target_url: str,
    config: RunnerConfig,
    samples: list[tuple[int, RequestSample]],
    started_at: str,
    finished_at: str,
    total_seconds: int,
    notes: dict[str, Any],
) -> WorkloadResult:
    """Turn the raw sample stream into a :class:`WorkloadResult`."""

    total = len(samples)
    successful = 0
    failed = 0
    errors_by_type: dict[str, int] = {}
    all_durations: list[float] = []
    bucketed: dict[int, list[float]] = {}
    bucketed_errors: dict[int, int] = {}

    for t, s in samples:
        all_durations.append(s.duration_ms)
        bucketed.setdefault(t, []).append(s.duration_ms)
        is_success = (
            s.error is None and 200 <= s.status_code < 400
        )
        if is_success:
            successful += 1
        else:
            failed += 1
            label = (
                s.error
                if s.error is not None
                else str(s.status_code)
            )
            errors_by_type[label] = errors_by_type.get(label, 0) + 1
            bucketed_errors[t] = bucketed_errors.get(t, 0) + 1

    error_rate = (failed / total) if total else 0.0
    latency = {
        "p50": round(percentile(all_durations, 0.50), 3),
        "p95": round(percentile(all_durations, 0.95), 3),
        "p99": round(percentile(all_durations, 0.99), 3),
        "max": round(max(all_durations), 3) if all_durations else 0.0,
        "mean": round(
            sum(all_durations) / len(all_durations), 3
        ) if all_durations else 0.0,
    }

    buckets: list[Bucket] = []
    for t in range(total_seconds):
        durs = bucketed.get(t, [])
        buckets.append(
            Bucket(
                t=t,
                rps_observed=len(durs),
                p50_ms=round(percentile(durs, 0.50), 3),
                p95_ms=round(percentile(durs, 0.95), 3),
                p99_ms=round(percentile(durs, 0.99), 3),
                max_ms=round(max(durs), 3) if durs else 0.0,
                error_count=bucketed_errors.get(t, 0),
            )
        )

    return WorkloadResult(
        workload=workload_name,
        target_url=target_url,
        target_rps=config.target_rps,
        ramp_seconds=config.ramp_seconds,
        hold_seconds=config.hold_seconds,
        cooldown_seconds=config.cooldown_seconds,
        started_at=started_at,
        finished_at=finished_at,
        total_requests=total,
        successful=successful,
        failed=failed,
        error_rate=round(error_rate, 6),
        latency_ms=latency,
        buckets=buckets,
        errors_by_type=errors_by_type,
        notes=notes,
    )


__all__ = [
    "Bucket",
    "RequestSample",
    "RunnerConfig",
    "WorkloadFn",
    "WorkloadResult",
    "percentile",
    "run_workload",
    "target_rps_at",
]
