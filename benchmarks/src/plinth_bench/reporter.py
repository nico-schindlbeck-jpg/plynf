# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 The Plinth Authors
"""JSON + markdown serialisation for :class:`runner.WorkloadResult`."""

from __future__ import annotations

import json
from pathlib import Path

from .runner import WorkloadResult


def to_dict(result: WorkloadResult) -> dict:
    """Return the canonical dict shape (used both for JSON + comparisons)."""

    return result.to_dict()


def write_json(result: WorkloadResult, path: Path) -> Path:
    """Write a single result to ``path`` as JSON. Creates parent dirs."""

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(to_dict(result), indent=2))
    return path


def render_markdown_table(results: list[WorkloadResult]) -> str:
    """Render a single-line-per-workload markdown summary table.

    Suitable for pasting into the README's "Performance" section.
    """

    lines = [
        "| Workload | RPS | p50 | p95 | p99 | error_rate |",
        "|----------|----:|----:|----:|----:|-----------:|",
    ]
    for r in results:
        lines.append(
            f"| {r.workload:21s} "
            f"| {r.target_rps:>5d} "
            f"| {r.latency_ms['p50']:>6.2f} ms "
            f"| {r.latency_ms['p95']:>6.2f} ms "
            f"| {r.latency_ms['p99']:>6.2f} ms "
            f"| {r.error_rate * 100:>5.2f}% |"
        )
    return "\n".join(lines) + "\n"


__all__ = ["render_markdown_table", "to_dict", "write_json"]
