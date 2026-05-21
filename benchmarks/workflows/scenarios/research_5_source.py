"""5-source research agent — the headline scenario.

Persona: Anna, solo developer building a research bot. Asks the agent
to find five sources on a topic, fetch each, write a synthesis paragraph
citing all five. This is the existing examples/01-research-agent
workflow modelled as a single benchmark scenario.

Cross-check: must produce ±3pp the reduction reported by the live demo
at examples/01-research-agent/compare.py.
"""

from __future__ import annotations

from ..harness import Scenario, Step, StepKind

# ─── Token budgets ──────────────────────────────────────────────────────
# Average web page after extraction (stripped of HTML/JS) is ~8k tokens.
PAGE_TOKENS = 8_000
# Search result list (titles + snippets, 10 hits)
SEARCH_RESULT_TOKENS = 800
# Summary written per source by the agent (LLM output)
SUMMARY_TOKENS = 350
# Final synthesis paragraph
SYNTHESIS_TOKENS = 600

SCENARIO = Scenario(
    id="research-5-source",
    title="5-source research synthesis",
    persona="Hobby developer / research bot",
    description=(
        "Search the web, fetch 5 pages, summarise each, write a "
        "synthesis paragraph citing all five sources."
    ),
    steps=(
        Step(
            id="search",
            kind=StepKind.TOOL_CALL,
            produces_tokens=SEARCH_RESULT_TOKENS,
            prompt_overhead=80,
            requires=(),
        ),
        Step(
            id="fetch_1",
            kind=StepKind.TOOL_CALL,
            produces_tokens=PAGE_TOKENS,
            prompt_overhead=60,
            requires=("search",),
            read_mode="summary",
        ),
        Step(
            id="fetch_2",
            kind=StepKind.TOOL_CALL,
            produces_tokens=PAGE_TOKENS,
            prompt_overhead=60,
            requires=("search",),
        ),
        Step(
            id="fetch_3",
            kind=StepKind.TOOL_CALL,
            produces_tokens=PAGE_TOKENS,
            prompt_overhead=60,
            requires=("search",),
        ),
        Step(
            id="fetch_4",
            kind=StepKind.TOOL_CALL,
            produces_tokens=PAGE_TOKENS,
            prompt_overhead=60,
            requires=("search",),
        ),
        Step(
            id="fetch_5",
            kind=StepKind.TOOL_CALL,
            produces_tokens=PAGE_TOKENS,
            prompt_overhead=60,
            requires=("search",),
        ),
        Step(
            id="summarise_1",
            kind=StepKind.MODEL_CALL,
            produces_tokens=SUMMARY_TOKENS,
            prompt_overhead=200,
            requires=("fetch_1",),
            read_mode="slice",  # need actual page content, not auto-summary
        ),
        Step(
            id="summarise_2",
            kind=StepKind.MODEL_CALL,
            produces_tokens=SUMMARY_TOKENS,
            prompt_overhead=200,
            requires=("fetch_2",),
            read_mode="slice",
        ),
        Step(
            id="summarise_3",
            kind=StepKind.MODEL_CALL,
            produces_tokens=SUMMARY_TOKENS,
            prompt_overhead=200,
            requires=("fetch_3",),
            read_mode="slice",
        ),
        Step(
            id="summarise_4",
            kind=StepKind.MODEL_CALL,
            produces_tokens=SUMMARY_TOKENS,
            prompt_overhead=200,
            requires=("fetch_4",),
            read_mode="slice",
        ),
        Step(
            id="summarise_5",
            kind=StepKind.MODEL_CALL,
            produces_tokens=SUMMARY_TOKENS,
            prompt_overhead=200,
            requires=("fetch_5",),
            read_mode="slice",
        ),
        Step(
            id="synthesise",
            kind=StepKind.MODEL_CALL,
            produces_tokens=SYNTHESIS_TOKENS,
            prompt_overhead=300,
            requires=(
                "summarise_1",
                "summarise_2",
                "summarise_3",
                "summarise_4",
                "summarise_5",
            ),
            read_mode="summary",
        ),
    ),
    notes=(
        "Pages average 8k tokens after HTML stripping. Real distribution "
        "is wide (academic papers 30k+, news articles 2k) — 8k is the "
        "median across our test set of 300 sources.",
        "Plinth's per-source summarise step uses slice (1500 tok) reads "
        "to keep summaries grounded in actual content. The final synthesis "
        "uses summary (300 tok) reads — it doesn't need the full pages.",
        "Exceeds 85% sanity floor — justification: the baseline re-sends "
        "all 5 fetched pages (5 × 8k = 40k tokens) on every model_call "
        "after fetch_5. The live demo at examples/01-research-agent "
        "reports ~71% in real runs because real LLM prompts have more "
        "overhead than this model accounts for; the deterministic upper "
        "bound is 85%.",
    ),
)
