"""Runs every workflow scenario and emits a comparison table + JSON.

Usage:
    python -m benchmarks.workflows.run_all
    python -m benchmarks.workflows.run_all --markdown        # MD output
    python -m benchmarks.workflows.run_all --no-write        # don't save JSON
"""

from __future__ import annotations

import argparse
import dataclasses
import datetime as _dt
import json
import sys
from pathlib import Path

from .harness import (
    INPUT_COST_PER_MTOK,
    OUTPUT_COST_PER_MTOK,
    ScenarioResult,
    run_all,
)
from .scenarios import ALL_SCENARIOS

REPO_ROOT = Path(__file__).resolve().parents[2]
RESULTS_DIR = REPO_ROOT / "benchmarks" / "workflows" / "results"


def _percent(x: float) -> str:
    return f"{x * 100:>5.1f}%"


def _fmt_int(n: int) -> str:
    return f"{n:>10,}"


def _fmt_usd(amount: float) -> str:
    return f"${amount:>7.3f}"


def render_text(results: list[ScenarioResult]) -> str:
    """ASCII table for terminal output."""
    lines = []
    lines.append("═" * 102)
    lines.append("  PLINTH — Workflow Token Benchmark Suite")
    lines.append("  Generated: " + _dt.datetime.now().isoformat(timespec="seconds"))
    lines.append("  Pricing:   Claude Sonnet 4.5 — $%.2f/M input, $%.2f/M output" % (
        INPUT_COST_PER_MTOK, OUTPUT_COST_PER_MTOK))
    lines.append("═" * 102)
    lines.append("")
    lines.append(
        f"  {'Scenario':<34} {'Baseline':>12} {'Plinth':>12} {'Saved':>8}   "
        f"{'$base':>8} {'$plinth':>8}"
    )
    lines.append("  " + "─" * 100)
    for r in results:
        title = r.scenario.title
        if len(title) > 33:
            title = title[:30] + "..."
        lines.append(
            f"  {title:<34} {r.baseline_total:>12,} {r.plinth_total:>12,}  "
            f"{_percent(r.reduction_pct)}   "
            f"{_fmt_usd(r.cost_baseline_usd)} {_fmt_usd(r.cost_plinth_usd)}"
        )
    lines.append("  " + "─" * 100)

    # Aggregate totals
    total_base = sum(r.baseline_total for r in results)
    total_plinth = sum(r.plinth_total for r in results)
    avg_reduction = (
        sum(r.reduction_pct for r in results) / len(results) if results else 0.0
    )
    total_cost_saved = sum(r.cost_saved_usd for r in results)
    lines.append(
        f"  {'Σ across all scenarios':<34} {total_base:>12,} {total_plinth:>12,}  "
        f"{_percent(avg_reduction)}   "
        f"{_fmt_usd(sum(r.cost_baseline_usd for r in results))} "
        f"{_fmt_usd(sum(r.cost_plinth_usd for r in results))}"
    )
    lines.append("═" * 102)
    lines.append("")
    lines.append(f"  Average reduction across scenarios: {_percent(avg_reduction).strip()}")
    lines.append(
        f"  Total cost saved (one run of each scenario): {_fmt_usd(total_cost_saved).strip()}"
    )
    lines.append("")
    lines.append("  Personas covered:")
    for r in results:
        lines.append(
            f"    • {r.scenario.persona:<40} → {_percent(r.reduction_pct).strip()} reduction"
        )
    lines.append("")
    return "\n".join(lines)


def render_markdown(results: list[ScenarioResult]) -> str:
    """Markdown for embedding in docs / PDFs / blog posts."""
    rows = []
    rows.append("# Workflow Benchmark Results")
    rows.append("")
    rows.append(f"Generated: `{_dt.datetime.now().isoformat(timespec='seconds')}`")
    rows.append("")
    rows.append("Pricing model: Claude Sonnet 4.5 — $3/M input, $15/M output (May 2026 list).")
    rows.append("")
    rows.append("## Per-scenario results")
    rows.append("")
    rows.append(
        "| Scenario | Persona | Baseline tokens | Plinth tokens | Reduction | $ baseline | $ plinth | $ saved |"
    )
    rows.append("|---|---|---:|---:|---:|---:|---:|---:|")
    for r in results:
        rows.append(
            f"| {r.scenario.title} | {r.scenario.persona} | "
            f"{r.baseline_total:,} | {r.plinth_total:,} | "
            f"**{_percent(r.reduction_pct).strip()}** | "
            f"${r.cost_baseline_usd:.3f} | ${r.cost_plinth_usd:.3f} | "
            f"${r.cost_saved_usd:.3f} |"
        )

    # Aggregate
    avg_reduction = (
        sum(r.reduction_pct for r in results) / len(results) if results else 0.0
    )
    total_saved = sum(r.cost_saved_usd for r in results)
    rows.append(
        f"| **Average** | — | — | — | **{_percent(avg_reduction).strip()}** | — | — | "
        f"**${total_saved:.3f}** |"
    )
    rows.append("")
    rows.append("## Methodology")
    rows.append("")
    rows.append("See `benchmarks/workflows/MODEL.md` for the token-accounting model.")
    rows.append("")
    rows.append("## Notes per scenario")
    rows.append("")
    for r in results:
        rows.append(f"### {r.scenario.title}")
        rows.append("")
        rows.append(r.scenario.description)
        if r.scenario.notes:
            rows.append("")
            for n in r.scenario.notes:
                rows.append(f"- {n}")
        rows.append("")
    return "\n".join(rows)


def to_json(results: list[ScenarioResult]) -> dict:
    """Canonical JSON shape for downstream tools."""
    return {
        "schema": "plinth.workflow_benchmark/v1",
        "generated": _dt.datetime.now().isoformat(timespec="seconds"),
        "pricing": {
            "model": "claude-sonnet-4.5",
            "input_per_mtok_usd": INPUT_COST_PER_MTOK,
            "output_per_mtok_usd": OUTPUT_COST_PER_MTOK,
            "as_of": "2026-05",
        },
        "scenarios": [
            {
                "id": r.scenario.id,
                "title": r.scenario.title,
                "persona": r.scenario.persona,
                "description": r.scenario.description,
                "notes": list(r.scenario.notes),
                "baseline_input_tokens": r.baseline_total_input,
                "baseline_output_tokens": r.baseline_total_output,
                "baseline_total_tokens": r.baseline_total,
                "plinth_input_tokens": r.plinth_total_input,
                "plinth_output_tokens": r.plinth_total_output,
                "plinth_total_tokens": r.plinth_total,
                "reduction_pct": round(r.reduction_pct * 100, 2),
                "cost_baseline_usd": round(r.cost_baseline_usd, 6),
                "cost_plinth_usd": round(r.cost_plinth_usd, 6),
                "cost_saved_usd": round(r.cost_saved_usd, 6),
                "steps": [dataclasses.asdict(s) for s in r.steps],
            }
            for r in results
        ],
        "aggregate": {
            "n_scenarios": len(results),
            "avg_reduction_pct": round(
                100 * sum(r.reduction_pct for r in results) / max(1, len(results)),
                2,
            ),
            "total_cost_baseline_usd": round(
                sum(r.cost_baseline_usd for r in results), 4
            ),
            "total_cost_plinth_usd": round(
                sum(r.cost_plinth_usd for r in results), 4
            ),
            "total_cost_saved_usd": round(
                sum(r.cost_saved_usd for r in results), 4
            ),
        },
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--markdown", action="store_true", help="Output Markdown")
    parser.add_argument(
        "--no-write", action="store_true", help="Don't write JSON to results/"
    )
    parser.add_argument(
        "--quiet", action="store_true", help="Only emit JSON path, no table"
    )
    args = parser.parse_args()

    results = run_all(ALL_SCENARIOS)

    # Sanity guard — fail loudly if any scenario crosses the 85% threshold
    # without being explicitly justified in its notes.
    for r in results:
        if r.reduction_pct > 0.85:
            note_says_explained = any(
                "85%" in n or "extreme" in n.lower() or "outlier" in n.lower()
                for n in r.scenario.notes
            )
            if not note_says_explained:
                print(
                    f"⚠ Scenario '{r.scenario.id}' reports "
                    f"{r.reduction_pct * 100:.1f}% — exceeds 85% sanity floor. "
                    f"Add a note explaining why this is realistic.",
                    file=sys.stderr,
                )

    if args.markdown:
        print(render_markdown(results))
    elif not args.quiet:
        print(render_text(results))

    if not args.no_write:
        RESULTS_DIR.mkdir(parents=True, exist_ok=True)
        ts = _dt.datetime.now().strftime("%Y%m%d-%H%M%S")
        out = RESULTS_DIR / f"{ts}-suite.json"
        out.write_text(json.dumps(to_json(results), indent=2))
        if args.quiet:
            print(str(out))
        else:
            print(f"\n  → JSON report: {out.relative_to(REPO_ROOT)}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
