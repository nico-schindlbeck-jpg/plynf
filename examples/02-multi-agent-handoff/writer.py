# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 The Plinth Authors
"""The Writer agent.

Reads:  ``research-out`` channel (waits for a ``research.complete`` msg)
Writes: ``writer-out`` channel + workspace ``draft.md`` file.

The Writer never sees raw source content. The handoff message contains
only the snapshot id and a list of KV-key references; the Writer pulls
the small structured fact bullets it actually needs from the workspace
and ignores everything else. Its prompt is bounded by the size of the
fact summaries (~2,000 tokens) rather than by the corpus size
(~10,000 tokens) — that's the architectural payoff of the channel-plus-
workspace pattern.
"""

from __future__ import annotations

import argparse
import json
import sys
from typing import Any

from shared import (
    AgentRecord,
    PipelineFacade,
    SnapshotRecord,
    agent_llm_call,
    get_pipeline_facade,
    kv_get,
    load_topics_config,
    wait_for_channel,
)


def _format_facts_for_prompt(
    fact_keys: list[str], facts: dict[str, str], titles: dict[str, str]
) -> str:
    """Render the small fact dict into a prompt-ready block."""
    blocks = []
    for key in fact_keys:
        url = key.split("/", 1)[1] if "/" in key else key
        title = titles.get(url, url)
        body = facts.get(key) or facts.get(url) or ""
        blocks.append(f"### Source: {title}\nURL: {url}\n{body}")
    return "\n\n".join(blocks)


# ---------------------------------------------------------------------------
# Public entry-point
# ---------------------------------------------------------------------------


def run_writer(
    *,
    facade: PipelineFacade,
    mode: str = "simulation",
    timeout_s: float = 60.0,
) -> AgentRecord:
    """Run the Writer end-to-end.

    Blocks on the ``research-out`` channel until a ``research.complete``
    message arrives; pulls the small structured facts from KV using the
    keys in the message payload; drafts a markdown report; persists it,
    snapshots the workspace, and emits ``draft.ready`` on ``writer-out``.
    """
    ws = facade.workspace
    record = AgentRecord(name="writer")

    msg = wait_for_channel(
        ws,
        "research-out",
        consumer="writer",
        msg_type="research.complete",
        timeout_s=timeout_s,
    )
    if msg is None:
        raise TimeoutError(
            f"writer: no research.complete message within {timeout_s}s"
        )

    payload: dict[str, Any] = msg.payload
    topic: str = payload["topic"]
    fact_keys: list[str] = list(payload["fact_keys"])

    # Pull just the fact bullets we need by key. The workspace holds the
    # full source content too, but we never read it — that's the point.
    facts: dict[str, str] = {}
    titles: dict[str, str] = {}
    for key in fact_keys:
        url = key.split("/", 1)[1] if "/" in key else key
        facts[key] = kv_get(ws, key) or ""
        meta = kv_get(ws, f"sources/meta/{url}", {}) or {}
        titles[url] = meta.get("title", url)

    facts_block = _format_facts_for_prompt(fact_keys, facts, titles)
    history: list[tuple[str, str]] = [
        (
            "user",
            f"You are the Writer in a 3-agent research pipeline. "
            f"Write a 500-1000 word markdown first-draft report on '{topic}' "
            f"using the following structured facts received from the Researcher. "
            f"Cite each source by URL.\n\n{facts_block}",
        ),
    ]
    draft = agent_llm_call(
        history,
        step="write:draft",
        purpose="draft",
        topic=topic,
        mode=mode,
        record=record,
    )

    ws.files.write("draft.md", draft)
    snap = ws.snapshot("draft-complete", message="writer first-draft committed")
    record.snapshots.append(SnapshotRecord(id=snap.id, name=snap.name, agent="writer"))

    ws.channels.send(
        "writer-out",
        payload={
            "topic": topic,
            "draft_path": "draft.md",
            "snapshot_id": snap.id,
            "word_count": len(draft.split()),
        },
        sender="writer",
        type="draft.ready",
    )

    # Ack the research-out message so it doesn't hang around in the queue.
    try:
        ws.channels.ack(msg)
    except Exception:  # noqa: BLE001
        # Some simulated buses don't require ack; tolerate it.
        pass

    return record


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Writer agent (standalone).")
    parser.add_argument("--workspace", required=True, help="Workspace name.")
    parser.add_argument(
        "--mode", default="simulation", choices=["simulation", "live"], help="LLM mode."
    )
    parser.add_argument(
        "--timeout-s",
        type=float,
        default=60.0,
        help="How long to wait for research.complete (seconds).",
    )
    parser.add_argument(
        "--record-out",
        default=None,
        help="If given, write the AgentRecord JSON to this path.",
    )
    args = parser.parse_args(argv)

    facade = get_pipeline_facade(args.workspace)
    print(
        f"[writer] workspace={args.workspace} mode={args.mode} "
        f"backend={facade.mode_label}"
    )
    _ = load_topics_config()  # warm-load to fail fast if config is broken
    rec = run_writer(facade=facade, mode=args.mode, timeout_s=args.timeout_s)

    print(
        f"writer done — LLM calls: {len(rec.llm_calls)}, "
        f"tokens: {rec.total_tokens:,}, snapshots: {len(rec.snapshots)}"
    )
    if args.record_out:
        with open(args.record_out, "w") as f:
            json.dump(rec.to_dict(), f, indent=2)
    return 0


if __name__ == "__main__":
    sys.exit(main())
