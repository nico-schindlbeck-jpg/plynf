# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 The Plinth Authors
"""Run baseline and Plinth side-by-side and print the comparison.

This is the headline demo entry point. It runs both agents on the same
topic, in the same mode, with the same LLM mock, and prints the token
comparison table that's quoted on the project README. A JSON report is
written to ``reports/<timestamp>-comparison.json`` for downstream
analysis or regression tracking.

Usage::

    python compare.py
    python compare.py --topic "renewable energy"
    python compare.py --topic "ai agents" --mode simulation
    ANTHROPIC_API_KEY=... python compare.py --mode live --topic "..."
"""

from __future__ import annotations

import argparse
import datetime as _dt
import json
import os
import sys
from typing import Any

from rich.console import Console
from rich.table import Table

from baseline import run_baseline
from shared import ResearchReport, load_topics_config, services_available
from with_plinth import run_with_plinth


# ---------------------------------------------------------------------------
# Reporting helpers
# ---------------------------------------------------------------------------


def _percent(reduction: float) -> str:
    return f"{reduction * 100:.1f} %"


def _format_table(
    baseline: ResearchReport,
    plinth: ResearchReport,
    *,
    topic: str,
    mode: str,
    report_path: str,
) -> str:
    """Build the headline comparison block as a single string."""
    if baseline.total_tokens > 0:
        token_reduction = 1.0 - (plinth.total_tokens / baseline.total_tokens)
    else:  # pragma: no cover — shouldn't happen
        token_reduction = 0.0
    cost_saved = baseline.total_cost_usd - plinth.total_cost_usd

    lines = []
    lines.append("═══════════════════════════════════════════════════════════════════")
    lines.append(f"  TOKEN-USAGE COMPARISON — research-agent on topic \"{topic}\"")
    lines.append("═══════════════════════════════════════════════════════════════════")
    lines.append(
        f"  Baseline (no Plinth):     {baseline.total_tokens:>9,} tokens   "
        f"|   ${baseline.total_cost_usd:.4f}"
    )
    lines.append(
        f"  With Plinth:              {plinth.total_tokens:>9,} tokens   "
        f"|   ${plinth.total_cost_usd:.4f}"
    )
    lines.append("  ─────────────────────────────────────────────")
    lines.append(
        f"  Reduction:                {_percent(token_reduction):>11}        "
        f"|   ${cost_saved:.4f} saved"
    )
    lines.append("═══════════════════════════════════════════════════════════════════")
    lines.append(
        f"  Wall-clock time:        Baseline {baseline.wall_clock_seconds:>5.1f} s   "
        f"|   Plinth {plinth.wall_clock_seconds:>5.1f} s"
    )
    lines.append(
        f"  Tool calls:             Baseline {baseline.tool_call_count:>5}   "
        f"|   Plinth {plinth.tool_call_count:>5}   "
        f"({plinth.cached_tool_calls} cached)"
    )
    lines.append("═══════════════════════════════════════════════════════════════════")
    lines.append(
        f"  Mode: {mode} | Topic: {topic}"
    )
    lines.append(
        f"  Baseline LLM calls: {baseline.llm_call_count} | "
        f"Plinth LLM calls: {plinth.llm_call_count}"
    )
    lines.append(f"  Report saved: {report_path}")
    return "\n".join(lines)


def _phase_for_step(step: str) -> str:
    """Bucket a per-step label into a coarse phase for the breakdown table."""
    if step.startswith("decide-search") or step == "search":
        return "search"
    if step.startswith("decide-fetch"):
        return "decide-fetch (per source)"
    if step.startswith("extract"):
        return "extract (per source)"
    if step.startswith("synthes"):
        return "synthesise"
    return step  # pragma: no cover - fall back


def _format_per_step_table(
    baseline: ResearchReport, plinth: ResearchReport, console: Console
) -> None:
    """Print a per-phase token-totals comparison.

    Both agents go through different step counts (baseline runs an
    extra ``decide-fetch`` reasoning per source), so a row-by-row
    index alignment would misalign the phases. Instead we bucket each
    LLM call into a phase (search / decide-fetch / extract / synthesise)
    and sum.
    """
    # Aggregate by phase.
    def _aggregate(calls: list) -> dict[str, dict[str, int]]:
        out: dict[str, dict[str, int]] = {}
        for c in calls:
            phase = _phase_for_step(c.step)
            entry = out.setdefault(phase, {"calls": 0, "tokens": 0})
            entry["calls"] += 1
            entry["tokens"] += c.prompt_tokens + c.response_tokens
        return out

    b_by_phase = _aggregate(baseline.llm_calls)
    p_by_phase = _aggregate(plinth.llm_calls)
    phases = ["search", "decide-fetch (per source)", "extract (per source)", "synthesise"]

    table = Table(
        title="Per-phase token totals",
        show_header=True,
        header_style="bold",
    )
    table.add_column("Phase")
    table.add_column("Baseline tokens", justify="right")
    table.add_column("Plinth tokens", justify="right")
    table.add_column("Δ (tokens)", justify="right")

    for phase in phases:
        b = b_by_phase.get(phase, {"calls": 0, "tokens": 0})
        p = p_by_phase.get(phase, {"calls": 0, "tokens": 0})
        delta = b["tokens"] - p["tokens"]
        table.add_row(
            f"{phase}  ({b['calls']}b / {p['calls']}p calls)",
            f"{b['tokens']:,}" if b["tokens"] else "-",
            f"{p['tokens']:,}" if p["tokens"] else "-",
            f"{delta:+,}",
        )
    # Totals row.
    table.add_row(
        "TOTAL",
        f"{baseline.total_tokens:,}",
        f"{plinth.total_tokens:,}",
        f"{baseline.total_tokens - plinth.total_tokens:+,}",
    )
    console.print(table)


# ---------------------------------------------------------------------------
# Reports directory
# ---------------------------------------------------------------------------


def _report_path() -> str:
    base = os.path.join(os.path.dirname(__file__), "reports")
    os.makedirs(base, exist_ok=True)
    timestamp = _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H-%M-%S")
    return os.path.join(base, f"{timestamp}-comparison.json")


def _save_report(
    path: str,
    *,
    baseline: ResearchReport,
    plinth: ResearchReport,
    topic: str,
    mode: str,
    services: dict[str, bool],
) -> None:
    """Write the full structured comparison to JSON."""
    if baseline.total_tokens > 0:
        token_reduction = 1.0 - (plinth.total_tokens / baseline.total_tokens)
    else:  # pragma: no cover
        token_reduction = 0.0

    payload: dict[str, Any] = {
        "schema_version": 1,
        "generated_at": _dt.datetime.now(_dt.timezone.utc).isoformat(),
        "topic": topic,
        "mode": mode,
        "services_available": services,
        "summary": {
            "baseline_total_tokens": baseline.total_tokens,
            "plinth_total_tokens": plinth.total_tokens,
            "token_reduction_pct": round(token_reduction * 100, 2),
            "baseline_cost_usd": round(baseline.total_cost_usd, 6),
            "plinth_cost_usd": round(plinth.total_cost_usd, 6),
            "cost_saved_usd": round(
                baseline.total_cost_usd - plinth.total_cost_usd, 6
            ),
            "baseline_tool_calls": baseline.tool_call_count,
            "plinth_tool_calls": plinth.tool_call_count,
            "plinth_cached_tool_calls": plinth.cached_tool_calls,
            "baseline_wall_clock_seconds": round(baseline.wall_clock_seconds, 4),
            "plinth_wall_clock_seconds": round(plinth.wall_clock_seconds, 4),
        },
        "baseline": baseline.to_dict(),
        "plinth": plinth.to_dict(),
    }
    with open(path, "w") as f:
        json.dump(payload, f, indent=2)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Run baseline vs. Plinth research agent and print a comparison."
    )
    parser.add_argument("--topic", default=None, help="Research topic.")
    parser.add_argument(
        "--mode",
        default="simulation",
        choices=["simulation", "mock-llm", "live"],
        help="LLM mode. Simulation uses a deterministic mock; live calls Anthropic.",
    )
    parser.add_argument(
        "--per-step",
        action="store_true",
        help="Print the per-step token breakdown table.",
    )
    args = parser.parse_args(argv)

    topic = args.topic or load_topics_config().get("default_topic", "renewable energy")
    mode = "simulation" if args.mode == "mock-llm" else args.mode

    console = Console()
    services = services_available()

    # Service-availability messaging (helpful but never blocking).
    if all(services.values()):
        console.print(
            "[green]All Plinth services reachable.[/green] "
            f"Workspace, gateway, and mock-mcp are running."
        )
    else:
        missing = [k for k, v in services.items() if not v]
        console.print(
            f"[yellow]Services not reachable:[/yellow] {', '.join(missing)}. "
            "Falling back to in-process fixtures + simulated gateway. "
            "This is fine for [bold]simulation[/bold] mode."
        )
        if "workspace" in missing or "gateway" in missing:
            console.print(
                "[dim]To run against real services: cd ../.. && make serve[/dim]"
            )

    if mode == "live" and not os.environ.get("ANTHROPIC_API_KEY"):
        console.print(
            "[yellow]ANTHROPIC_API_KEY not set; live mode would fail.[/yellow] "
            "Falling back to simulation."
        )
        mode = "simulation"

    console.print(f"[bold]Running baseline...[/bold] (topic={topic!r}, mode={mode})")
    baseline = run_baseline(topic, mode=mode)

    console.print(f"[bold]Running with Plinth...[/bold] (topic={topic!r}, mode={mode})")
    plinth = run_with_plinth(topic, mode=mode)

    report_path = _report_path()
    _save_report(
        report_path,
        baseline=baseline,
        plinth=plinth,
        topic=topic,
        mode=mode,
        services=services,
    )

    print()
    print(_format_table(
        baseline,
        plinth,
        topic=topic,
        mode=mode,
        report_path=os.path.relpath(report_path, os.path.dirname(__file__)),
    ))

    if args.per_step:
        print()
        _format_per_step_table(baseline, plinth, console)

    # Exit code reflects whether reduction was achieved (≥ 40%, the
    # quality bar from the demo spec). CI can use this to gate.
    if baseline.total_tokens > 0:
        reduction = 1.0 - (plinth.total_tokens / baseline.total_tokens)
        if reduction < 0.4:
            console.print(
                f"[red]Reduction {reduction * 100:.1f}% is below the 40% quality bar.[/red]"
            )
            return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
