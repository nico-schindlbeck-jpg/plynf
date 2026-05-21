"""Harness — computes baseline vs Plinth token usage for a Scenario.

Pure data-in / numbers-out. Does no I/O, no LLM calls, no network.
Deterministic.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Iterable

# ─── Pricing constants (Claude Sonnet 4.5, May 2026 list price) ─────────
INPUT_COST_PER_MTOK = 3.00
OUTPUT_COST_PER_MTOK = 15.00

# ─── Plinth-side constants ──────────────────────────────────────────────
HANDLE_TOKENS = 30  # cost of carrying a handle like "ws://x/y/z@v1" in prompt
DEFAULT_SUMMARY_SIZE = 300  # tokens read when dereferencing for default summary
DEFAULT_SLICE_SIZE = 1500  # tokens read for a focused slice / top-k search


class StepKind(str, Enum):
    """What the step is doing — affects accounting symmetry."""

    TOOL_CALL = "tool_call"
    MODEL_CALL = "model_call"
    HANDOFF = "handoff"


@dataclass(frozen=True)
class Step:
    """One step in an agent workflow.

    Attributes:
        id: stable name unique within the scenario
        kind: see StepKind
        produces_tokens: how big the step's output is
        prompt_overhead: fixed instruction/template tokens for this step
        requires: ids of prior steps whose outputs this step needs
        read_mode: "summary" (300 tokens per dereference) or
                   "slice" (1500 tokens — used when the agent needs more
                   detail than the default summary projection)
        baseline_must_send_full: if true, in the baseline implementation
                   the full output of `requires` items is sent even if a
                   summary would suffice — e.g. a code-review prompt that
                   needs the actual diff, not a summary.
    """

    id: str
    kind: StepKind
    produces_tokens: int
    prompt_overhead: int = 100
    requires: tuple[str, ...] = ()
    read_mode: str = "summary"  # or "slice"
    baseline_must_send_full: bool = True


@dataclass(frozen=True)
class Scenario:
    """A benchmarkable workflow."""

    id: str
    title: str
    persona: str  # who runs this workflow
    description: str
    steps: tuple[Step, ...]
    # Optional manual notes that appear in the report
    notes: tuple[str, ...] = ()


@dataclass
class StepResult:
    step_id: str
    baseline_input: int
    baseline_output: int
    plinth_input: int
    plinth_output: int


@dataclass
class ScenarioResult:
    scenario: Scenario
    steps: list[StepResult]
    baseline_total_input: int = 0
    baseline_total_output: int = 0
    plinth_total_input: int = 0
    plinth_total_output: int = 0

    @property
    def baseline_total(self) -> int:
        return self.baseline_total_input + self.baseline_total_output

    @property
    def plinth_total(self) -> int:
        return self.plinth_total_input + self.plinth_total_output

    @property
    def reduction_pct(self) -> float:
        if self.baseline_total == 0:
            return 0.0
        return 1.0 - (self.plinth_total / self.baseline_total)

    @property
    def cost_baseline_usd(self) -> float:
        return (
            self.baseline_total_input * INPUT_COST_PER_MTOK / 1_000_000
            + self.baseline_total_output * OUTPUT_COST_PER_MTOK / 1_000_000
        )

    @property
    def cost_plinth_usd(self) -> float:
        return (
            self.plinth_total_input * INPUT_COST_PER_MTOK / 1_000_000
            + self.plinth_total_output * OUTPUT_COST_PER_MTOK / 1_000_000
        )

    @property
    def cost_saved_usd(self) -> float:
        return self.cost_baseline_usd - self.cost_plinth_usd


def run(scenario: Scenario) -> ScenarioResult:
    """Compute baseline vs Plinth token usage for a scenario."""
    by_id = {s.id: s for s in scenario.steps}
    result = ScenarioResult(scenario=scenario, steps=[])

    # Cumulative output tokens — what the baseline carries forward at each step.
    cumulative_output = 0

    for step in scenario.steps:
        # Output tokens are the same in both implementations.
        output_tokens = step.produces_tokens

        # ── Baseline input: prompt overhead + all prior outputs ──
        baseline_input = step.prompt_overhead + cumulative_output

        # ── Plinth input: prompt overhead + handles + scoped reads ──
        plinth_input = step.prompt_overhead
        for req_id in step.requires:
            plinth_input += HANDLE_TOKENS
            req = by_id[req_id]
            if step.read_mode == "slice":
                plinth_input += min(DEFAULT_SLICE_SIZE, req.produces_tokens)
            else:
                plinth_input += min(DEFAULT_SUMMARY_SIZE, req.produces_tokens)

        # For tool calls, the "output" is the tool result — same in both
        # implementations. The Plinth side stores it in the workspace
        # rather than returning it inline, but the tokens produced are
        # identical.
        result.steps.append(
            StepResult(
                step_id=step.id,
                baseline_input=baseline_input,
                baseline_output=output_tokens,
                plinth_input=plinth_input,
                plinth_output=output_tokens,
            )
        )

        result.baseline_total_input += baseline_input
        result.baseline_total_output += output_tokens
        result.plinth_total_input += plinth_input
        result.plinth_total_output += output_tokens

        # In the baseline, this step's output now joins the running context.
        cumulative_output += output_tokens

    return result


def run_all(scenarios: Iterable[Scenario]) -> list[ScenarioResult]:
    return [run(s) for s in scenarios]
