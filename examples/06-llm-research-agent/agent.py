# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 The Plinth Authors
"""Research agent built on the v1.2 ``client.llm`` namespace.

Mirrors :mod:`examples/01-research-agent/with_plinth.py` (the
state-externalising agent) but goes through the new SDK surface for
every LLM call:

* ``client.llm.use_provider("mock", responses=[...])`` for the demo
  default — no API key, no network egress, deterministic output.
* ``client.llm.complete(...)`` for each reasoning step — driving real
  cost & token accounting through Plinth's audit pipeline.
* ``--mode=live`` switches the provider to ``"anthropic"`` and uses the
  ``ANTHROPIC_API_KEY`` env var; the Plinth audit endpoint records the
  per-call cost so the dashboard / Prometheus pick it up.

The agent intentionally exercises three reasoning shapes the LLM layer
needs to handle: short prompts ("decide-search"), per-source extraction
prompts (longest input tokens), and a synthesis prompt (longest output
tokens). When run against the mock provider, the same canned response
list is reused so the output is deterministic across runs.
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
import time
from dataclasses import dataclass, field
from typing import Any

from plinth import LLMResponse, Plinth

# Quiet the SDK's failover logger when running the demo without a
# gateway — the audit-recording POST will warn on every call otherwise.
# Real applications usually have a gateway up and don't see this.
logging.getLogger("plinth.sdk.http").setLevel(logging.ERROR)


# ---------------------------------------------------------------------------
# Sample sources — the agent operates on a fixed corpus so demos are
# reproducible without web access. Real workloads would call out to
# ``client.tools.invoke("web.search", ...)`` for these.
# ---------------------------------------------------------------------------


SAMPLE_SOURCES: list[dict[str, str]] = [
    {
        "url": "https://example.test/renewable-energy-1",
        "title": "Solar power adoption — 2025 trends",
        "content": (
            "Global solar generation grew 28% year-over-year, with new "
            "utility-scale capacity outpacing wind for the second year. "
            "Storage co-location is rising sharply: 41% of new utility "
            "solar projects now pair with batteries."
        ),
    },
    {
        "url": "https://example.test/renewable-energy-2",
        "title": "Offshore wind — capacity additions",
        "content": (
            "Offshore wind added 11 GW worldwide in 2024. Floating "
            "turbines crossed cost parity with fixed-bottom in deep-water "
            "deployments. Permitting timelines remain the chief bottleneck."
        ),
    },
    {
        "url": "https://example.test/renewable-energy-3",
        "title": "Green hydrogen — electrolyzer scale-up",
        "content": (
            "Electrolyzer manufacturing capacity tripled, but project "
            "starts lagged. Cost projections for 2030 fell another 18% "
            "thanks to cheaper PEM stacks. Off-takers remain scarce."
        ),
    },
]


# ---------------------------------------------------------------------------
# Mock canned responses for each LLM step. Each role is short to keep the
# demo terse and to make token counts easy to reason about.
# ---------------------------------------------------------------------------


MOCK_RESPONSES: list[str] = [
    # Step 1: decide-to-search
    "I'll search for sources on the topic via the workspace's tool gateway.",
    # Step 2..4: per-source extraction (3 sources)
    "Key facts: solar grew 28%, batteries pair with 41% of new projects.",
    "Key facts: 11 GW offshore wind added; floating turbines viable.",
    "Key facts: electrolyzer capacity tripled; 2030 cost projections fell.",
    # Step 5: synthesis
    (
        "## Renewable Energy Outlook\n\n"
        "Three high-signal trends emerged in 2025:\n\n"
        "- **Solar + storage**: 41% of new utility solar pairs with batteries "
        "(see https://example.test/renewable-energy-1).\n"
        "- **Offshore wind scaling**: 11 GW added; floating turbines parity "
        "(https://example.test/renewable-energy-2).\n"
        "- **Hydrogen capacity overhang**: electrolyzers tripled but projects "
        "lagged off-takers (https://example.test/renewable-energy-3).\n\n"
        "Recommendation: prioritise battery+solar co-location and floating "
        "wind for deep-water sites; defer hydrogen capex pending firmer "
        "demand signals."
    ),
]


# ---------------------------------------------------------------------------
# Per-call accounting
# ---------------------------------------------------------------------------


@dataclass
class StepRecord:
    """One LLM step's accounting."""

    step: str
    input_tokens: int
    output_tokens: int
    cost_usd: float
    duration_ms: int
    audit_id: str | None


@dataclass
class AgentReport:
    """Full run summary."""

    topic: str
    provider: str
    mode: str
    steps: list[StepRecord] = field(default_factory=list)
    report_text: str = ""
    wall_clock_seconds: float = 0.0

    @property
    def total_input_tokens(self) -> int:
        return sum(s.input_tokens for s in self.steps)

    @property
    def total_output_tokens(self) -> int:
        return sum(s.output_tokens for s in self.steps)

    @property
    def total_cost_usd(self) -> float:
        return sum(s.cost_usd for s in self.steps)


# ---------------------------------------------------------------------------
# Agent body
# ---------------------------------------------------------------------------


def _record(report: AgentReport, *, step: str, response: LLMResponse) -> None:
    """Append a step to the report."""
    report.steps.append(
        StepRecord(
            step=step,
            input_tokens=response.input_tokens,
            output_tokens=response.output_tokens,
            cost_usd=response.cost_usd,
            duration_ms=response.duration_ms,
            audit_id=response.audit_id,
        )
    )


def run(topic: str, *, client: Plinth, mode: str, model: str) -> AgentReport:
    """Drive the research agent end-to-end with ``client.llm``."""
    report = AgentReport(
        topic=topic,
        provider=client.llm.provider.name if client.llm.provider else "?",
        mode=mode,
    )
    start = time.perf_counter()

    # ------------------------------------------------------------------
    # Step 1 — Decide-to-search. Short prompt; small response.
    # ------------------------------------------------------------------
    decide = client.llm.complete(
        model=model,
        messages=[
            {
                "role": "system",
                "content": (
                    "You are a research agent on the Plinth substrate. "
                    "Decide whether to search and announce the call."
                ),
            },
            {"role": "user", "content": f"Research the topic '{topic}'."},
        ],
        max_tokens=128,
        agent_id="example-06",
    )
    _record(report, step="decide-search", response=decide)

    # ------------------------------------------------------------------
    # Step 2..N — Per-source extraction. Each call sees ONLY its source.
    # This is the central architectural shift vs. the no-Plinth baseline:
    # source content lives in the workspace, not in LLM history.
    # ------------------------------------------------------------------
    facts_by_url: dict[str, str] = {}
    for src in SAMPLE_SOURCES:
        extraction = client.llm.complete(
            model=model,
            messages=[
                {
                    "role": "system",
                    "content": "Extract 3 key facts from the source.",
                },
                {
                    "role": "user",
                    "content": (
                        f"Title: {src['title']}\n"
                        f"URL: {src['url']}\n\n"
                        f"---\n{src['content']}\n---"
                    ),
                },
            ],
            max_tokens=256,
            agent_id="example-06",
        )
        facts_by_url[src["url"]] = extraction.content
        _record(report, step=f"extract:{src['url']}", response=extraction)

    # ------------------------------------------------------------------
    # Step N+1 — Synthesis. Sees only the structured facts (small).
    # ------------------------------------------------------------------
    facts_block = "\n\n".join(
        f"### Facts from {url}\n{facts}" for url, facts in facts_by_url.items()
    )
    synthesis = client.llm.complete(
        model=model,
        messages=[
            {
                "role": "system",
                "content": (
                    "Synthesize a short markdown report citing each source."
                ),
            },
            {
                "role": "user",
                "content": f"Topic: {topic}\n\n{facts_block}",
            },
        ],
        max_tokens=1024,
        agent_id="example-06",
    )
    _record(report, step="synthesise", response=synthesis)
    report.report_text = synthesis.content
    report.wall_clock_seconds = time.perf_counter() - start
    return report


# ---------------------------------------------------------------------------
# Driver
# ---------------------------------------------------------------------------


def configure_client(mode: str) -> tuple[Plinth, str]:
    """Build a Plinth client and configure its LLM provider for ``mode``.

    Returns ``(client, model_name)``.
    """
    client = Plinth(
        # Default URLs — example never asserts the gateway is reachable;
        # if it isn't, audit recording silently fails (by design).
        api_key=os.environ.get("PLINTH_API_KEY", "local-dev"),
    )
    if mode == "live":
        client.llm.use_provider(
            "anthropic",
            api_key=os.environ.get("ANTHROPIC_API_KEY"),
        )
        model = os.environ.get("PLINTH_LIVE_MODEL", "claude-sonnet-4-5")
    elif mode == "openai":
        client.llm.use_provider(
            "openai",
            api_key=os.environ.get("OPENAI_API_KEY"),
        )
        model = os.environ.get("PLINTH_OPENAI_MODEL", "gpt-5-mini")
    else:
        # mock (default)
        client.llm.use_provider("mock", responses=MOCK_RESPONSES)
        model = "mock-default"
    return client, model


def _print_summary(report: AgentReport) -> None:
    print()
    print(f"LLM research agent — topic: {report.topic!r}")
    print(f"  Provider           : {report.provider}")
    print(f"  Mode               : {report.mode}")
    print(f"  LLM calls          : {len(report.steps)}")
    print(f"  Input tokens       : {report.total_input_tokens:,}")
    print(f"  Output tokens      : {report.total_output_tokens:,}")
    print(f"  Total tokens       : {report.total_input_tokens + report.total_output_tokens:,}")
    print(f"  Estimated cost USD : {report.total_cost_usd:.6f}")
    print(f"  Wall clock         : {report.wall_clock_seconds:.2f}s")
    audit_ids = [s.audit_id for s in report.steps if s.audit_id]
    if audit_ids:
        print(f"  Audit events       : {len(audit_ids)} recorded")
    else:
        print("  Audit events       : 0 (gateway not reachable, expected in offline mode)")
    print()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="LLM-research-agent demo using the Plinth SDK's client.llm namespace."
    )
    parser.add_argument("--topic", default="renewable energy", help="Research topic.")
    parser.add_argument(
        "--mode",
        default="mock",
        choices=["mock", "live", "openai"],
        help="LLM provider mode. mock=MockProvider (default), live=Anthropic, openai=OpenAI.",
    )
    args = parser.parse_args(argv)

    if args.mode == "live" and not os.environ.get("ANTHROPIC_API_KEY"):
        print(
            "[example06] ANTHROPIC_API_KEY not set; falling back to mock mode.",
            file=sys.stderr,
        )
        args.mode = "mock"
    if args.mode == "openai" and not os.environ.get("OPENAI_API_KEY"):
        print(
            "[example06] OPENAI_API_KEY not set; falling back to mock mode.",
            file=sys.stderr,
        )
        args.mode = "mock"

    client, model = configure_client(args.mode)
    try:
        report = run(args.topic, client=client, mode=args.mode, model=model)
    finally:
        client.close()

    _print_summary(report)
    print("--- Report ---")
    print(report.report_text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
