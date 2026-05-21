"""Multi-agent content pipeline — Researcher → Writer → Reviewer × 3 rounds.

Persona: Content team building a marketing-blog automation. Three
specialist agents each have their own narrow prompt:
  - Researcher fetches sources + writes a brief
  - Writer turns brief into draft
  - Reviewer critiques the draft

If the reviewer rejects, writer revises (round 2). Up to 3 rounds.
This is the existing examples/02-multi-agent-handoff workflow modelled
as a benchmark scenario.

Plinth advantage: each agent's prompt only contains what THAT AGENT
needs to see. Reviewer never sees the raw research sources; researcher
never sees the previous reviewer feedback. The baseline can't do this
because there's only one chat history shared by everyone.
"""

from __future__ import annotations

from ..harness import Scenario, Step, StepKind

SOURCE_TOKENS = 7_000
BRIEF_TOKENS = 1_200
DRAFT_TOKENS = 2_500
REVIEW_TOKENS = 600

SCENARIO = Scenario(
    id="multi-agent-content",
    title="Researcher → Writer → Reviewer (3 rounds)",
    persona="Content automation / 3-agent pipeline",
    description=(
        "Researcher fetches 4 sources + writes a brief. Writer drafts. "
        "Reviewer critiques. Round 2 and 3 repeat writer + reviewer "
        "until reviewer approves (modelled as fixed 3 rounds)."
    ),
    steps=(
        # ─── Round 1 ─────────────────────────────────────────────────
        Step(
            id="r1_fetch_1",
            kind=StepKind.TOOL_CALL,
            produces_tokens=SOURCE_TOKENS,
            prompt_overhead=60,
            requires=(),
        ),
        Step(
            id="r1_fetch_2",
            kind=StepKind.TOOL_CALL,
            produces_tokens=SOURCE_TOKENS,
            prompt_overhead=60,
            requires=(),
        ),
        Step(
            id="r1_fetch_3",
            kind=StepKind.TOOL_CALL,
            produces_tokens=SOURCE_TOKENS,
            prompt_overhead=60,
            requires=(),
        ),
        Step(
            id="r1_fetch_4",
            kind=StepKind.TOOL_CALL,
            produces_tokens=SOURCE_TOKENS,
            prompt_overhead=60,
            requires=(),
        ),
        Step(
            id="r1_brief",
            kind=StepKind.MODEL_CALL,
            produces_tokens=BRIEF_TOKENS,
            prompt_overhead=250,
            requires=("r1_fetch_1", "r1_fetch_2", "r1_fetch_3", "r1_fetch_4"),
            read_mode="slice",  # Researcher needs the actual content
        ),
        Step(
            id="r1_draft",
            kind=StepKind.MODEL_CALL,
            produces_tokens=DRAFT_TOKENS,
            prompt_overhead=300,
            requires=("r1_brief",),  # Writer only needs the brief, not raw sources
            read_mode="slice",
        ),
        Step(
            id="r1_review",
            kind=StepKind.MODEL_CALL,
            produces_tokens=REVIEW_TOKENS,
            prompt_overhead=250,
            requires=("r1_draft",),  # Reviewer only sees the draft
            read_mode="slice",
        ),
        # ─── Round 2 ─────────────────────────────────────────────────
        Step(
            id="r2_draft",
            kind=StepKind.MODEL_CALL,
            produces_tokens=DRAFT_TOKENS,
            prompt_overhead=300,
            # Writer needs: previous draft + review notes. Not original sources.
            requires=("r1_draft", "r1_review"),
            read_mode="slice",
        ),
        Step(
            id="r2_review",
            kind=StepKind.MODEL_CALL,
            produces_tokens=REVIEW_TOKENS,
            prompt_overhead=250,
            requires=("r2_draft",),
            read_mode="slice",
        ),
        # ─── Round 3 ─────────────────────────────────────────────────
        Step(
            id="r3_draft",
            kind=StepKind.MODEL_CALL,
            produces_tokens=DRAFT_TOKENS,
            prompt_overhead=300,
            requires=("r2_draft", "r2_review"),
            read_mode="slice",
        ),
        Step(
            id="r3_review",
            kind=StepKind.MODEL_CALL,
            produces_tokens=REVIEW_TOKENS,
            prompt_overhead=250,
            requires=("r3_draft",),
            read_mode="slice",
        ),
    ),
    notes=(
        "The huge win here is role-scoping: in baseline, the reviewer's "
        "prompt by round 3 contains the original 4 sources, the brief, "
        "all 3 drafts, and 2 prior reviews. Plinth's reviewer only sees "
        "the current draft (1 handle, 1 slice).",
        "Modelled as 3 fixed rounds. Real workflows abort early on "
        "reviewer approval; the average in our test set was 2.3 rounds.",
    ),
)
