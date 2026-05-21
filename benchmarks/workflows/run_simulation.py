"""Monte-Carlo runner — N perturbed runs per scenario.

Usage:
    python -m benchmarks.workflows.run_simulation                  # 100 × 8
    python -m benchmarks.workflows.run_simulation --runs 200
    python -m benchmarks.workflows.run_simulation --seed 999
    python -m benchmarks.workflows.run_simulation --markdown
"""

from __future__ import annotations

import argparse
import datetime as _dt
import json
import sys
from pathlib import Path

from .harness import INPUT_COST_PER_MTOK, OUTPUT_COST_PER_MTOK
from .scenarios import ALL_SCENARIOS
from .simulation import (
    DEFAULT_SEED,
    N_RUNS_PER_SCENARIO,
    SuiteSimulation,
    simulate_all,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
RESULTS_DIR = REPO_ROOT / "benchmarks" / "workflows" / "results"


def _pct(x: float) -> str:
    return f"{x * 100:.1f}%"


def _usd(x: float) -> str:
    return f"${x:,.2f}"


def render_text(suite: SuiteSimulation, runs_per: int, seed: int) -> str:
    lines = []
    lines.append("═" * 110)
    lines.append("  PLINTH — Workflow Token Benchmark · MONTE-CARLO SIMULATION")
    lines.append(f"  Generated:  {_dt.datetime.now().isoformat(timespec='seconds')}")
    lines.append(
        f"  Settings:   {runs_per} runs/scenario × {len(suite.sims)} scenarios = "
        f"{suite.total_runs} total runs · seed={seed}"
    )
    lines.append(
        f"  Pricing:    Claude Sonnet 4.5 — ${INPUT_COST_PER_MTOK:.2f}/M input, "
        f"${OUTPUT_COST_PER_MTOK:.2f}/M output"
    )
    lines.append("═" * 110)
    lines.append("")
    lines.append(
        f"  {'Scenario':<40} {'Mean':>7} {'Median':>7} {'p25':>7} {'p75':>7} "
        f"{'p95':>7} {'σ':>6} {'$ avg saved':>13}"
    )
    lines.append("  " + "─" * 108)
    for sim in suite.sims:
        title = sim.scenario_title
        if len(title) > 39:
            title = title[:36] + "..."
        lines.append(
            f"  {title:<40} "
            f"{_pct(sim.mean_reduction):>7} "
            f"{_pct(sim.median_reduction):>7} "
            f"{_pct(sim.p25_reduction):>7} "
            f"{_pct(sim.p75_reduction):>7} "
            f"{_pct(sim.p95_reduction):>7} "
            f"{sim.stdev_reduction * 100:>5.1f}pp "
            f"{_usd(sim.mean_saved_usd):>13}"
        )
    lines.append("  " + "─" * 108)
    lines.append(
        f"  {'Σ all runs':<40} "
        f"{_pct(suite.mean_reduction):>7} "
        f"{_pct(suite.median_reduction):>7} "
        f"{_pct(suite.p25_reduction):>7} "
        f"{_pct(suite.p75_reduction):>7} "
        f"{_pct(suite.p95_reduction):>7} "
        f"{suite.stdev_reduction * 100:>5.1f}pp "
        f"{_usd(suite.mean_cost_saved_per_run_usd):>13}"
    )
    lines.append("═" * 110)
    lines.append("")
    lines.append(f"  HEADLINE MARKETING NUMBER:")
    lines.append(f"    Mean reduction across {suite.total_runs} runs: {_pct(suite.mean_reduction)}")
    lines.append(
        f"    95% of runs fall between {_pct(suite.p05_reduction)} and "
        f"{_pct(suite.p95_reduction)}"
    )
    lines.append(
        f"    Total cost saved if you ran each scenario {runs_per} times: "
        f"{_usd(suite.total_cost_saved_usd)}"
    )
    lines.append("")
    return "\n".join(lines)


def render_markdown(suite: SuiteSimulation, runs_per: int, seed: int) -> str:
    rows = []
    rows.append("# Workflow Benchmark — Monte-Carlo Results")
    rows.append("")
    rows.append(
        f"`{runs_per} runs/scenario × {len(suite.sims)} scenarios = "
        f"{suite.total_runs} total runs · seed={seed} · generated "
        f"{_dt.datetime.now().isoformat(timespec='seconds')}`"
    )
    rows.append("")
    rows.append("## Headline number")
    rows.append("")
    rows.append(
        f"**Average token reduction: {_pct(suite.mean_reduction)}** across "
        f"{suite.total_runs} simulated runs."
    )
    rows.append("")
    rows.append(
        f"- Median: {_pct(suite.median_reduction)}"
    )
    rows.append(
        f"- 95% confidence range: {_pct(suite.p05_reduction)} — {_pct(suite.p95_reduction)}"
    )
    rows.append(
        f"- IQR (typical case): {_pct(suite.p25_reduction)} — {_pct(suite.p75_reduction)}"
    )
    rows.append(
        f"- Standard deviation: {suite.stdev_reduction * 100:.1f} percentage points"
    )
    rows.append("")
    rows.append("## Per scenario")
    rows.append("")
    rows.append(
        "| Scenario | Persona | Mean | Median | p25 | p75 | p95 | σ | $ saved (avg) |"
    )
    rows.append("|---|---|---:|---:|---:|---:|---:|---:|---:|")
    for sim in suite.sims:
        rows.append(
            f"| {sim.scenario_title} | {sim.persona} | "
            f"**{_pct(sim.mean_reduction)}** | "
            f"{_pct(sim.median_reduction)} | "
            f"{_pct(sim.p25_reduction)} | "
            f"{_pct(sim.p75_reduction)} | "
            f"{_pct(sim.p95_reduction)} | "
            f"{sim.stdev_reduction * 100:.1f}pp | "
            f"{_usd(sim.mean_saved_usd)} |"
        )
    rows.append(
        f"| **All scenarios pooled** | — | "
        f"**{_pct(suite.mean_reduction)}** | "
        f"{_pct(suite.median_reduction)} | "
        f"{_pct(suite.p25_reduction)} | "
        f"{_pct(suite.p75_reduction)} | "
        f"{_pct(suite.p95_reduction)} | "
        f"{suite.stdev_reduction * 100:.1f}pp | "
        f"{_usd(suite.mean_cost_saved_per_run_usd)} |"
    )
    rows.append("")
    rows.append("## What we cite where")
    rows.append("")
    rows.append(
        "| Surface | Claim | Source |"
    )
    rows.append("|---|---|---|")
    rows.append(
        f"| Hero (one number) | `~{_pct(suite.mean_reduction).rstrip('%').split('.')[0]}% average` | this run, mean |"
    )
    rows.append(
        f"| Pricing CTA / sales deck | "
        f"`{_pct(suite.p25_reduction).rstrip('%').split('.')[0]}-{_pct(suite.p75_reduction).rstrip('%').split('.')[0]}% typical, "
        f"{_pct(suite.mean_reduction).rstrip('%').split('.')[0]}% average` | this run, IQR + mean |"
    )
    rows.append(
        f"| Honest range (worst case) | "
        f"`min run: {_pct(min(s.min_reduction for s in suite.sims))}` | this run, min |"
    )
    rows.append("")
    rows.append("## Methodology")
    rows.append("")
    rows.append(
        f"Each scenario template (see `scenarios/`) is perturbed with two kinds of variance:"
    )
    rows.append("")
    rows.append(
        f"1. **Token-size variance**: every step's `produces_tokens` is multiplied by a log-normal factor centred on 1.0 with σ tuned per scenario (0.22 to 0.40 — see `simulation.py:VARIANCE_RECIPES`)."
    )
    rows.append(
        f"2. **Step-count variance**: workflows with naturally variable counts (number of sources fetched, files in a PR, turns in a conversation) sample from realistic ranges. A 5-source research task might run on 3 or 8 sources; a code review might cover 5 or 25 files."
    )
    rows.append("")
    rows.append(
        f"The seed is fixed (`{seed}`) — re-running this script reproduces the exact numbers above. To regenerate a fresh set, change seed and document the reason in your commit message."
    )
    rows.append("")
    return "\n".join(rows)


def to_json(suite: SuiteSimulation, runs_per: int, seed: int) -> dict:
    return {
        "schema": "plinth.workflow_simulation/v1",
        "generated": _dt.datetime.now().isoformat(timespec="seconds"),
        "settings": {
            "runs_per_scenario": runs_per,
            "n_scenarios": len(suite.sims),
            "total_runs": suite.total_runs,
            "seed": seed,
        },
        "pricing": {
            "model": "claude-sonnet-4.5",
            "input_per_mtok_usd": INPUT_COST_PER_MTOK,
            "output_per_mtok_usd": OUTPUT_COST_PER_MTOK,
            "as_of": "2026-05",
        },
        "aggregate": {
            "mean_reduction_pct": round(suite.mean_reduction * 100, 2),
            "median_reduction_pct": round(suite.median_reduction * 100, 2),
            "p05_reduction_pct": round(suite.p05_reduction * 100, 2),
            "p25_reduction_pct": round(suite.p25_reduction * 100, 2),
            "p75_reduction_pct": round(suite.p75_reduction * 100, 2),
            "p95_reduction_pct": round(suite.p95_reduction * 100, 2),
            "stdev_pp": round(suite.stdev_reduction * 100, 2),
            "total_cost_saved_usd": round(suite.total_cost_saved_usd, 2),
            "mean_cost_saved_per_run_usd": round(suite.mean_cost_saved_per_run_usd, 4),
        },
        "scenarios": [
            {
                "id": sim.scenario_id,
                "title": sim.scenario_title,
                "persona": sim.persona,
                "n_runs": sim.n_runs,
                "mean_reduction_pct": round(sim.mean_reduction * 100, 2),
                "median_reduction_pct": round(sim.median_reduction * 100, 2),
                "p05_reduction_pct": round(sim.p05_reduction * 100, 2),
                "p25_reduction_pct": round(sim.p25_reduction * 100, 2),
                "p75_reduction_pct": round(sim.p75_reduction * 100, 2),
                "p95_reduction_pct": round(sim.p95_reduction * 100, 2),
                "min_reduction_pct": round(sim.min_reduction * 100, 2),
                "max_reduction_pct": round(sim.max_reduction * 100, 2),
                "stdev_pp": round(sim.stdev_reduction * 100, 2),
                "mean_saved_usd": round(sim.mean_saved_usd, 4),
                "samples": [
                    {
                        "i": s.run_index,
                        "n_steps": s.n_steps,
                        "reduction_pct": round(s.reduction_pct * 100, 3),
                        "saved_usd": round(s.cost_saved_usd, 4),
                    }
                    for s in sim.samples
                ],
            }
            for sim in suite.sims
        ],
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--runs",
        type=int,
        default=N_RUNS_PER_SCENARIO,
        help=f"Runs per scenario (default {N_RUNS_PER_SCENARIO})",
    )
    parser.add_argument(
        "--seed", type=int, default=DEFAULT_SEED, help=f"RNG seed (default {DEFAULT_SEED})"
    )
    parser.add_argument("--markdown", action="store_true", help="Emit Markdown")
    parser.add_argument("--no-write", action="store_true", help="Don't write JSON")
    parser.add_argument("--quiet", action="store_true", help="JSON path only")
    args = parser.parse_args()

    sims = simulate_all(ALL_SCENARIOS, n_runs=args.runs, seed=args.seed)
    suite = SuiteSimulation(sims=sims)

    if args.markdown:
        print(render_markdown(suite, args.runs, args.seed))
    elif not args.quiet:
        print(render_text(suite, args.runs, args.seed))

    if not args.no_write:
        RESULTS_DIR.mkdir(parents=True, exist_ok=True)
        ts = _dt.datetime.now().strftime("%Y%m%d-%H%M%S")
        out = RESULTS_DIR / f"{ts}-simulation-n{args.runs}.json"
        out.write_text(json.dumps(to_json(suite, args.runs, args.seed), indent=2))
        if args.quiet:
            print(str(out))
        else:
            print(f"  → JSON: {out.relative_to(REPO_ROOT)}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
