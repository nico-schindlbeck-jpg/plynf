# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 The Plynf Authors
"""Tests for the quality-verification harness (plinth_proxy.verify)."""

from __future__ import annotations

import json
from pathlib import Path

from plinth_proxy.policy_engine import load_policy
from plinth_proxy.verify import (
    VerifyReport,
    answers_equivalent,
    normalise_answer,
    render_report,
    run_verification,
)

POLICIES = Path(__file__).parent.parent / "src" / "plinth_proxy" / "policies"


def _spec() -> dict:
    return {
        "connector": "orders",
        "cases": [
            {
                "tool": "get_order",
                "raw_response": {
                    "orderId": "ORD-1",
                    "status": "in_transit",
                    "carrier": "UPS",
                    "internalRoutingCode": "WH3-X",
                    "warehouseNotes": None,
                    "createdAt": "2026-06-01T10:00:00Z",
                },
                "questions": ["What is the status?", "Which carrier?"],
            }
        ],
    }


def _extracting_ask(answers: dict[str, str]):
    """Fake model: answers by question keyword, only if the value is present
    in the provided context — mimicking a model that can only use what it sees."""

    async def ask(system: str, user: str) -> str:
        for keyword, value in answers.items():
            if keyword in user.lower():
                return value if value in user else "UNKNOWN"
        return "UNKNOWN"

    return ask


async def test_equivalent_answers_no_regression():
    policy = load_policy(POLICIES / "orders.default.yaml")
    ask = _extracting_ask({"status": "in_transit", "carrier": "UPS"})
    report = await run_verification(_spec(), policy, ask)

    assert report.equivalence_rate == 1.0
    assert report.regressions == []
    # Shaping must actually save tokens on the bloated payload.
    assert report.token_saved_pct > 0.3
    case = report.cases[0]
    assert case.raw_tokens > case.shaped_tokens > 0


async def test_regression_is_flagged_with_answers():
    """If shaping drops a field the model needed, the harness must flag it."""
    policy = load_policy(POLICIES / "orders.default.yaml")

    # 'internalRoutingCode' is (correctly) not in the keep-list — a question
    # about it answers from raw but not from shaped context.
    spec = _spec()
    spec["cases"][0]["questions"] = ["What is the internalroutingcode?"]
    ask = _extracting_ask({"internalroutingcode": "WH3-X"})

    report = await run_verification(spec, policy, ask)

    assert report.equivalence_rate == 0.0
    assert len(report.regressions) == 1
    tool, q = report.regressions[0]
    assert tool == "get_order"
    assert q.raw_answer == "WH3-X"
    assert q.shaped_answer == "UNKNOWN"
    assert q.method == "mismatch"


async def test_judge_breaks_ties():
    async def judge_equivalent(system: str, user: str) -> str:
        return "EQUIVALENT"

    eq, method = await answers_equivalent("q", "UPS delivers it", "carrier: UPS", judge_equivalent)
    assert eq is True
    assert method == "judge"

    async def judge_different(system: str, user: str) -> str:
        return "DIFFERENT"

    eq, method = await answers_equivalent("q", "UPS", "DHL", judge_different)
    assert eq is False
    assert method == "judge"


async def test_exact_match_skips_judge():
    async def exploding_judge(system: str, user: str) -> str:
        raise AssertionError("judge must not be called on exact matches")

    eq, method = await answers_equivalent("q", '"In Transit." ', "in transit", exploding_judge)
    assert eq is True
    assert method == "exact"


def test_normalise_answer():
    assert normalise_answer('  "UPS".  ') == "ups"
    assert normalise_answer("In   Transit!") == "in transit"
    assert normalise_answer("**1249.99**") == "1249.99"


async def test_report_serialisation_and_rendering():
    policy = load_policy(POLICIES / "orders.default.yaml")
    ask = _extracting_ask({"status": "in_transit", "carrier": "UPS"})
    report = await run_verification(_spec(), policy, ask)

    d = report.to_dict()
    json.dumps(d)  # must be JSON-serialisable
    assert d["equivalence_rate"] == 1.0
    assert d["questions_total"] == 2
    assert d["regressions"] == []

    text = render_report(report)
    assert "TOKENS SAVED" in text
    assert "ANSWERS SAME : 100.0%" in text
    assert "none — shaping did not change a single answer" in text


async def test_empty_spec_is_perfectly_equivalent():
    policy = load_policy(POLICIES / "orders.default.yaml")

    async def ask(system: str, user: str) -> str:  # pragma: no cover
        return ""

    report = await run_verification({"cases": []}, policy, ask)
    assert report.equivalence_rate == 1.0
    assert report.token_saved_pct == 0.0
    assert isinstance(report, VerifyReport)
