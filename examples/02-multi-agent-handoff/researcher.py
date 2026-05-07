# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 The Plinth Authors
"""The Researcher agent.

Reads:  a topic (from CLI / orchestrator).
Writes: ``research-out`` channel + workspace files (``sources/*.txt``,
        ``facts.json``) + workspace KV (``facts/*``, ``sources/meta/*``).

The Researcher's job is to convert a topic into structured, machine-
readable findings the Writer can consume directly. The architectural
shift vs. a naive single-agent pipeline is that the Researcher's
*output* is a structured channel message containing snapshot + KV-key
references, not the raw source text. Downstream agents pull what they
need by key from the workspace; they never see the (~10kt) raw sources
in their prompts.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from typing import Any

from shared import (
    AgentRecord,
    PipelineFacade,
    SnapshotRecord,
    ToolCallRecord,
    _unwrap_invoke,
    agent_llm_call,
    get_pipeline_facade,
    load_topics_config,
    slugify,
)


# ---------------------------------------------------------------------------
# Tool dispatch helpers — gateway when available, fixtures when not.
# ---------------------------------------------------------------------------


def _invoke_tool(
    facade: PipelineFacade,
    tool_id: str,
    arguments: dict[str, Any],
    *,
    record: AgentRecord,
) -> dict[str, Any]:
    """Dispatch ``tool_id`` either via the gateway or via the local fixture."""
    start = time.perf_counter()
    cached = False
    if facade.sdk_client is not None:
        try:
            resp = facade.sdk_client.tools.invoke(
                tool_id,
                arguments,
                workspace_id=facade.workspace_id,
                agent_id="researcher",
            )
            cached = bool(getattr(resp, "cached", False))
            result = _unwrap_invoke(resp)
        except Exception as exc:  # noqa: BLE001
            print(f"[researcher] gateway invoke failed: {exc}; using fixture backend")
            if tool_id == "web.search":
                result = facade.tool_backend.search(
                    arguments.get("query", ""), k=int(arguments.get("k", 5))
                )
            else:
                result = facade.tool_backend.fetch(arguments["url"])
    elif tool_id == "web.search":
        result = facade.tool_backend.search(
            arguments.get("query", ""), k=int(arguments.get("k", 5))
        )
    elif tool_id == "web.fetch":
        result = facade.tool_backend.fetch(arguments["url"])
    else:
        raise ValueError(f"unknown tool {tool_id!r}")
    duration_ms = int((time.perf_counter() - start) * 1000)
    record.tool_calls.append(
        ToolCallRecord(
            tool=tool_id,
            arguments=arguments,
            cached=cached,
            duration_ms=duration_ms,
        )
    )
    return result


# ---------------------------------------------------------------------------
# Public entry-point
# ---------------------------------------------------------------------------


def run_researcher(
    topic: str,
    *,
    facade: PipelineFacade,
    mode: str = "simulation",
) -> AgentRecord:
    """Run the Researcher end-to-end.

    Steps:

    1. Search for ``topic``.
    2. For each source, fetch the content into ``ws.files``; record
       per-source metadata in ``ws.kv``.
    3. Per-source LLM extraction (each call sees one source at a time).
    4. Snapshot the workspace, then send a ``research.complete`` message
       on ``research-out`` carrying snapshot id + KV-key references.
    """
    ws = facade.workspace
    record = AgentRecord(name="researcher")
    ws.kv.set("topic", topic)

    # --- search ---
    search = _invoke_tool(facade, "web.search", {"query": topic, "k": 5}, record=record)
    sources = search.get("results", [])

    # Acknowledgement step (kept tiny — represents the search-decision
    # turn an agent reasoning system would do here).
    agent_llm_call(
        [
            (
                "user",
                f"You are the Researcher. Acknowledge that you'll search "
                f"for sources on '{topic}'.",
            ),
        ],
        step="research:decide-search",
        purpose="short",
        topic=topic,
        mode=mode,
        record=record,
    )

    # --- fetch each source ---
    ws.kv.set("sources/index", [s["url"] for s in sources])
    for src in sources:
        fetched = _invoke_tool(facade, "web.fetch", {"url": src["url"]}, record=record)
        content = fetched["content"]
        ws.files.write(f"sources/{slugify(src['url'])}.txt", content)
        ws.kv.set(
            f"sources/meta/{src['url']}",
            {"title": src["title"], "url": src["url"], "snippet": src.get("snippet", "")},
        )

    # --- per-source extraction ---
    fact_keys: list[str] = []
    facts_summary: list[dict[str, Any]] = []
    for src in sources:
        path = f"sources/{slugify(src['url'])}.txt"
        text = ws.files.read(path, as_text=True)
        if isinstance(text, bytes):
            text = text.decode("utf-8")
        history: list[tuple[str, str]] = [
            (
                "user",
                f"Extract 3-5 key facts from the following source.\n\n"
                f"Source title: {src['title']}\n"
                f"Source URL: {src['url']}\n\n"
                f"---\n{text}\n---",
            ),
        ]
        response = agent_llm_call(
            history,
            step=f"research:extract:{src['url']}",
            purpose="extraction",
            topic=topic,
            mode=mode,
            record=record,
        )
        kv_key = f"facts/{src['url']}"
        ws.kv.set(kv_key, response)
        fact_keys.append(kv_key)
        facts_summary.append({"url": src["url"], "title": src["title"], "facts": response})

    # Persist a flat artifact too — handy for the live demo's "open the
    # workspace and inspect" story. The channel payload will not include
    # this; only the keys.
    ws.files.write("facts.json", json.dumps(facts_summary, indent=2))

    # --- snapshot + handoff ---
    snap = ws.snapshot(
        "research-complete",
        message=f"researcher finished {len(sources)} sources for {topic!r}",
    )
    record.snapshots.append(
        SnapshotRecord(id=snap.id, name=snap.name, agent="researcher")
    )

    ws.channels.send(
        "research-out",
        payload={
            "topic": topic,
            "snapshot_id": snap.id,
            "fact_keys": fact_keys,
            "source_count": len(sources),
            "sources": [{"url": s["url"], "title": s["title"]} for s in sources],
        },
        sender="researcher",
        type="research.complete",
    )
    return record


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Researcher agent (standalone).")
    parser.add_argument("--workspace", required=True, help="Workspace name.")
    parser.add_argument("--topic", default=None, help="Research topic.")
    parser.add_argument(
        "--mode",
        default="simulation",
        choices=["simulation", "live"],
        help="LLM mode (simulation = deterministic mock; live = Anthropic API).",
    )
    parser.add_argument(
        "--record-out",
        default=None,
        help="If given, write the AgentRecord JSON to this path.",
    )
    args = parser.parse_args(argv)

    topic = args.topic or load_topics_config().get("default_topic", "renewable energy")
    facade = get_pipeline_facade(args.workspace)

    print(
        f"[researcher] workspace={args.workspace} mode={args.mode} "
        f"backend={facade.mode_label}"
    )
    rec = run_researcher(topic, facade=facade, mode=args.mode)

    print(
        f"researcher done — LLM calls: {len(rec.llm_calls)}, "
        f"tokens: {rec.total_tokens:,}, "
        f"tool calls: {len(rec.tool_calls)}, "
        f"snapshots: {len(rec.snapshots)}"
    )
    if args.record_out:
        with open(args.record_out, "w") as f:
            json.dump(rec.to_dict(), f, indent=2)
    return 0


if __name__ == "__main__":
    sys.exit(main())
