# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 The Plinth Authors
"""The Reviewer agent.

Reads:  ``writer-out`` channel (waits for a ``draft.ready`` msg)
Writes: ``final-out`` channel + workspace ``final.md`` and
        ``critique.md`` files.

The Reviewer reads only the draft that the Writer committed (via the
workspace, found through the channel handoff payload). Two LLM calls:
``critique`` then ``finalize``. Both prompts are bounded by the size
of the draft, not by the corpus size.
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
    files_read_text,
    get_pipeline_facade,
    load_topics_config,
    wait_for_channel,
)


def _extract_changes(critique: str) -> list[str]:
    """Best-effort: pull bullet/numbered items out of the critique text."""
    out: list[str] = []
    for raw in critique.splitlines():
        stripped = raw.strip()
        if not stripped:
            continue
        if stripped[:2] in ("- ", "* "):
            out.append(stripped[2:].strip())
            continue
        if len(stripped) > 2 and stripped[0].isdigit() and stripped[1] in ".)":
            out.append(stripped[2:].strip())
    return out


def run_reviewer(
    *,
    facade: PipelineFacade,
    mode: str = "simulation",
    timeout_s: float = 60.0,
) -> AgentRecord:
    """Run the Reviewer end-to-end.

    Waits for ``draft.ready`` on ``writer-out``, reads the draft from the
    workspace, runs critique and finalize LLM calls, persists the final
    report, and emits ``pipeline.done`` on ``final-out``.
    """
    ws = facade.workspace
    record = AgentRecord(name="reviewer")

    msg = wait_for_channel(
        ws,
        "writer-out",
        consumer="reviewer",
        msg_type="draft.ready",
        timeout_s=timeout_s,
    )
    if msg is None:
        raise TimeoutError(f"reviewer: no draft.ready message within {timeout_s}s")

    payload: dict[str, Any] = msg.payload
    topic: str = payload.get("topic", "")
    draft = files_read_text(ws, payload["draft_path"])

    # --- critique ---
    critique_history: list[tuple[str, str]] = [
        (
            "user",
            f"You are the Reviewer in a 3-agent research pipeline. "
            f"Critique the following draft on '{topic}'. Return 3-5 concrete, "
            f"actionable issues that should be addressed before publication.\n\n"
            f"---\n{draft}\n---",
        ),
    ]
    critique = agent_llm_call(
        critique_history,
        step="review:critique",
        purpose="critique",
        topic=topic,
        mode=mode,
        record=record,
    )

    # --- finalize ---
    finalize_history: list[tuple[str, str]] = [
        (
            "user",
            f"Revise the following draft on '{topic}' to address every "
            f"issue raised in the critique. Return the full revised report.\n\n"
            f"=== DRAFT ===\n{draft}\n\n"
            f"=== CRITIQUE ===\n{critique}",
        ),
    ]
    final = agent_llm_call(
        finalize_history,
        step="review:finalize",
        purpose="finalize",
        topic=topic,
        mode=mode,
        record=record,
    )

    ws.files.write("final.md", final)
    ws.files.write("critique.md", critique)
    snap = ws.snapshot("final-complete", message="reviewer published final report")
    record.snapshots.append(SnapshotRecord(id=snap.id, name=snap.name, agent="reviewer"))

    changes = _extract_changes(critique)
    ws.channels.send(
        "final-out",
        payload={
            "topic": topic,
            "snapshot_id": snap.id,
            "final_path": "final.md",
            "review_notes": critique,
            "changes": changes,
            "word_count": len(final.split()),
        },
        sender="reviewer",
        type="pipeline.done",
    )

    try:
        ws.channels.ack(msg)
    except Exception:  # noqa: BLE001
        pass

    return record


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Reviewer agent (standalone).")
    parser.add_argument("--workspace", required=True, help="Workspace name.")
    parser.add_argument(
        "--mode", default="simulation", choices=["simulation", "live"], help="LLM mode."
    )
    parser.add_argument(
        "--timeout-s",
        type=float,
        default=60.0,
        help="How long to wait for draft.ready (seconds).",
    )
    parser.add_argument(
        "--record-out",
        default=None,
        help="If given, write the AgentRecord JSON to this path.",
    )
    args = parser.parse_args(argv)

    facade = get_pipeline_facade(args.workspace)
    print(
        f"[reviewer] workspace={args.workspace} mode={args.mode} "
        f"backend={facade.mode_label}"
    )
    _ = load_topics_config()
    rec = run_reviewer(facade=facade, mode=args.mode, timeout_s=args.timeout_s)
    print(
        f"reviewer done — LLM calls: {len(rec.llm_calls)}, "
        f"tokens: {rec.total_tokens:,}, snapshots: {len(rec.snapshots)}"
    )
    if args.record_out:
        with open(args.record_out, "w") as f:
            json.dump(rec.to_dict(), f, indent=2)
    return 0


if __name__ == "__main__":
    sys.exit(main())
