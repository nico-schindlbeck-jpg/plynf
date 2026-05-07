# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 The Plinth Authors
"""Driver process for the durable-workflow demo.

This script:

1. Creates / fetches the ``durable-demo`` workspace.
2. Idempotently creates a ``research-pipeline`` workflow
   (manifest: ``search`` → ``fetch`` → ``extract`` → ``synth``).
3. Creates each step in ``initial_status="pending"`` so workers can
   lease them. (The legacy v0.2 default is ``running`` for in-process
   agents; the v0.5 durable executor wants pending steps.)
4. Polls the workflow until it transitions to ``completed`` /
   ``failed``, printing progress every few seconds.
5. Reads ``report.md`` from the workspace and prints it.

Run it AFTER starting at least one ``plinth-workflow-worker``:

    plinth-workflow-worker --handlers-module handlers --concurrency 2

You can kill the worker mid-flight (Ctrl+C); start another one — it
will pick up where the first left off.
"""

from __future__ import annotations

import argparse
import sys
import time

from plinth import Plinth

from shared import make_client_kwargs, services_available

WORKFLOW_NAME = "research-pipeline"
WORKFLOW_STEPS = ["search", "fetch", "extract", "synth"]


def _ensure_services() -> None:
    services = services_available()
    missing = [k for k, v in services.items() if not v]
    if missing:
        print(
            f"[start] services not reachable: {missing}. "
            "Start them with `make services` then retry.",
            file=sys.stderr,
        )
        sys.exit(2)


def _ensure_pending_steps(wf, *, topic: str) -> int:
    """Create any missing pending steps + return how many were started.

    Idempotent: re-running picks up where the previous attempt left off.
    Steps that already have a ``running`` / ``completed`` attempt are
    skipped.
    """

    wf.refresh()
    completed = {s.name for s in wf.steps if s.status == "completed"}
    in_flight = {s.name for s in wf.steps if s.status in {"running", "pending"}}
    started = 0
    for name in WORKFLOW_STEPS:
        if name in completed or name in in_flight:
            continue
        wf.start_step(
            name,
            input={"topic": topic, "k": 5},
            initial_status="pending",
        )
        started += 1
    return started


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="start_workflow")
    parser.add_argument(
        "--topic",
        default="renewable energy",
        help="Research topic to drive the workflow.",
    )
    parser.add_argument(
        "--workspace-name",
        default="durable-demo",
        help="Name of the workspace (created if missing).",
    )
    parser.add_argument(
        "--timeout",
        type=int,
        default=120,
        help="Max seconds to wait for completion.",
    )
    parser.add_argument(
        "--poll-interval",
        type=float,
        default=2.0,
    )
    args = parser.parse_args(argv)

    _ensure_services()

    client = Plinth(**make_client_kwargs())
    ws = client.workspace(args.workspace_name)
    print(f"[start] workspace: {ws.id} ({ws.name})")

    wf = ws.workflows.get_or_create(WORKFLOW_NAME, steps=WORKFLOW_STEPS)
    print(f"[start] workflow: {wf.id} (status={wf.status})")

    started = _ensure_pending_steps(wf, topic=args.topic)
    if started:
        print(f"[start] queued {started} pending steps for the worker pool")
    else:
        print("[start] all steps already in flight or done")

    deadline = time.time() + args.timeout
    last_status: str | None = None
    while True:
        wf.refresh()
        status = wf.status
        if status != last_status:
            done = sum(1 for s in wf.steps if s.status == "completed")
            running = sum(1 for s in wf.steps if s.status == "running")
            pending = sum(1 for s in wf.steps if s.status == "pending")
            print(
                f"[start] status={status} "
                f"completed={done} running={running} pending={pending}"
            )
            last_status = status

        if status in {"completed", "failed", "cancelled"}:
            break
        if time.time() > deadline:
            print(f"[start] TIMEOUT after {args.timeout}s; current status={status}")
            return 1

        time.sleep(args.poll_interval)

    print(f"[start] workflow {wf.id} {status}")
    if status == "completed":
        try:
            report = ws.files.read("report.md", as_text=True)
        except Exception as exc:  # noqa: BLE001
            print(f"[start] could not read report.md: {exc}")
            return 0
        print("---- report.md ----")
        print(report)
    return 0 if status == "completed" else 1


if __name__ == "__main__":
    sys.exit(main())
