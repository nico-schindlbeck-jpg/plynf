# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 The Plinth Authors
"""``plinth-bench`` — stress benchmarks for the Plinth services.

A lightweight load-generator built on ``asyncio`` + ``httpx`` rather than
pulling in locust or k6. The runner ramps RPS from 10 → target, holds at
target, and cools down — capturing every request's latency and status so
we can produce credible p50/p95/p99 numbers and per-second buckets.

Public API:

* :class:`runner.WorkloadResult` — the JSON-serialisable result object.
* :func:`runner.run_workload` — drive a registered workload at a target RPS.
* :func:`reporter.write_json` / :func:`compare.compare_runs` — render results.
"""

from __future__ import annotations

__version__ = "0.5.0"
