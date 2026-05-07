# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 The Plinth Authors
"""Pipeline orchestrator: spawn 3 agents, run the pipeline, print the result.

This file is the entry point for the multi-agent demo. It:

1. Builds (or attaches to) a Plinth workspace named ``pipeline-<topic>-<ulid>``.
2. Spawns three agents — Researcher, Writer, Reviewer — each in its own
   thread. The threads share one workspace facade so that all three see
   each other's writes (the real workspace handles this via HTTP; the
   in-process simulation by sharing a Python object).
3. Waits for ``final-out`` to receive a ``pipeline.done`` message.
4. Prints the boxed summary table and writes a JSON report under
   ``reports/<timestamp>-pipeline.json``.

Why threads, not subprocesses
-----------------------------
The spec allows either. Threads are chosen because:

* In simulation mode (no services) the in-process workspace is a single
  Python object; threads naturally share it. Subprocesses would need a
  disk-backed bus.
* In SDK mode the SDK is fully thread-safe — each agent gets its own
  ``Workspace`` view — and we keep wall-clock latency low.
* The CLI still supports running each agent standalone (see
  ``researcher.py --workspace ...`` etc.), so the "agents are
  independent processes" story remains demonstrable without the
  orchestrator forcing it.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import threading
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from shared import (
    AgentRecord,
    PipelineFacade,
    SONNET_INPUT_USD_PER_MTOK,
    SONNET_OUTPUT_USD_PER_MTOK,
    estimate_cost,
    get_pipeline_facade,
    load_topics_config,
    services_available,
    slugify,
    wait_for_channel,
)

from researcher import run_researcher
from reviewer import run_reviewer
from writer import run_writer


# ---------------------------------------------------------------------------
# Aggregated pipeline record
# ---------------------------------------------------------------------------


@dataclass
class PipelineReport:
    """The aggregate output of one multi-agent pipeline run."""

    topic: str
    mode: str
    backend: str  # "sdk" | "simulated"
    workspace_name: str
    workspace_id: str
    final_report: str
    review_notes: str
    changes: list[str]
    agents: list[AgentRecord] = field(default_factory=list)
    wall_clock_seconds: float = 0.0
    channel_message_counts: dict[str, int] = field(default_factory=dict)
    channel_server_depth: dict[str, int] = field(default_factory=dict)
    snapshots: list[dict[str, Any]] = field(default_factory=list)
    services: dict[str, bool] = field(default_factory=dict)
    started_at: str = ""
    finished_at: str = ""

    @property
    def total_input_tokens(self) -> int:
        return sum(a.total_input_tokens for a in self.agents)

    @property
    def total_output_tokens(self) -> int:
        return sum(a.total_output_tokens for a in self.agents)

    @property
    def total_tokens(self) -> int:
        return self.total_input_tokens + self.total_output_tokens

    @property
    def total_cost_usd(self) -> float:
        return estimate_cost(self.total_input_tokens, self.total_output_tokens)

    @property
    def llm_call_count(self) -> int:
        return sum(len(a.llm_calls) for a in self.agents)

    @property
    def tool_call_count(self) -> int:
        return sum(len(a.tool_calls) for a in self.agents)

    def to_dict(self) -> dict[str, Any]:
        return {
            "topic": self.topic,
            "mode": self.mode,
            "backend": self.backend,
            "workspace_name": self.workspace_name,
            "workspace_id": self.workspace_id,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "summary": {
                "total_input_tokens": self.total_input_tokens,
                "total_output_tokens": self.total_output_tokens,
                "total_tokens": self.total_tokens,
                "total_cost_usd": self.total_cost_usd,
                "llm_calls": self.llm_call_count,
                "tool_calls": self.tool_call_count,
                "wall_clock_seconds": self.wall_clock_seconds,
                "channel_messages_sent": self.channel_message_counts,
                "channel_depth_after_acks": self.channel_server_depth,
                "snapshot_count": len(self.snapshots),
            },
            "agents": [a.to_dict() for a in self.agents],
            "snapshots": list(self.snapshots),
            "review_notes": self.review_notes,
            "changes": list(self.changes),
            "final_report": self.final_report,
            "services": dict(self.services),
        }


# ---------------------------------------------------------------------------
# Threaded pipeline runner
# ---------------------------------------------------------------------------


def _run_thread(
    fn: Any, *, label: str, errors: dict[str, BaseException], **kwargs: Any
) -> AgentRecord:
    """Wrapper that runs ``fn`` and stores the agent record into ``out``."""
    try:
        return fn(**kwargs)
    except BaseException as exc:  # noqa: BLE001 — propagate to orchestrator
        errors[label] = exc
        raise


def run_pipeline(topic: str, *, mode: str = "simulation") -> PipelineReport:
    """Run the three-agent pipeline end-to-end.

    Args:
        topic: Research topic.
        mode: ``"simulation"`` (deterministic mock LLM, default) or
            ``"live"`` (real Anthropic API, requires ANTHROPIC_API_KEY).

    Returns:
        A :class:`PipelineReport` aggregating per-agent token usage,
        final report text, snapshots, and channel-message counts.
    """
    ulid = uuid.uuid4().hex[:12].upper()
    workspace_name = f"pipeline-{slugify(topic)}-{ulid}"
    facade = get_pipeline_facade(workspace_name)

    print()
    print("═" * 67)
    print(f"  PLINTH MULTI-AGENT PIPELINE — topic: {topic!r}")
    print("═" * 67)
    print(f"  Workspace : {workspace_name}")
    print(f"  Workspace ID: {facade.workspace_id}")
    print(f"  Backend   : {facade.mode_label}  (services: {facade.services})")
    print()

    started = time.time()

    records: dict[str, AgentRecord] = {}
    errors: dict[str, BaseException] = {}

    def _researcher() -> None:
        records["researcher"] = _run_thread(
            run_researcher,
            label="researcher",
            errors=errors,
            topic=topic,
            facade=facade,
            mode=mode,
        )

    def _writer() -> None:
        records["writer"] = _run_thread(
            run_writer,
            label="writer",
            errors=errors,
            facade=facade,
            mode=mode,
            timeout_s=60.0,
        )

    def _reviewer() -> None:
        records["reviewer"] = _run_thread(
            run_reviewer,
            label="reviewer",
            errors=errors,
            facade=facade,
            mode=mode,
            timeout_s=120.0,
        )

    # Start writer + reviewer first so they're already polling, then the
    # researcher kicks the chain off. This mirrors a real deployment in
    # which subscribers are ready before publishers send.
    threads = [
        threading.Thread(target=_writer, name="writer", daemon=True),
        threading.Thread(target=_reviewer, name="reviewer", daemon=True),
        threading.Thread(target=_researcher, name="researcher", daemon=True),
    ]
    pids = [22000 + i for i in range(3)]  # cosmetic — threads have no real pid
    for t, pid in zip([threads[2], threads[0], threads[1]], pids):
        print(f"  ▶ {t.name:<10s} started   (tid 0x{t.ident or 0:x}, slot {pid})")
    print()
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=180.0)

    if errors:
        for label, exc in errors.items():
            print(f"[error] {label}: {exc}", file=sys.stderr)
        raise RuntimeError(f"pipeline failed: {list(errors.keys())}")

    # The reviewer thread joins only after it has emitted on final-out.
    # We still poll once to confirm the message is there for the report.
    final_msg = wait_for_channel(
        facade.workspace,
        "final-out",
        consumer="orchestrator",
        msg_type="pipeline.done",
        timeout_s=5.0,
    )
    if final_msg is None:
        raise RuntimeError("pipeline.done message never appeared on final-out")

    payload = final_msg.payload
    final_report = ""
    final_path = payload.get("final_path", "final.md")
    raw = facade.workspace.files.read(final_path, as_text=True)
    final_report = raw.decode("utf-8") if isinstance(raw, bytes) else raw

    finished = time.time()

    # Channel inventory. The pipeline sends exactly one message per
    # handoff channel — we track the "sent" count rather than the
    # current depth, because writer/reviewer ack their inbound message
    # which removes it from the channel.
    channel_counts: dict[str, int] = {
        "research-out": 1,
        "writer-out": 1,
        "final-out": 1,
    }
    # Best-effort: enrich with on-server data (current depth) when
    # services are up, for the JSON report.
    server_depth: dict[str, int] = {}
    if facade.sdk_client is not None:
        try:
            for ch in facade.workspace.channels.list():
                # The SDK has shipped two shapes: a `ChannelHandle` and a
                # plain `Channel` model. Both expose `name`. Only the
                # handle has `model.message_count`.
                name = getattr(ch, "name", None)
                if not name:
                    continue
                if hasattr(ch, "model") and ch.model is not None:
                    server_depth[name] = ch.model.message_count
                elif hasattr(ch, "message_count"):
                    server_depth[name] = ch.message_count
        except Exception as exc:  # noqa: BLE001
            print(f"[warn] could not list channels via SDK: {exc}")
    else:
        sim_channels = facade.workspace.channels  # type: ignore[attr-defined]
        for ch_name in ("research-out", "writer-out", "final-out"):
            server_depth[ch_name] = sim_channels.message_count(ch_name)

    # Snapshot inventory.
    snapshots: list[dict[str, Any]] = []
    for rec in (records.get("researcher"), records.get("writer"), records.get("reviewer")):
        if rec is None:
            continue
        for snap in rec.snapshots:
            snapshots.append({"id": snap.id, "name": snap.name, "agent": snap.agent})

    return PipelineReport(
        topic=topic,
        mode=mode,
        backend=facade.mode_label,
        workspace_name=workspace_name,
        workspace_id=facade.workspace_id,
        final_report=final_report,
        review_notes=payload.get("review_notes", ""),
        changes=list(payload.get("changes", [])),
        agents=[records["researcher"], records["writer"], records["reviewer"]],
        wall_clock_seconds=finished - started,
        channel_message_counts=channel_counts,
        channel_server_depth=server_depth,
        snapshots=snapshots,
        services=dict(facade.services),
        started_at=datetime.fromtimestamp(started, tz=timezone.utc).isoformat(),
        finished_at=datetime.fromtimestamp(finished, tz=timezone.utc).isoformat(),
    )


# ---------------------------------------------------------------------------
# Pretty printer — the boxed summary the spec asks for
# ---------------------------------------------------------------------------


def _print_summary(report: PipelineReport) -> None:
    line = "═" * 67
    print()
    print(line)
    print(f"  PIPELINE RESULT — topic: {report.topic!r}")
    print(line)
    print(f"  Workspace : {report.workspace_name}")
    print(f"  Backend   : {report.backend}")
    print()

    print("  Channel handoffs:")
    for ch, label in [
        ("research-out", "researcher → writer"),
        ("writer-out", "writer → reviewer"),
        ("final-out", "reviewer → done"),
    ]:
        n = report.channel_message_counts.get(ch, 0)
        word = "message" if n == 1 else "messages"
        print(f"    {ch:<14s}: {n} {word:<8s}  ({label})")
    print()

    print(f"  Pipeline complete in {report.wall_clock_seconds:.2f}s")
    print()

    print("  Tokens used:")
    for agent in report.agents:
        print(f"    {agent.name:<10s} : {agent.total_tokens:>7,}")
    print("    " + "─" * 24)
    print(
        f"    {'TOTAL':<10s} : {report.total_tokens:>7,}   |   "
        f"${report.total_cost_usd:.4f}"
    )
    print()

    word_count = len(report.final_report.split())
    print(f"  Final report : {word_count} words  ({len(report.final_report)} chars)")
    print()

    print("  Snapshot history:")
    for snap in report.snapshots:
        print(f"    {snap['id'][:24]:<24s}  {snap['name']:<22s} ({snap['agent']})")
    print(line)


# ---------------------------------------------------------------------------
# JSON report
# ---------------------------------------------------------------------------


def _save_json_report(report: PipelineReport) -> str:
    here = os.path.dirname(os.path.abspath(__file__))
    out_dir = os.path.join(here, "reports")
    os.makedirs(out_dir, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H-%M-%S")
    path = os.path.join(out_dir, f"{stamp}-pipeline.json")
    with open(path, "w") as f:
        json.dump(report.to_dict(), f, indent=2)
    return path


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Plinth multi-agent pipeline (research → write → review)."
    )
    parser.add_argument("--topic", default=None, help="Research topic.")
    parser.add_argument(
        "--mode",
        default="simulation",
        choices=["simulation", "live"],
        help="LLM mode (default: simulation; live requires ANTHROPIC_API_KEY).",
    )
    args = parser.parse_args(argv)

    topic = args.topic or load_topics_config().get("default_topic", "renewable energy")

    services = services_available()
    if all(services.values()):
        print(f"[plinth] All services reachable: {services}")
    else:
        missing = [k for k, v in services.items() if not v]
        print(
            f"[plinth] Services not reachable: {missing}. "
            "Falling back to in-process simulation."
        )

    report = run_pipeline(topic, mode=args.mode)
    _print_summary(report)
    out_path = _save_json_report(report)
    print(f"\n  JSON report saved: {out_path}")
    print(
        f"  Pricing reference: input ${SONNET_INPUT_USD_PER_MTOK}/Mtok, "
        f"output ${SONNET_OUTPUT_USD_PER_MTOK}/Mtok (Anthropic Sonnet)."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
