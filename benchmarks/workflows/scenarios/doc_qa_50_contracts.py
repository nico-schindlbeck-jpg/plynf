"""Document Q&A across 50 contracts — RAG-style retrieval + citation.

Persona: Legal-ops team running an internal Q&A bot over the contract
library. User asks "show me all MSAs with a non-standard liability cap".
Agent must find them, extract the relevant clauses, write an answer
that cites each.

This is the workflow where Plinth's auto-indexing of writes pays off
the most. The 50 contracts were indexed at write time; the agent's
top-k search returns 8 candidates immediately, never having to
load the other 42.
"""

from __future__ import annotations

from ..harness import Scenario, Step, StepKind

CONTRACT_TOKENS = 25_000  # average MSA / SOW
RELEVANT_CLAUSES_TOKENS = 800  # focused slice when extracted
ANSWER_TOKENS = 1_500
QUESTION_TOKENS = 150

# Workflow: rather than scan all 50, agent first uses vector search
# (a tool call) to find top-8 candidates, then dereferences each
# candidate's "liability cap" clause as a slice.

SCENARIO = Scenario(
    id="doc-qa-50-contracts",
    title="Q&A across 50 contracts with citation",
    persona="Legal ops / internal knowledge bot",
    description=(
        "Given a natural-language question about 50 stored contracts, "
        "find the 8 most relevant, extract the cited clause from each, "
        "compose an answer that cites all 8."
    ),
    steps=(
        Step(
            id="question",
            kind=StepKind.TOOL_CALL,
            produces_tokens=QUESTION_TOKENS,
            prompt_overhead=50,
            requires=(),
        ),
        Step(
            id="vector_search",
            kind=StepKind.TOOL_CALL,
            produces_tokens=2_000,  # 8 hits × ~250 tokens (title + snippet + score)
            prompt_overhead=120,
            requires=("question",),
        ),
        # ─── Per-candidate clause extraction (8 candidates) ─────────
        *[
            Step(
                id=f"extract_clause_{i + 1}",
                kind=StepKind.MODEL_CALL,
                produces_tokens=RELEVANT_CLAUSES_TOKENS,
                prompt_overhead=200,
                # Baseline must load full contract; Plinth uses slice
                # (1500 tokens of focused region).
                requires=("vector_search",),
                read_mode="slice",
            )
            for i in range(8)
        ],
        Step(
            id="compose_answer",
            kind=StepKind.MODEL_CALL,
            produces_tokens=ANSWER_TOKENS,
            prompt_overhead=350,
            requires=tuple(f"extract_clause_{i + 1}" for i in range(8)),
            read_mode="slice",  # need exact citation text
        ),
    ),
    notes=(
        "Critical: the 42 non-matching contracts are NEVER loaded. "
        "Plinth's vector index identifies the 8 candidates from "
        "embeddings; the prompt only carries handles to those 8.",
        "The contracts themselves were indexed at write time (one-shot "
        "cost amortised over all future queries). The scenario only "
        "models the query side.",
        "Baseline approximation: a naive RAG implementation that "
        "doesn't use a vector index would load all 50 contracts × 25k "
        "tokens = 1.25M tokens. Even the 'better baseline' that uses "
        "vector search loads the full 8 hit-contracts. We model this "
        "baseline — the *naive* one would show 95%+ reduction, which "
        "we'd discount as unfair.",
    ),
)
