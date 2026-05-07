# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 The Plinth Authors
"""The no-Plinth baseline research agent.

This is what an agent looks like without externalised state:

* Memory lives in the conversation history.
* Every reasoning step sends the full history to the LLM.
* Tool calls go directly to the backend — no caching layer.
* Source content gets inlined into the history when fetched, so it ends
  up re-sent on every subsequent reasoning step.

The wasteful pattern is *deliberate and realistic*: it is how most
naive agent implementations actually behave. The headline number for the
demo depends on this baseline being faithful to that anti-pattern, so
we model it carefully and measure tokens at every call.

Run directly with::

    python baseline.py --topic "renewable energy"
"""

from __future__ import annotations

import argparse
import sys
import time
from typing import Any

from shared import (
    FixtureToolBackend,
    HTTPToolBackend,
    ResearchReport,
    ToolCallRecord,
    get_tool_backend,
    llm_call,
    load_topics_config,
)


# ---------------------------------------------------------------------------
# Public entry-point
# ---------------------------------------------------------------------------


def run_baseline(topic: str, mode: str = "simulation") -> ResearchReport:
    """Run the baseline (no-Plinth) agent end-to-end on ``topic``.

    Returns a fully-populated :class:`ResearchReport` with token and
    tool-call accounting that the comparison driver can read.
    """
    record = ResearchReport(topic=topic, report_text="", sources=[])
    backend, _backend_kind = get_tool_backend()
    history: list[tuple[str, str]] = []

    start = time.perf_counter()

    # ------------------------------------------------------------------
    # Step 1 — search the web for sources.
    # ------------------------------------------------------------------
    # The LLM "decides" to search, then we execute the search via tool.
    history.append((
        "user",
        f"You are a research agent. Research the topic '{topic}'. "
        f"Begin by searching the web for 5 high-quality sources. "
        f"For each source, fetch the full content and review it. "
        f"Then extract the key facts from each, and finally synthesise "
        f"a 500-1000 word markdown report citing all of them.",
    ))
    history.append((
        "assistant",
        f"I'll search for sources on '{topic}'. Calling web.search.",
    ))
    # Token accounting on the "decide-to-search" step.
    llm_call(
        list(history),
        step="decide-search",
        purpose="short",
        topic=topic,
        mode=mode,
        record=record,
    )

    search_start = time.perf_counter()
    search_result = backend.search(topic, k=5)
    record.tool_calls.append(
        ToolCallRecord(
            tool="web.search",
            arguments={"query": topic, "k": 5},
            cached=False,
            duration_ms=int((time.perf_counter() - search_start) * 1000),
        )
    )
    sources = search_result["results"]
    record.sources = sources

    # The search results enter the history.
    history.append((
        "tool",
        f"web.search returned {len(sources)} sources:\n"
        + "\n".join(
            f"- {s['title']}: {s['url']} — {s['snippet']}" for s in sources
        ),
    ))

    # ------------------------------------------------------------------
    # Step 2 — fetch each source and fold the content into history.
    # ------------------------------------------------------------------
    # This is the cardinal sin of no-state agents: the source content
    # gets inlined into history, where it then gets re-sent on every
    # subsequent LLM call.
    for src in sources:
        history.append((
            "assistant",
            f"Now fetching {src['url']} so I can review its full content.",
        ))
        # The "decide-to-fetch" reasoning step still pays prompt cost
        # because the LLM is shown the full prior context.
        llm_call(
            list(history),
            step=f"decide-fetch:{src['url']}",
            purpose="short",
            topic=topic,
            mode=mode,
            record=record,
        )

        fetch_start = time.perf_counter()
        fetch_result = backend.fetch(src["url"])
        record.tool_calls.append(
            ToolCallRecord(
                tool="web.fetch",
                arguments={"url": src["url"]},
                cached=False,  # baseline never caches
                duration_ms=int((time.perf_counter() - fetch_start) * 1000),
            )
        )
        content = fetch_result["content"]
        history.append((
            "tool",
            f"web.fetch({src['url']}) returned:\n\n{content}",
        ))

    # ------------------------------------------------------------------
    # Step 3 — extract facts. This is one big LLM call across all the
    # source content concatenated in history. The prompt is small but
    # the *history* is enormous.
    # ------------------------------------------------------------------
    history.append((
        "user",
        "Now extract 3-5 key facts from each source. "
        "Return them as a structured list keyed by source URL.",
    ))
    extraction = llm_call(
        list(history),
        step="extract-facts",
        purpose="extraction",
        topic=topic,
        mode=mode,
        record=record,
    )
    history.append(("assistant", extraction))

    # ------------------------------------------------------------------
    # Step 4 — synthesise the report. Same problem, even bigger context:
    # all source content + the extraction response are now in history.
    # ------------------------------------------------------------------
    history.append((
        "user",
        f"Now synthesise a 500-1000 word markdown report on '{topic}' "
        "citing all sources. Use clear section headings and a final "
        "list of recommendations.",
    ))
    report_text = llm_call(
        list(history),
        step="synthesise",
        purpose="synthesis",
        topic=topic,
        mode=mode,
        record=record,
    )

    record.report_text = report_text
    record.wall_clock_seconds = time.perf_counter() - start
    return record


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _print_summary(report: ResearchReport) -> None:
    print()
    print(f"Baseline agent — topic: {report.topic!r}")
    print(f"  LLM calls          : {report.llm_call_count}")
    print(f"  Input tokens       : {report.total_input_tokens:,}")
    print(f"  Output tokens      : {report.total_output_tokens:,}")
    print(f"  Total tokens       : {report.total_tokens:,}")
    print(f"  Estimated cost USD : {report.total_cost_usd:.4f}")
    print(f"  Tool calls         : {report.tool_call_count}")
    print(f"  Wall clock         : {report.wall_clock_seconds:.2f}s")
    print()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="No-Plinth research-agent baseline.")
    parser.add_argument("--topic", default=None, help="Research topic.")
    parser.add_argument(
        "--mode",
        default="simulation",
        choices=["simulation", "mock-llm", "live"],
        help="LLM mode (simulation/mock-llm are equivalent).",
    )
    args = parser.parse_args(argv)

    topic = args.topic or load_topics_config().get("default_topic", "renewable energy")
    mode = "simulation" if args.mode == "mock-llm" else args.mode

    backend, kind = get_tool_backend()
    if isinstance(backend, FixtureToolBackend):
        print(
            "[baseline] Using bundled fixture content (mock-mcp not reachable). "
            "This is fine for simulation mode."
        )
    elif isinstance(backend, HTTPToolBackend):
        print(f"[baseline] Using HTTP backend at {backend._base_url}")  # noqa: SLF001

    report = run_baseline(topic, mode=mode)
    _print_summary(report)
    return 0


if __name__ == "__main__":
    sys.exit(main())
