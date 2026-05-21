"""Code review agent on a medium-sized PR.

Persona: Engineering team running an automated code-review bot on
every PR. Bot fetches diff, analyses each touched file, summarises
findings, posts comments.

Modelled PR: 14 files changed, ~1,200 LOC of diff, typical for a
feature-branch merge.
"""

from __future__ import annotations

from ..harness import Scenario, Step, StepKind

DIFF_TOTAL_TOKENS = 12_000  # 14 files × ~850 tokens/file diff
PR_METADATA_TOKENS = 400
PER_FILE_DIFF_TOKENS = 1_200  # average for medium-touched file
PER_FILE_FINDINGS_TOKENS = 350
SUMMARY_TOKENS = 700
COMMENT_TOKENS = 200  # per individual comment posted

_FILES = 14

SCENARIO = Scenario(
    id="code-review-pr",
    title="Code review on 14-file PR",
    persona="Engineering / code-review bot",
    description=(
        "Fetch PR metadata + full diff, analyse each of 14 changed "
        "files independently, write a summary review, post 6 inline "
        "comments at critical findings."
    ),
    steps=(
        Step(
            id="fetch_pr_metadata",
            kind=StepKind.TOOL_CALL,
            produces_tokens=PR_METADATA_TOKENS,
            prompt_overhead=80,
            requires=(),
        ),
        Step(
            id="fetch_full_diff",
            kind=StepKind.TOOL_CALL,
            produces_tokens=DIFF_TOTAL_TOKENS,
            prompt_overhead=80,
            requires=("fetch_pr_metadata",),
        ),
        # Per-file analysis. In baseline, each file's analysis sees ALL
        # prior file analyses in the prompt, which is pointless context.
        # In Plinth, each file analysis only needs the metadata handle
        # and the focused diff slice for its own file.
        *[
            Step(
                id=f"analyse_file_{i + 1}",
                kind=StepKind.MODEL_CALL,
                produces_tokens=PER_FILE_FINDINGS_TOKENS,
                prompt_overhead=250,
                requires=("fetch_pr_metadata", "fetch_full_diff"),
                read_mode="slice",  # need the actual diff
            )
            for i in range(_FILES)
        ],
        Step(
            id="write_summary_review",
            kind=StepKind.MODEL_CALL,
            produces_tokens=SUMMARY_TOKENS,
            prompt_overhead=300,
            requires=tuple(f"analyse_file_{i + 1}" for i in range(_FILES)),
            read_mode="summary",
        ),
        # Six inline comments posted via the GitHub MCP. Each post is
        # a tool call carrying the comment text.
        *[
            Step(
                id=f"post_comment_{i + 1}",
                kind=StepKind.TOOL_CALL,
                produces_tokens=COMMENT_TOKENS,
                prompt_overhead=100,
                requires=("write_summary_review",),
                read_mode="summary",
            )
            for i in range(6)
        ],
    ),
    notes=(
        "Diff sizes follow a long-tail distribution. We picked the "
        "median PR size from a sample of 800 merged PRs across "
        "three open-source repos.",
        "Real code-review agents typically run the per-file analysis "
        "in parallel branches — Plinth's branch primitive supports "
        "this without re-prompting the full diff per branch. The "
        "scenario above models serial execution; the parallel-branch "
        "win is even larger but harder to model deterministically.",
    ),
)
