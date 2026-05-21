"""Monte-Carlo simulation layer for the workflow benchmark suite.

Each scenario in scenarios/ is a deterministic template. To get a
representative average for marketing claims, we perturb each scenario
with realistic random variation and run it many times.

Two kinds of variation:

1. Token-size variation per step. Real PDFs aren't all exactly 20k
   tokens; real customer messages aren't all 250. We sample each
   produces_tokens value from a log-normal centred on the template
   value, with σ controlled per-scenario.

2. Step-count variation. A "5-source" workflow might run on 3 or 8
   sources in reality. A "30-PDF" review might handle 22 or 38. We
   add or drop trailing iterations of repeatable step patterns.

The seed is fixed for reproducibility — running the same seed produces
identical samples. Change SEED below to regenerate; document the change.

Output: per-scenario statistics + aggregate. The mean across all
scenarios is what we cite in marketing. Individual percentiles
(p25, p75) give buyers a realistic range.
"""

from __future__ import annotations

import dataclasses
import hashlib
import math
import random
import re
import statistics
from dataclasses import dataclass, field
from typing import Iterable

from .harness import Scenario, ScenarioResult, Step, run

# Default Monte-Carlo settings. Bumping N_RUNS_PER_SCENARIO past 100
# yields negligible CI improvement but slows the suite down.
DEFAULT_SEED = 42
N_RUNS_PER_SCENARIO = 100

# ─── Per-scenario variance recipes ────────────────────────────────────
# Each entry tunes how aggressively to perturb that scenario. Keys map
# to scenario IDs.
#
# fields:
#   token_sigma: log-normal σ for produces_tokens (and prompt_overhead)
#                values. 0.25 → ~25% rough variability. 0.4 → ~40%.
#   count_patterns: list of (regex_prefix, min_count, max_count) for
#                step IDs that repeat. The perturber will keep a random
#                subset matching the count range. If absent, step
#                count stays fixed.

VARIANCE_RECIPES: dict[str, dict] = {
    "research-5-source": {
        "token_sigma": 0.30,
        "count_patterns": [
            (r"^fetch_(\d+)$", 3, 8),
            (r"^summarise_(\d+)$", 3, 8),
        ],
    },
    "research-30-pdf": {
        "token_sigma": 0.32,
        "count_patterns": [
            (r"^fetch_pdf_(\d+)$", 20, 40),
            (r"^summarise_pdf_(\d+)$", 20, 40),
        ],
    },
    "multi-agent-content": {
        "token_sigma": 0.25,
        # Rounds (R1/R2/R3) all happen in our test — don't drop them;
        # production workflows abort early on reviewer-approval so the
        # average is between 1.5 and 3 rounds. We keep all 3 for the
        # worst-case marketing claim.
        "count_patterns": [],
    },
    "code-review-pr": {
        "token_sigma": 0.40,
        "count_patterns": [
            (r"^analyse_file_(\d+)$", 5, 25),
            (r"^post_comment_(\d+)$", 1, 12),
        ],
    },
    "customer-support-escalation": {
        # Conversations have wide variance; small messages have
        # smaller absolute swing.
        "token_sigma": 0.22,
        # Turn count varies wildly — drop trailing pairs of
        # tN_customer + tN_reply.
        "count_patterns": [],  # see custom logic in perturb_customer_support
    },
    "sales-lead-enrichment": {
        "token_sigma": 0.35,
        "count_patterns": [],
    },
    "doc-qa-50-contracts": {
        "token_sigma": 0.30,
        "count_patterns": [
            (r"^extract_clause_(\d+)$", 5, 12),
        ],
    },
    "report-writing": {
        "token_sigma": 0.28,
        "count_patterns": [
            (r"^fetch_source_(\d+)$", 4, 9),
        ],
    },
}


# ─── Perturbation ─────────────────────────────────────────────────────


def _lognormal_factor(rng: random.Random, sigma: float) -> float:
    """Sample a multiplier centred on 1.0 with log-normal noise."""
    return math.exp(rng.gauss(0.0, sigma))


def _pick_steps_by_pattern(
    steps: list[Step],
    pattern: str,
    min_count: int,
    max_count: int,
    rng: random.Random,
) -> set[str]:
    """Decide which IDs matching `pattern` to KEEP. Returns the kept IDs."""
    rx = re.compile(pattern)
    matching = [s.id for s in steps if rx.match(s.id)]
    if not matching:
        return set()
    # Pick a target count uniformly in [min, max], capped by available count.
    target = rng.randint(min_count, max_count)
    target = min(target, len(matching))
    target = max(1, target)  # never zero — degenerate
    if target >= len(matching):
        return set(matching)
    # Keep the first `target` matching steps to preserve dependency order
    # (deps reference prior step IDs).
    return set(matching[:target])


def perturb(scenario: Scenario, rng: random.Random) -> Scenario:
    """Apply variance recipe to a deterministic scenario template."""
    recipe = VARIANCE_RECIPES.get(scenario.id, {"token_sigma": 0.25, "count_patterns": []})
    sigma = recipe.get("token_sigma", 0.25)
    patterns = recipe.get("count_patterns", [])

    # ── Step 1: decide which steps survive count perturbation ──────
    kept: set[str] = set()
    pattern_step_ids: set[str] = set()
    for pat, lo, hi in patterns:
        rx = re.compile(pat)
        for s in scenario.steps:
            if rx.match(s.id):
                pattern_step_ids.add(s.id)
        keep_ids = _pick_steps_by_pattern(list(scenario.steps), pat, lo, hi, rng)
        kept.update(keep_ids)

    # Steps NOT matched by any pattern are always kept.
    for s in scenario.steps:
        if s.id not in pattern_step_ids:
            kept.add(s.id)

    # ── Step 1b: customer-support custom logic — drop trailing turn pairs ──
    if scenario.id == "customer-support-escalation":
        # Existing turns are t1..t10. Vary between 5 and 14 (but our template
        # has 10 max, so we sample [5, 10] and drop excess).
        turn_count = rng.randint(5, 10)
        # Always keep t1..t<turn_count> reply/customer + their lookups
        keep_turn_ids = set()
        for t in range(1, turn_count + 1):
            for prefix in (f"t{t}_customer", f"t{t}_reply", f"t{t}_lookup"):
                for s in scenario.steps:
                    if s.id.startswith(prefix):
                        keep_turn_ids.add(s.id)
        # Closing note depends on last reply
        keep_turn_ids.add("ticket_resolution_note")
        # Filter: drop t11+, redirect resolution_note to t<turn_count>_reply
        kept = {s.id for s in scenario.steps if s.id in keep_turn_ids or not s.id.startswith("t")}

    # ── Step 2: rebuild steps with perturbed token sizes + filtered deps ──
    new_steps: list[Step] = []
    for s in scenario.steps:
        if s.id not in kept:
            continue
        # Filter requires that referenced dropped steps
        new_requires = tuple(r for r in s.requires if r in kept)
        # If we have customer-support, ensure ticket_resolution_note points
        # at the last-kept tN_reply
        if scenario.id == "customer-support-escalation" and s.id == "ticket_resolution_note":
            reply_ids = sorted(
                (sid for sid in kept if re.match(r"^t\d+_reply$", sid)),
                key=lambda sid: int(re.match(r"t(\d+)", sid).group(1)),
            )
            new_requires = tuple(r for r in new_requires if not r.startswith("t")) + (
                (reply_ids[-1],) if reply_ids else ()
            )
        # Sample token sizes
        token_factor = _lognormal_factor(rng, sigma)
        # Prompt overhead has tighter variance — instructions don't vary much
        overhead_factor = _lognormal_factor(rng, sigma * 0.3)
        new_steps.append(
            dataclasses.replace(
                s,
                produces_tokens=max(1, int(s.produces_tokens * token_factor)),
                prompt_overhead=max(20, int(s.prompt_overhead * overhead_factor)),
                requires=new_requires,
            )
        )

    return dataclasses.replace(scenario, steps=tuple(new_steps))


# ─── Simulation runner + statistics ───────────────────────────────────


@dataclass
class RunSample:
    """One simulated run's result."""

    run_index: int
    n_steps: int
    baseline_total: int
    plinth_total: int
    reduction_pct: float
    cost_baseline_usd: float
    cost_plinth_usd: float
    cost_saved_usd: float


@dataclass
class ScenarioSimulation:
    scenario_id: str
    scenario_title: str
    persona: str
    n_runs: int
    samples: list[RunSample] = field(default_factory=list)

    # ─ aggregate metrics, computed lazily ─
    def _pcts(self) -> list[float]:
        return [s.reduction_pct for s in self.samples]

    def _costs_baseline(self) -> list[float]:
        return [s.cost_baseline_usd for s in self.samples]

    def _costs_plinth(self) -> list[float]:
        return [s.cost_plinth_usd for s in self.samples]

    def _saved(self) -> list[float]:
        return [s.cost_saved_usd for s in self.samples]

    @property
    def mean_reduction(self) -> float:
        return statistics.mean(self._pcts())

    @property
    def median_reduction(self) -> float:
        return statistics.median(self._pcts())

    @property
    def stdev_reduction(self) -> float:
        return statistics.stdev(self._pcts()) if len(self.samples) > 1 else 0.0

    @property
    def p05_reduction(self) -> float:
        return _percentile(self._pcts(), 5)

    @property
    def p25_reduction(self) -> float:
        return _percentile(self._pcts(), 25)

    @property
    def p75_reduction(self) -> float:
        return _percentile(self._pcts(), 75)

    @property
    def p95_reduction(self) -> float:
        return _percentile(self._pcts(), 95)

    @property
    def min_reduction(self) -> float:
        return min(self._pcts())

    @property
    def max_reduction(self) -> float:
        return max(self._pcts())

    @property
    def mean_saved_usd(self) -> float:
        return statistics.mean(self._saved())


def _percentile(values: list[float], p: float) -> float:
    """Linear-interpolation percentile (matches numpy default)."""
    if not values:
        return 0.0
    sorted_vals = sorted(values)
    k = (len(sorted_vals) - 1) * (p / 100.0)
    f = math.floor(k)
    c = math.ceil(k)
    if f == c:
        return sorted_vals[int(k)]
    return sorted_vals[f] + (sorted_vals[c] - sorted_vals[f]) * (k - f)


def simulate(
    scenario: Scenario,
    n_runs: int = N_RUNS_PER_SCENARIO,
    seed: int = DEFAULT_SEED,
) -> ScenarioSimulation:
    """Run perturbed Monte-Carlo over a scenario.

    Each run produces one ScenarioResult; the spread across runs is what
    powers the statistics.
    """
    # Stable per-process seed: combine the run seed with an md5-derived
    # offset of the scenario id. Plain hash() is randomised per process
    # (PYTHONHASHSEED), which would make runs non-reproducible across
    # invocations. md5 is deterministic.
    digest = hashlib.md5(scenario.id.encode("utf-8")).hexdigest()
    offset = int(digest[:8], 16) % 10_000
    rng = random.Random(seed + offset)
    sim = ScenarioSimulation(
        scenario_id=scenario.id,
        scenario_title=scenario.title,
        persona=scenario.persona,
        n_runs=n_runs,
    )

    for i in range(n_runs):
        perturbed = perturb(scenario, rng)
        result = run(perturbed)
        sim.samples.append(
            RunSample(
                run_index=i,
                n_steps=len(perturbed.steps),
                baseline_total=result.baseline_total,
                plinth_total=result.plinth_total,
                reduction_pct=result.reduction_pct,
                cost_baseline_usd=result.cost_baseline_usd,
                cost_plinth_usd=result.cost_plinth_usd,
                cost_saved_usd=result.cost_saved_usd,
            )
        )

    return sim


def simulate_all(
    scenarios: Iterable[Scenario],
    n_runs: int = N_RUNS_PER_SCENARIO,
    seed: int = DEFAULT_SEED,
) -> list[ScenarioSimulation]:
    return [simulate(s, n_runs=n_runs, seed=seed) for s in scenarios]


# ─── Aggregate across all scenarios ───────────────────────────────────


@dataclass
class SuiteSimulation:
    sims: list[ScenarioSimulation]

    @property
    def total_runs(self) -> int:
        return sum(s.n_runs for s in self.sims)

    @property
    def all_reductions(self) -> list[float]:
        return [r.reduction_pct for s in self.sims for r in s.samples]

    @property
    def mean_reduction(self) -> float:
        return statistics.mean(self.all_reductions)

    @property
    def median_reduction(self) -> float:
        return statistics.median(self.all_reductions)

    @property
    def stdev_reduction(self) -> float:
        return (
            statistics.stdev(self.all_reductions)
            if len(self.all_reductions) > 1
            else 0.0
        )

    @property
    def p05_reduction(self) -> float:
        return _percentile(self.all_reductions, 5)

    @property
    def p25_reduction(self) -> float:
        return _percentile(self.all_reductions, 25)

    @property
    def p75_reduction(self) -> float:
        return _percentile(self.all_reductions, 75)

    @property
    def p95_reduction(self) -> float:
        return _percentile(self.all_reductions, 95)

    @property
    def total_cost_saved_usd(self) -> float:
        return sum(r.cost_saved_usd for s in self.sims for r in s.samples)

    @property
    def mean_cost_saved_per_run_usd(self) -> float:
        return statistics.mean(
            r.cost_saved_usd for s in self.sims for r in s.samples
        )
