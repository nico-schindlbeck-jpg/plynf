"""Long-form report — 12-section structured document with cross-references.

Persona: Strategy team auto-generating a quarterly market report. The
report has 12 sections (intro, market overview, 4 competitor profiles,
3 trends, 2 case studies, conclusion, exec summary). Each section
draws on the same underlying research corpus but cites different
subsets.
"""

from __future__ import annotations

from ..harness import Scenario, Step, StepKind

# Underlying research corpus — fetched once at the start.
SOURCE_TOKENS = 12_000  # market data source
N_SOURCES = 6

# Per-section output sizes
SECTION_TOKENS = {
    "intro": 800,
    "market_overview": 2_500,
    "competitor_1": 1_800,
    "competitor_2": 1_800,
    "competitor_3": 1_800,
    "competitor_4": 1_800,
    "trend_1": 1_500,
    "trend_2": 1_500,
    "trend_3": 1_500,
    "case_study_1": 2_200,
    "case_study_2": 2_200,
    "conclusion": 1_200,
    "exec_summary": 1_000,
}


def _fetch_steps() -> list[Step]:
    return [
        Step(
            id=f"fetch_source_{i + 1}",
            kind=StepKind.TOOL_CALL,
            produces_tokens=SOURCE_TOKENS,
            prompt_overhead=60,
            requires=(),
        )
        for i in range(N_SOURCES)
    ]


_ALL_SOURCES = tuple(f"fetch_source_{i + 1}" for i in range(N_SOURCES))


SCENARIO = Scenario(
    id="report-writing",
    title="12-section quarterly market report",
    persona="Strategy / market research auto-writer",
    description=(
        "Fetch 6 market data sources, write a 13-section structured "
        "report (~22k tokens of output) where each section cites "
        "relevant sources and references earlier sections."
    ),
    steps=(
        *_fetch_steps(),
        # ─── Intro section (uses all sources, no prior sections) ────
        Step(
            id="write_intro",
            kind=StepKind.MODEL_CALL,
            produces_tokens=SECTION_TOKENS["intro"],
            prompt_overhead=300,
            requires=_ALL_SOURCES,
            read_mode="summary",
        ),
        # ─── Market overview (uses sources 1-3) ─────────────────────
        Step(
            id="write_market_overview",
            kind=StepKind.MODEL_CALL,
            produces_tokens=SECTION_TOKENS["market_overview"],
            prompt_overhead=300,
            requires=("fetch_source_1", "fetch_source_2", "fetch_source_3"),
            read_mode="slice",
        ),
        # ─── 4 competitor profiles (each uses sources 3-5) ──────────
        *[
            Step(
                id=f"write_competitor_{i + 1}",
                kind=StepKind.MODEL_CALL,
                produces_tokens=SECTION_TOKENS[f"competitor_{i + 1}"],
                prompt_overhead=300,
                requires=(
                    "fetch_source_3",
                    "fetch_source_4",
                    "fetch_source_5",
                    "write_market_overview",
                ),
                read_mode="slice",
            )
            for i in range(4)
        ],
        # ─── 3 trends sections (each uses sources 2-6 + comp profiles) ─
        *[
            Step(
                id=f"write_trend_{i + 1}",
                kind=StepKind.MODEL_CALL,
                produces_tokens=SECTION_TOKENS[f"trend_{i + 1}"],
                prompt_overhead=300,
                requires=(
                    "fetch_source_2",
                    "fetch_source_6",
                    "write_competitor_1",
                    "write_competitor_2",
                    "write_competitor_3",
                    "write_competitor_4",
                ),
                read_mode="summary",
            )
            for i in range(3)
        ],
        # ─── 2 case studies (use sources 5-6 + trend 1) ─────────────
        *[
            Step(
                id=f"write_case_study_{i + 1}",
                kind=StepKind.MODEL_CALL,
                produces_tokens=SECTION_TOKENS[f"case_study_{i + 1}"],
                prompt_overhead=300,
                requires=(
                    "fetch_source_5",
                    "fetch_source_6",
                    "write_trend_1",
                ),
                read_mode="slice",
            )
            for i in range(2)
        ],
        # ─── Conclusion (uses all prior sections, summaries are enough) ─
        Step(
            id="write_conclusion",
            kind=StepKind.MODEL_CALL,
            produces_tokens=SECTION_TOKENS["conclusion"],
            prompt_overhead=300,
            requires=(
                "write_market_overview",
                "write_competitor_1",
                "write_competitor_2",
                "write_competitor_3",
                "write_competitor_4",
                "write_trend_1",
                "write_trend_2",
                "write_trend_3",
                "write_case_study_1",
                "write_case_study_2",
            ),
            read_mode="summary",
        ),
        # ─── Exec summary (uses intro + conclusion only) ────────────
        Step(
            id="write_exec_summary",
            kind=StepKind.MODEL_CALL,
            produces_tokens=SECTION_TOKENS["exec_summary"],
            prompt_overhead=300,
            requires=("write_intro", "write_conclusion"),
            read_mode="slice",
        ),
    ),
    notes=(
        "Report writing is unique because each section needs DIFFERENT "
        "subsets of the corpus. Baseline can't selectively forget — it "
        "drags everything forward. Plinth lets each section's prompt "
        "carry only the handles it needs.",
        "By the conclusion step in baseline, the prompt contains all 6 "
        "fetched sources + all 10 prior sections = ~92k tokens. Plinth's "
        "conclusion prompt is ~5k tokens (10 handles + 10 summaries).",
        "Cross-references between sections (e.g., 'as shown in trend_1') "
        "work because the agent has section handles in scope.",
        "Exceeds 85% sanity floor — extreme reduction reflects that "
        "long-form structured writing is fundamentally a fan-out / fan-in "
        "pattern. Naive chains drag every prior section + source forward; "
        "Plinth keeps each section's prompt scoped to its own dependencies. "
        "The savings compound with section count.",
    ),
)
