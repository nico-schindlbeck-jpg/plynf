# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 The Plynf Authors
"""Quality-verification harness (P0.2) — the trust mechanism.

Answers the question **"does shaping ever change the answer?"** with data
instead of assertion. Given a set of tool responses, a set of agent
questions, and a model, it replays the *raw* and the *shaped* context
through the model and diffs the answers. It reports two numbers:

* **% tokens saved** on the tool-response portion, and
* **% answers unchanged** (the answer-equivalence rate),

plus the specific regressions — every (tool, question) where the shaped
context produced a different answer, so the offending keep-list can be fixed.

This is an OFFLINE tool. It may call a model as many times as it likes;
the proxy's hot path stays a pure transform (see PLYNF_ENGINEERING_BRIEF
§4 — invariants).

Usage::

    python -m plinth_proxy.verify spec.yaml \\
        --base-url http://localhost:11434/v1 --model llama3.1 \\
        [--judge-model llama3.1] [--json] [--fail-under 1.0]

Spec format (YAML)::

    connector: orders          # which shipped/custom policy to shape with
    cases:
      - tool: get_order
        raw_response: { ... the bloated payload ... }
        questions:
          - "What is the order status?"
          - "Which carrier delivers it, and when?"

Equivalence is decided deterministically first (normalised string match);
if the strings differ and a judge model is configured, the judge breaks
the tie. Without a judge, a string mismatch counts as a regression —
conservative by design: we'd rather flag a false positive than miss a
changed answer.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import re
import sys
from collections.abc import Awaitable, Callable
from dataclasses import asdict, dataclass, field
from pathlib import Path

import yaml

from .policy_engine import ConnectorPolicy, load_all_policies
from .tokens import count_json_tokens

# An "ask" function: (system_prompt, user_prompt) -> model answer.
AskFn = Callable[[str, str], Awaitable[str]]

ANSWER_SYSTEM_PROMPT = (
    "You are a precise assistant. Answer the question using ONLY the JSON "
    "tool response provided. Reply with the minimal value or phrase that "
    "answers the question — no explanations, no restating the question. "
    "If the tool response does not contain the answer, reply exactly: UNKNOWN"
)

JUDGE_SYSTEM_PROMPT = (
    "You compare two answers to the same question. Reply with exactly one "
    "word: EQUIVALENT if they convey the same information (wording, casing, "
    "formatting and irrelevant detail may differ), or DIFFERENT otherwise."
)


# ---------------------------------------------------------------------------
# Result model
# ---------------------------------------------------------------------------


@dataclass
class QuestionResult:
    question: str
    raw_answer: str
    shaped_answer: str
    equivalent: bool
    method: str  # "exact" | "judge" | "mismatch"


@dataclass
class CaseResult:
    tool: str
    raw_tokens: int
    shaped_tokens: int
    questions: list[QuestionResult] = field(default_factory=list)

    @property
    def saved_pct(self) -> float:
        if self.raw_tokens == 0:
            return 0.0
        return (self.raw_tokens - self.shaped_tokens) / self.raw_tokens


@dataclass
class VerifyReport:
    cases: list[CaseResult]

    @property
    def total_raw_tokens(self) -> int:
        return sum(c.raw_tokens for c in self.cases)

    @property
    def total_shaped_tokens(self) -> int:
        return sum(c.shaped_tokens for c in self.cases)

    @property
    def token_saved_pct(self) -> float:
        if self.total_raw_tokens == 0:
            return 0.0
        return (self.total_raw_tokens - self.total_shaped_tokens) / self.total_raw_tokens

    @property
    def question_results(self) -> list[tuple[str, QuestionResult]]:
        return [(c.tool, q) for c in self.cases for q in c.questions]

    @property
    def equivalence_rate(self) -> float:
        qs = self.question_results
        if not qs:
            return 1.0
        return sum(1 for _, q in qs if q.equivalent) / len(qs)

    @property
    def regressions(self) -> list[tuple[str, QuestionResult]]:
        return [(tool, q) for tool, q in self.question_results if not q.equivalent]

    def to_dict(self) -> dict:
        return {
            "token_saved_pct": round(self.token_saved_pct, 4),
            "equivalence_rate": round(self.equivalence_rate, 4),
            "total_raw_tokens": self.total_raw_tokens,
            "total_shaped_tokens": self.total_shaped_tokens,
            "questions_total": len(self.question_results),
            "regressions": [
                {"tool": tool, **asdict(q)} for tool, q in self.regressions
            ],
            "cases": [
                {
                    "tool": c.tool,
                    "raw_tokens": c.raw_tokens,
                    "shaped_tokens": c.shaped_tokens,
                    "saved_pct": round(c.saved_pct, 4),
                    "questions": [asdict(q) for q in c.questions],
                }
                for c in self.cases
            ],
        }


# ---------------------------------------------------------------------------
# Equivalence
# ---------------------------------------------------------------------------


def normalise_answer(s: str) -> str:
    """Casing-, whitespace- and punctuation-insensitive canonical form."""
    s = s.strip().lower()
    s = re.sub(r"[\"'`*_]", "", s)  # quotes / markdown emphasis
    s = re.sub(r"[.,;:!?]+$", "", s)  # trailing punctuation
    return re.sub(r"\s+", " ", s)


async def answers_equivalent(
    question: str,
    raw_answer: str,
    shaped_answer: str,
    judge: AskFn | None,
) -> tuple[bool, str]:
    """Return (equivalent, method). Deterministic first, judge as tiebreak."""
    if normalise_answer(raw_answer) == normalise_answer(shaped_answer):
        return True, "exact"
    if judge is not None:
        verdict = await judge(
            JUDGE_SYSTEM_PROMPT,
            f"Question: {question}\n\nAnswer A: {raw_answer}\n\nAnswer B: {shaped_answer}",
        )
        if "EQUIVALENT" in verdict.strip().upper():
            return True, "judge"
        return False, "judge"
    return False, "mismatch"


# ---------------------------------------------------------------------------
# Core run
# ---------------------------------------------------------------------------


def _context_prompt(question: str, payload) -> str:
    serialised = json.dumps(payload, separators=(",", ":"), default=str)
    return f"Tool response:\n```json\n{serialised}\n```\n\nQuestion: {question}"


async def run_verification(
    spec: dict,
    policy: ConnectorPolicy,
    ask: AskFn,
    judge: AskFn | None = None,
) -> VerifyReport:
    """Replay every (case, question) with raw vs shaped context and diff."""
    from .policy_engine import apply  # local import keeps module load light

    cases: list[CaseResult] = []
    for case in spec.get("cases", []):
        tool = case["tool"]
        raw = case["raw_response"]
        shaped = apply(raw, policy.policy_for(tool))

        result = CaseResult(
            tool=tool,
            raw_tokens=count_json_tokens(raw),
            shaped_tokens=count_json_tokens(shaped),
        )

        for q in case.get("questions", []):
            question = q if isinstance(q, str) else q["q"]
            raw_answer = await ask(ANSWER_SYSTEM_PROMPT, _context_prompt(question, raw))
            shaped_answer = await ask(ANSWER_SYSTEM_PROMPT, _context_prompt(question, shaped))
            equivalent, method = await answers_equivalent(
                question, raw_answer, shaped_answer, judge
            )
            result.questions.append(
                QuestionResult(
                    question=question,
                    raw_answer=raw_answer,
                    shaped_answer=shaped_answer,
                    equivalent=equivalent,
                    method=method,
                )
            )

        cases.append(result)

    return VerifyReport(cases=cases)


# ---------------------------------------------------------------------------
# OpenAI-compatible transport (works for OpenAI, Ollama, vLLM, the proxy, …)
# ---------------------------------------------------------------------------


def make_openai_ask(base_url: str, api_key: str, model: str) -> AskFn:
    import httpx

    async def ask(system: str, user: str) -> str:
        async with httpx.AsyncClient(timeout=120.0) as client:
            resp = await client.post(
                f"{base_url.rstrip('/')}/chat/completions",
                headers={"Authorization": f"Bearer {api_key}"},
                json={
                    "model": model,
                    "temperature": 0,
                    "messages": [
                        {"role": "system", "content": system},
                        {"role": "user", "content": user},
                    ],
                },
            )
            resp.raise_for_status()
            return resp.json()["choices"][0]["message"]["content"] or ""

    return ask


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------


def render_report(report: VerifyReport) -> str:
    lines: list[str] = []
    lines.append("")
    lines.append("Plynf quality verification — raw vs shaped context")
    lines.append("=" * 60)
    for c in report.cases:
        lines.append(
            f"\n{c.tool}:  {c.raw_tokens} → {c.shaped_tokens} tokens "
            f"(−{c.saved_pct:.0%})"
        )
        for q in c.questions:
            mark = "✓" if q.equivalent else "✗"
            lines.append(f"  {mark} [{q.method}] {q.question}")
            if not q.equivalent:
                lines.append(f"      raw    → {q.raw_answer}")
                lines.append(f"      shaped → {q.shaped_answer}")
    lines.append("\n" + "-" * 60)
    lines.append(
        f"TOKENS SAVED : {report.token_saved_pct:.1%} "
        f"({report.total_raw_tokens} → {report.total_shaped_tokens})"
    )
    lines.append(
        f"ANSWERS SAME : {report.equivalence_rate:.1%} "
        f"({len(report.question_results) - len(report.regressions)}"
        f"/{len(report.question_results)})"
    )
    if report.regressions:
        lines.append(f"REGRESSIONS  : {len(report.regressions)} — fix the keep-list(s) above")
    else:
        lines.append("REGRESSIONS  : none — shaping did not change a single answer")
    lines.append("")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _find_policy(policies: dict[str, ConnectorPolicy], connector: str) -> ConnectorPolicy:
    if connector in policies:
        return policies[connector]
    available = ", ".join(sorted(policies)) or "(none)"
    sys.exit(f"unknown connector {connector!r} — available: {available}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m plinth_proxy.verify",
        description="Replay raw vs shaped tool responses through a model and diff the answers.",
    )
    parser.add_argument("spec", help="YAML spec: connector + cases (tool, raw_response, questions)")
    parser.add_argument("--base-url", default="https://api.openai.com/v1")
    parser.add_argument("--api-key", default="", help="defaults to $OPENAI_API_KEY")
    parser.add_argument("--model", required=True)
    parser.add_argument("--judge-model", default="", help="optional tiebreak judge model")
    parser.add_argument("--policies-dir", default="", help="defaults to the shipped policies")
    parser.add_argument("--json", action="store_true", help="machine-readable output")
    parser.add_argument(
        "--fail-under",
        type=float,
        default=1.0,
        help="exit 1 if equivalence rate is below this (default 1.0 — any regression fails)",
    )
    args = parser.parse_args(argv)

    import os

    api_key = args.api_key or os.environ.get("OPENAI_API_KEY", "unused")

    spec = yaml.safe_load(Path(args.spec).read_text(encoding="utf-8"))
    if not isinstance(spec, dict) or "connector" not in spec:
        sys.exit("spec must be a YAML mapping with a 'connector' key")

    policies_dir = args.policies_dir or str(Path(__file__).parent / "policies")
    policy = _find_policy(load_all_policies(policies_dir), spec["connector"])

    ask = make_openai_ask(args.base_url, api_key, args.model)
    judge = (
        make_openai_ask(args.base_url, api_key, args.judge_model)
        if args.judge_model
        else None
    )

    report = asyncio.run(run_verification(spec, policy, ask, judge))

    if args.json:
        print(json.dumps(report.to_dict(), indent=2, ensure_ascii=False))
    else:
        print(render_report(report))

    return 0 if report.equivalence_rate >= args.fail_under else 1


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
