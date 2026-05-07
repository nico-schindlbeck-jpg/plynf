# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 The Plinth Authors
"""Compare two benchmark runs and produce a markdown table.

Input shape is whatever :func:`reporter.write_json` produces (or a list
of those dicts when comparing whole suites).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any


def _percent_delta(baseline: float, candidate: float) -> str:
    """Return a signed percentage delta with ``+/-`` prefix.

    Treats a zero-baseline as ``n/a`` (avoids ``inf%`` in output).
    """

    if baseline == 0:
        return "  n/a"
    pct = (candidate - baseline) / baseline * 100.0
    return f"{pct:+6.1f}%"


def _read_runs(path: Path) -> list[dict[str, Any]]:
    """Read either a single-run JSON or a list of runs JSON."""

    data = json.loads(path.read_text())
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        return [data]
    raise ValueError(f"Unexpected JSON shape in {path}: {type(data).__name__}")


def compare_runs(baseline_path: Path, candidate_path: Path) -> str:
    """Return a markdown table comparing baseline → candidate.

    Files may be either single-run JSON or a list of runs (suite). When
    both inputs contain the same workload, that row appears in the
    output. Workloads present in only one input are appended at the end
    with the missing side blank.
    """

    base_runs = {r["workload"]: r for r in _read_runs(baseline_path)}
    cand_runs = {r["workload"]: r for r in _read_runs(candidate_path)}
    workloads_in_order: list[str] = []
    seen: set[str] = set()
    for r in _read_runs(baseline_path):
        wl = r["workload"]
        if wl not in seen:
            workloads_in_order.append(wl)
            seen.add(wl)
    for r in _read_runs(candidate_path):
        wl = r["workload"]
        if wl not in seen:
            workloads_in_order.append(wl)
            seen.add(wl)

    lines = [
        "| Workload         | Metric  | Baseline | Run B    | Δ       |",
        "|------------------|---------|---------:|---------:|--------:|",
    ]
    for wl in workloads_in_order:
        b = base_runs.get(wl)
        c = cand_runs.get(wl)
        for metric in ("p50", "p95", "p99"):
            b_val = (b or {}).get("latency_ms", {}).get(metric)
            c_val = (c or {}).get("latency_ms", {}).get(metric)
            b_str = f"{b_val:>7.2f}" if b_val is not None else "    -  "
            c_str = f"{c_val:>7.2f}" if c_val is not None else "    -  "
            if b_val is not None and c_val is not None:
                delta = _percent_delta(b_val, c_val)
            else:
                delta = "    -  "
            lines.append(
                f"| {wl:16s} | {metric + ' ms':<7s} | {b_str}  | {c_str}  | {delta} |"
            )
        # Error rate row — useful for spotting regressions.
        b_err = (b or {}).get("error_rate")
        c_err = (c or {}).get("error_rate")
        b_str = f"{b_err * 100:>6.2f}%" if b_err is not None else "    -  "
        c_str = f"{c_err * 100:>6.2f}%" if c_err is not None else "    -  "
        if b_err is not None and c_err is not None:
            if b_err == 0 and c_err == 0:
                delta = "  0.0pp"
            else:
                delta = f"{(c_err - b_err) * 100:+5.2f}pp"
        else:
            delta = "    -  "
        lines.append(
            f"| {wl:16s} | err_pct | {b_str}  | {c_str}  | {delta} |"
        )

    return "\n".join(lines) + "\n"


__all__ = ["compare_runs"]
