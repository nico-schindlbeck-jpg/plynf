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
import re
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


def _fragment_present(frag: str, haystack: str) -> bool:
    """Format-tolerant containment: exact, numeric-normalised, or token-wise.

    The recorded answer may format values differently from the JSON
    serialisation ("184000.50" vs 184000.5, "the 502s stopped…" vs
    "502s stopped…"). We accept the fragment if (a) it appears verbatim,
    (b) it is a number whose canonical form appears, or (c) every salient
    token (len ≥ 3) appears — strict enough that a dropped field
    ("samsara") still fails.
    """
    if frag in haystack:
        return True
    try:
        num = float(frag.replace(",", ""))
    except ValueError:
        num = None
    if num is not None:
        return f"{num:g}" in haystack
    tokens = re.findall(r"[a-z0-9:.\-]{3,}", frag)
    return bool(tokens) and all(t in haystack for t in tokens)


def cmd_gate(args: argparse.Namespace) -> int:
    """Deterministic CI gate: no recorded answer may vanish from the shaped context.

    For every (tool, question) whose recorded run answered from the SHAPED
    context (shaped_answer != UNKNOWN and equivalent to raw), assert that
    the answer text still occurs in the response shaped with the CURRENT
    policies. If a policy edit drops a field the model needed (the
    Competitor__c class of bug), this fails — without any model call.
    """
    answers = json.loads(Path(args.answers).read_text(encoding="utf-8"))
    by_tool: dict[str, list[dict]] = {}
    for a in answers:
        by_tool.setdefault(a["tool"], []).append(a)

    violations: list[str] = []
    checked = 0
    for spec_path in args.specs:
        spec, policy = _load(spec_path, args.policies_dir)
        for case in spec.get("cases", []):
            tool = case["tool"]
            shaped = apply(case["raw_response"], policy.policy_for(tool))
            haystack = json.dumps(shaped, ensure_ascii=False, default=str).lower()
            for rec in by_tool.get(tool, []):
                ans = str(rec.get("shaped_answer", "")).strip()
                raw_ans = str(rec.get("raw_answer", "")).strip()
                # Only gate answers the shaped context demonstrably contained.
                if not ans or ans.upper() == "UNKNOWN" or ans != raw_ans:
                    continue
                checked += 1
                # Multi-part answers ("Logistics, 310 employees"): every
                # value-ish fragment must survive.
                fragments = [
                    f.strip().lower()
                    for f in re.split(r"[,;]| and | closing ", ans)
                    if f.strip()
                ]
                for frag in fragments:
                    frag_core = frag.replace(" employees", "").strip()
                    if frag_core and not _fragment_present(frag_core, haystack):
                        violations.append(
                            f"{tool} / {rec['question']!r}: fragment {frag_core!r} "
                            f"no longer present in the shaped response"
                        )

    print(f"policy gate: {checked} recorded answers checked against current policies")
    if violations:
        print("\nREGRESSIONS — a policy edit dropped information the model needed:")
        for v in violations:
            print(f"  ✗ {v}")
        return 1
    print("✓ every recorded answer is still derivable from the shaped context")
    return 0


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

    p_gate = sub.add_parser(
        "gate", help="CI gate: recorded answers must survive the current policies"
    )
    p_gate.add_argument("answers")
    p_gate.add_argument("specs", nargs="+")
    p_gate.add_argument("--policies-dir", default=None)
    p_gate.set_defaults(fn=cmd_gate)

    args = parser.parse_args()
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
