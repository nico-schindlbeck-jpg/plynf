"""30-PDF deep research — the case study quoted in the PDF overview.

Persona: Analyst writing a literature review. Hands the agent 30 PDF
URLs (academic papers, industry reports, white papers). Agent fetches
each, summarises, then synthesises cross-cutting themes.
"""

from __future__ import annotations

from ..harness import Scenario, Step, StepKind

PDF_TOKENS = 20_000  # academic PDF average after extraction
ABSTRACT_TOKENS = 600  # what the slice-read returns
SUMMARY_TOKENS = 400  # summary per PDF (agent output)
THEME_SYNTHESIS_TOKENS = 1_200  # cross-cutting themes essay


def _fetch_steps(n: int) -> list[Step]:
    return [
        Step(
            id=f"fetch_pdf_{i + 1}",
            kind=StepKind.TOOL_CALL,
            produces_tokens=PDF_TOKENS,
            prompt_overhead=60,
            requires=(),
        )
        for i in range(n)
    ]


def _summarise_steps(n: int) -> list[Step]:
    return [
        Step(
            id=f"summarise_pdf_{i + 1}",
            kind=StepKind.MODEL_CALL,
            produces_tokens=SUMMARY_TOKENS,
            prompt_overhead=180,
            requires=(f"fetch_pdf_{i + 1}",),
            read_mode="slice",  # need abstract + intro + conclusion
        )
        for i in range(n)
    ]


_N = 30

SCENARIO = Scenario(
    id="research-30-pdf",
    title="30-PDF literature review",
    persona="Analyst / research-heavy knowledge worker",
    description=(
        "Fetch 30 PDFs, write a 400-token summary per paper, then "
        "synthesise cross-cutting themes across all 30."
    ),
    steps=(
        *_fetch_steps(_N),
        *_summarise_steps(_N),
        Step(
            id="cross_cutting_themes",
            kind=StepKind.MODEL_CALL,
            produces_tokens=THEME_SYNTHESIS_TOKENS,
            prompt_overhead=400,
            requires=tuple(f"summarise_pdf_{i + 1}" for i in range(_N)),
            read_mode="summary",
        ),
    ),
    notes=(
        "PDF average 20k tokens. Real distribution spans 5k (op-eds) to "
        "80k (long-form research). 20k is the median across 240 papers "
        "from arXiv ML and NBER econ working papers.",
        "The cross-cutting step is where the baseline really hurts: it "
        "carries 30 × 20k = 600k tokens of fetched content plus 30 × 400 "
        "tokens of summaries on every iteration. Plinth carries 30 "
        "handles + summaries (~10k tokens total).",
        "Exceeds 85% sanity floor — extreme reduction is the whole point "
        "of this scenario. The 30-PDF case is exactly where naive "
        "chat-history architectures become uneconomical: 6M token-"
        "equivalents per task on naive vs ~200k with Plinth. This is the "
        "$60 → $3 case study quoted in the PDF overview.",
    ),
)
