#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 The Plynf Authors
"""Replay-mode driver for the quality-verification harness.

`python -m plinth_proxy.verify` talks to a live OpenAI-compatible endpoint.
This script supports the *replay* workflow instead: answers are produced
out-of-band (by a human-supervised frontier model, a batch job, a design
partner's transcript …) and fed back in, so the canonical report machinery
(equivalence rules, regression listing, token math) stays the single source
of truth.

Step 1 — emit the contexts the model must answer from::

    python scripts/verify_replay.py contexts SPEC.yaml > contexts.json

  contexts.json contains, per (case, question), the system prompt and both
  user prompts (raw + shaped). Answer each pair INDEPENDENTLY — the model
  answering the shaped context must never see the raw one.

Step 2 — produce the canonical report from the collected answers::

    python scripts/verify_replay.py report ANSWERS.json SPEC.yaml [SPEC2.yaml ...] [--json]

  ANSWERS.json: [{"tool": ..., "question": ..., "raw_answer": ...,
                  "shaped_answer": ...}, ...]
  Multiple specs (different connectors) are merged into one report.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

import yaml

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "services" / "proxy" / "src"))

from plinth_proxy.policy_engine import apply, load_all_policies  # noqa: E402
from plinth_proxy.verify import (  # noqa: E402
    ANSWER_SYSTEM_PROMPT,
    _context_prompt,
    render_report,
    run_verification,
)

POLICIES_DIR = REPO / "services" / "proxy" / "src" / "plinth_proxy" / "policies"


def _load(spec_path: str, policies_dir: str | None = None):
    spec = yaml.safe_load(Path(spec_path).read_text(encoding="utf-8"))
    policies = load_all_policies(Path(policies_dir) if policies_dir else POLICIES_DIR)
    connector = spec["connector"]
    if connector not in policies:
        sys.exit(f"unknown connector {connector!r}")
    return spec, policies[connector]


def cmd_contexts(args: argparse.Namespace) -> int:
    spec, policy = _load(args.spec, args.policies_dir)
    out = []
    for case in spec.get("cases", []):
        tool = case["tool"]
        raw = case["raw_response"]
        shaped = apply(raw, policy.policy_for(tool))
        for q in case.get("questions", []):
            question = q if isinstance(q, str) else q["q"]
            out.append(
                {
                    "tool": tool,
                    "question": question,
                    "system": ANSWER_SYSTEM_PROMPT,
                    "raw_prompt": _context_prompt(question, raw),
                    "shaped_prompt": _context_prompt(question, shaped),
                }
            )
    json.dump(out, sys.stdout, indent=2, ensure_ascii=False)
    print()
    return 0


def cmd_report(args: argparse.Namespace) -> int:
    from plinth_proxy.verify import VerifyReport

    answers = json.loads(Path(args.answers).read_text(encoding="utf-8"))

    # Index answers by (tool, question). The fake "ask" function looks up the
    # recorded answer; raw vs shaped is told apart by which serialised payload
    # the prompt embeds.
    by_key: dict[tuple[str, str], dict] = {
        (a["tool"], a["question"]): a for a in answers
    }

    all_cases = []
    missing: list[str] = []

    for spec_path in args.specs:
        spec, policy = _load(spec_path, args.policies_dir)

        # Build prompt → (key, kind) lookup so ask() resolves deterministically.
        prompt_lookup: dict[str, tuple[tuple[str, str], str]] = {}
        for case in spec.get("cases", []):
            tool = case["tool"]
            raw = case["raw_response"]
            shaped = apply(raw, policy.policy_for(tool))
            for q in case.get("questions", []):
                question = q if isinstance(q, str) else q["q"]
                prompt_lookup[_context_prompt(question, raw)] = ((tool, question), "raw_answer")
                prompt_lookup[_context_prompt(question, shaped)] = (
                    (tool, question),
                    "shaped_answer",
                )

        async def ask(system: str, user: str, _lookup=prompt_lookup) -> str:
            key, kind = _lookup[user]
            rec = by_key.get(key)
            if rec is None or kind not in rec:
                missing.append(f"{key[0]} / {key[1]} ({kind})")
                return "<MISSING ANSWER>"
            return rec[kind]

        all_cases.extend(asyncio.run(run_verification(spec, policy, ask)).cases)

    report = VerifyReport(cases=all_cases)

    if missing:
        sys.exit("missing answers for: " + "; ".join(sorted(set(missing))))

    if args.json:
        print(json.dumps(report.to_dict(), indent=2, ensure_ascii=False))
    else:
        print(render_report(report))
    return 0 if report.equivalence_rate >= args.fail_under else 1


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_ctx = sub.add_parser("contexts", help="emit per-question raw+shaped prompts")
    p_ctx.add_argument("spec")
    p_ctx.add_argument("--policies-dir", default=None)
    p_ctx.set_defaults(fn=cmd_contexts)

    p_rep = sub.add_parser("report", help="build the canonical report from answers")
    p_rep.add_argument("answers")
    p_rep.add_argument("specs", nargs="+")
    p_rep.add_argument("--policies-dir", default=None)
    p_rep.add_argument("--json", action="store_true")
    p_rep.add_argument("--fail-under", type=float, default=0.0)
    p_rep.set_defaults(fn=cmd_report)

    args = parser.parse_args()
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
