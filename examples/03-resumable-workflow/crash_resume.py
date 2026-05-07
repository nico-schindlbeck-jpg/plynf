# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 The Plinth Authors
"""The resumable-workflow demo entry point.

Spawns ``workflow_agent.py`` as a subprocess **twice** on the same
workspace name:

* **Run 1** crashes at the ``outline`` step (``--crash-at outline``).
  The subprocess exits with code 99. The workflow on disk is left in
  ``running`` state with ``discover``, ``fetch``, ``extract`` completed
  and ``outline`` started but not snapshotted.

* **Run 2** is launched on the same workspace name with no
  ``--crash-at``. The agent reads the workflow's ``resume_info()``,
  finds the next pending step is ``outline``, and proceeds to
  completion. Discover / fetch / extract are not redone.

Between the runs the demo prints the workflow state so the user can
see what was preserved across the crash boundary.

Finally a summary table reports the tokens used in each run, the
tokens saved by resume, and a brief reliability story.

Usage:

    python crash_resume.py --topic "renewable energy"
    python crash_resume.py --inspect <workspace-name>
"""

from __future__ import annotations

import argparse
import datetime as _dt
import json
import os
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from rich.console import Console

from shared import (
    SimulatedWorkflowStore,
    SimulatedWorkspaceStore,
    estimate_cost,
    services_available,
    short_ulid,
    slugify,
)


HERE = Path(__file__).resolve().parent
AGENT_PATH = HERE / "workflow_agent.py"
REPORTS_DIR = HERE / "reports"

console = Console()


# ---------------------------------------------------------------------------
# Subprocess driver
# ---------------------------------------------------------------------------


@dataclass
class RunResult:
    """Result of one ``workflow_agent.py`` subprocess invocation."""

    run_label: str
    return_code: int
    metrics: dict[str, Any]
    wall_clock_seconds: float
    stderr_lines: list[str]

    @property
    def crashed(self) -> bool:
        return bool(self.metrics.get("crashed"))

    @property
    def total_tokens(self) -> int:
        return int(self.metrics.get("total_tokens", 0))

    @property
    def total_cost_usd(self) -> float:
        return float(self.metrics.get("total_cost_usd", 0.0))

    @property
    def workflow_id(self) -> str:
        return str(self.metrics.get("workflow_id", ""))

    @property
    def steps_run(self) -> list[dict[str, Any]]:
        return list(self.metrics.get("steps") or [])


def _run_subprocess(
    *, run_label: str, args: list[str], echo_stderr: bool = True
) -> RunResult:
    """Invoke ``workflow_agent.py`` and parse its final stdout JSON line."""
    full_args = [sys.executable, str(AGENT_PATH), *args]
    start = time.perf_counter()
    proc = subprocess.run(
        full_args,
        env={**os.environ},
        capture_output=True,
        text=True,
        check=False,
    )
    wall = time.perf_counter() - start

    stderr_lines: list[str] = []
    if proc.stderr:
        for line in proc.stderr.rstrip("\n").splitlines():
            stderr_lines.append(line)
            if echo_stderr:
                console.print(f"  [dim]{line}[/dim]")

    metrics: dict[str, Any] = {}
    for line in reversed(proc.stdout.strip().splitlines()):
        if not line.strip():
            continue
        try:
            metrics = json.loads(line)
            break
        except json.JSONDecodeError:
            continue
    if not metrics:
        console.print(
            f"[red]No metrics parsed from agent stdout (return code {proc.returncode})[/red]"
        )
    return RunResult(
        run_label=run_label,
        return_code=proc.returncode,
        metrics=metrics,
        wall_clock_seconds=wall,
        stderr_lines=stderr_lines,
    )


# ---------------------------------------------------------------------------
# Inspection — read workflow state from disk and pretty-print it.
# ---------------------------------------------------------------------------


_STATUS_GLYPHS = {
    "completed": "✓",
    "running":   "◐",
    "failed":    "✗",
    "pending":   "·",
    "cancelled": "·",
}


@dataclass
class _StepView:
    name: str
    status: str
    finished_at: float | None
    snapshot_id: str | None


def _format_time(epoch: float | None) -> str:
    if not epoch:
        return ""
    return _dt.datetime.fromtimestamp(epoch).strftime("%H:%M:%S")


def _inspect_via_sdk(workspace_name: str) -> bool:
    """Inspect workflow state via the real Plinth SDK if services are up.

    Returns True if a workflow was found and printed, False to fall back.
    """
    services = services_available()
    if not services["workspace"]:
        return False
    try:
        from plinth import Plinth  # type: ignore[attr-defined]

        client = Plinth(
            workspace_url=os.environ.get(
                "PLINTH_WORKSPACE_URL", "http://localhost:7421"
            ),
            gateway_url=os.environ.get(
                "PLINTH_GATEWAY_URL", "http://localhost:7422"
            ),
            api_key="local-dev",
        )
        ws = client.workspace(workspace_name)
        rows = list(ws.workflows.list())
        if not rows:
            return False
        wf_summary = rows[-1]
        # ``list`` returns bare ``Workflow`` rows; call ``get`` to fetch
        # the full handle with its step log.
        wf = ws.workflows.get(wf_summary.id)
    except Exception as exc:  # noqa: BLE001
        console.print(
            f"[yellow]  SDK inspection failed ({exc}); trying simulated store...[/yellow]"
        )
        return False

    # Re-fetch the latest server view. ``steps`` is a property on the
    # SDK handle (returns the cached :class:`WorkflowStep` list).
    if hasattr(wf, "refresh"):
        try:
            wf.refresh()
        except Exception:  # noqa: BLE001
            pass
    steps_attr = wf.steps
    steps = list(steps_attr() if callable(steps_attr) else steps_attr)
    completed_names = {s.name for s in steps if s.status == "completed"}
    steps_by_name: dict[str, _StepView] = {}
    for s in steps:
        finished_epoch = (
            s.finished_at.timestamp() if s.finished_at is not None else None
        )
        steps_by_name[s.name] = _StepView(
            name=s.name,
            status=s.status,
            finished_at=finished_epoch,
            snapshot_id=s.snapshot_id,
        )
    manifest = list(wf.steps_manifest)
    for name in manifest:
        if name not in steps_by_name:
            steps_by_name[name] = _StepView(
                name=name, status="pending", finished_at=None, snapshot_id=None
            )
    workflow_status = wf.status
    if all(name in completed_names for name in manifest):
        workflow_status = "completed"

    _render_inspection(
        wf_name=wf.name,
        wf_id=wf.id,
        manifest=manifest,
        steps_by_name=steps_by_name,
        workflow_status=workflow_status,
    )
    return True


def inspect_workflow(workspace_name: str) -> None:
    """Pretty-print the workflow state for ``workspace_name``.

    Tries the real SDK first; falls back to the simulated store. This
    way the demo prints a coherent view in both modes.
    """
    if _inspect_via_sdk(workspace_name):
        return

    store = SimulatedWorkflowStore(workspace_name)
    workflows = store.list()
    if not workflows:
        console.print(
            f"[yellow]  No workflows found for workspace {workspace_name!r}.[/yellow]"
        )
        return

    wf = workflows[-1]
    completed_names = {s.name for s in wf.steps if s.status == "completed"}
    steps_by_name: dict[str, _StepView] = {}
    for s in wf.steps:
        steps_by_name[s.name] = _StepView(
            name=s.name,
            status=s.status,
            finished_at=s.finished_at,
            snapshot_id=s.snapshot_id,
        )
    for name in wf.steps_manifest:
        if name not in steps_by_name:
            steps_by_name[name] = _StepView(
                name=name, status="pending", finished_at=None, snapshot_id=None
            )

    workflow_status = wf.status
    if all(name in completed_names for name in wf.steps_manifest):
        workflow_status = "completed"

    _render_inspection(
        wf_name=wf.name,
        wf_id=wf.id,
        manifest=wf.steps_manifest,
        steps_by_name=steps_by_name,
        workflow_status=workflow_status,
    )


def _render_inspection(
    *,
    wf_name: str,
    wf_id: str,
    manifest: list[str],
    steps_by_name: dict[str, _StepView],
    workflow_status: str,
) -> None:
    """Shared renderer for both SDK and simulated inspection."""
    console.print(f"  Workflow: {wf_name} ({wf_id})")
    console.print(f"  Status:   [bold]{workflow_status}[/bold]")
    console.print("  Steps:")
    for name in manifest:
        view = steps_by_name[name]
        glyph = _STATUS_GLYPHS.get(view.status, "?")
        snap_text = f"snapshot: {view.snapshot_id}" if view.snapshot_id else "no snapshot"
        when = _format_time(view.finished_at)
        if view.status == "completed":
            console.print(
                f"    [green]{glyph}[/green] {name:<10}"
                f" completed at {when}  ({snap_text})"
            )
        elif view.status == "running":
            console.print(
                f"    [yellow]{glyph}[/yellow] {name:<10}"
                f" running    (no snapshot — incomplete)"
            )
        elif view.status == "failed":
            console.print(
                f"    [red]{glyph}[/red] {name:<10} failed     "
                f"(no snapshot — incomplete)"
            )
        else:
            console.print(f"    [dim]{glyph}[/dim] {name:<10} pending")


# ---------------------------------------------------------------------------
# Final summary
# ---------------------------------------------------------------------------


def _committed_step_tokens(run: RunResult) -> int:
    """Sum the tokens of steps in ``run`` that completed with a snapshot.

    These are the steps whose work was *durably preserved* across a
    process crash — exactly the work the resume agent did not have to
    redo. Steps the run started but didn't snapshot (i.e. the crashed
    step) are excluded.
    """
    total = 0
    for step in run.steps_run:
        if step.get("snapshot_id"):
            total += int(step.get("input_tokens", 0)) + int(
                step.get("output_tokens", 0)
            )
    return total


def print_final_summary(
    *, topic: str, workspace_name: str, run1: RunResult, run2: RunResult
) -> None:
    """Headline summary block.

    "Saved by resume" = the tokens of Run 1's *committed* steps (each
    has a snapshot id). These are the steps the resume agent did not
    have to redo. Without Plinth's snapshot+workflow primitives, the
    resuming agent would replay these from scratch.

    The percentage is the saved tokens as a share of the total work the
    pipeline takes — i.e. Run 1's committed work + Run 2's full work.
    That's what the agent would otherwise have to redo from scratch.
    """
    saved_tokens = _committed_step_tokens(run1)
    # Total work the pipeline costs (committed-from-Run1 + full Run 2).
    total_pipeline_tokens = saved_tokens + run2.total_tokens

    # Saved cost: Sonnet pricing on the committed input + output split.
    saved_in = sum(
        int(s.get("input_tokens", 0))
        for s in run1.steps_run
        if s.get("snapshot_id")
    )
    saved_out = sum(
        int(s.get("output_tokens", 0))
        for s in run1.steps_run
        if s.get("snapshot_id")
    )
    saved_cost = estimate_cost(saved_in, saved_out)

    if total_pipeline_tokens > 0:
        savings_pct = saved_tokens / total_pipeline_tokens * 100
    else:
        savings_pct = 0.0

    word_count = _final_word_count(workspace_name)

    console.print()
    console.print("═══════════════════════════════════════════════════════════════════")
    console.print("[bold cyan]  RESUME COMPLETE[/bold cyan]")
    console.print("═══════════════════════════════════════════════════════════════════")
    console.print(f"  Topic: {topic}")
    console.print(f"  Workspace: {workspace_name}")
    console.print(f"  Workflow:  {run1.workflow_id or run2.workflow_id}")
    console.print()
    crash_at = run1.metrics.get("crash_at") or "?"
    label_run1 = f"Run 1 (crashed at {crash_at!r}):"
    label_run2 = "Run 2 (resumed):"
    label_saved = "Saved by resume:"
    width = max(len(label_run1), len(label_run2), len(label_saved))
    console.print(
        f"  {label_run1:<{width}}  {run1.total_tokens:>6,} tokens   "
        f"|   ${run1.total_cost_usd:.4f}"
    )
    console.print(
        f"  {label_run2:<{width}}  {run2.total_tokens:>6,} tokens   "
        f"|   ${run2.total_cost_usd:.4f}"
    )
    console.print("  " + "─" * (width + 31))
    console.print(
        f"  {label_saved:<{width}}  {saved_tokens:>6,} tokens   "
        f"|   ${saved_cost:.4f}"
    )
    console.print()
    if word_count:
        console.print(f"  Final report: {word_count:,} words")
    console.print("═══════════════════════════════════════════════════════════════════")
    console.print(
        "  Without Plinth's resume: every crash means starting over from scratch."
    )
    console.print(
        f"  With Plinth: {savings_pct:.0f}% of work avoided on resume."
    )
    console.print("═══════════════════════════════════════════════════════════════════")


def _final_word_count(workspace_name: str) -> int:
    """Read the final.md from the workspace and count words.

    Tries the real workspace service first; falls back to the simulated
    store on disk. Returns 0 if neither has the file.
    """
    services = services_available()
    if services["workspace"]:
        try:
            from plinth import Plinth  # type: ignore[attr-defined]

            client = Plinth(
                workspace_url=os.environ.get(
                    "PLINTH_WORKSPACE_URL", "http://localhost:7421"
                ),
                gateway_url=os.environ.get(
                    "PLINTH_GATEWAY_URL", "http://localhost:7422"
                ),
                api_key="local-dev",
            )
            ws = client.workspace(workspace_name)
            text = ws.files.read("final.md", as_text=True)
            return len(text.split())
        except Exception:  # noqa: BLE001
            pass
    try:
        sim = SimulatedWorkspaceStore(workspace_name)
        return len(sim.files.read("final.md", default="").split())
    except Exception:  # noqa: BLE001
        return 0


# ---------------------------------------------------------------------------
# JSON report writer
# ---------------------------------------------------------------------------


def _save_json_report(
    *, topic: str, workspace_name: str, run1: RunResult, run2: RunResult
) -> Path:
    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    timestamp = _dt.datetime.now().strftime("%Y-%m-%dT%H-%M-%S")
    path = REPORTS_DIR / f"{timestamp}-resumable-{slugify(topic)}.json"
    payload = {
        "topic": topic,
        "workspace_name": workspace_name,
        "run_1_crash": {
            "metrics": run1.metrics,
            "return_code": run1.return_code,
            "wall_clock_seconds": run1.wall_clock_seconds,
        },
        "run_2_resume": {
            "metrics": run2.metrics,
            "return_code": run2.return_code,
            "wall_clock_seconds": run2.wall_clock_seconds,
        },
        "summary": {
            "tokens_run_1": run1.total_tokens,
            "tokens_run_2": run2.total_tokens,
            "tokens_total": run1.total_tokens + run2.total_tokens,
            "tokens_saved_by_resume": run1.total_tokens,
            "cost_run_1_usd": run1.total_cost_usd,
            "cost_run_2_usd": run2.total_cost_usd,
        },
    }
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    return path


# ---------------------------------------------------------------------------
# Driver
# ---------------------------------------------------------------------------


def _make_workspace_name(topic: str) -> str:
    return f"resume-demo-{slugify(topic)}-{short_ulid()[:10].lower()}"


def _print_phase_header(title: str) -> None:
    console.print()
    console.print("[bold cyan]" + ("═" * 67) + "[/bold cyan]")
    console.print(f"[bold cyan]  {title}[/bold cyan]")
    console.print("[bold cyan]" + ("═" * 67) + "[/bold cyan]")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Plinth resumable-workflow demo: crash mid-flight, resume from snapshot."
    )
    parser.add_argument(
        "--topic",
        default="renewable energy",
        help="Research topic.",
    )
    parser.add_argument(
        "--crash-at",
        default="outline",
        help="Step to crash at in run 1 (default: outline).",
    )
    parser.add_argument(
        "--inspect",
        default=None,
        metavar="WORKSPACE_NAME",
        help="Inspect a workspace's workflow state (no run).",
    )
    args = parser.parse_args(argv)

    if args.inspect:
        console.print(f"  Inspecting workspace [bold]{args.inspect}[/bold]:")
        inspect_workflow(args.inspect)
        return 0

    workspace_name = _make_workspace_name(args.topic)

    console.print()
    console.print("═══════════════════════════════════════════════════════════════════")
    console.print(
        f"[bold]  RESUMABLE WORKFLOW DEMO — topic: {args.topic!r}[/bold]"
    )
    console.print("═══════════════════════════════════════════════════════════════════")
    console.print(f"  Workspace: {workspace_name}")
    services = services_available()
    if all(services.values()):
        console.print(
            "[green]  All Plinth services reachable; using SDK against real services.[/green]"
        )
    else:
        missing = [k for k, v in services.items() if not v]
        console.print(
            f"[yellow]  Services not reachable: {missing}. "
            f"Falling back to file-backed simulation; the crash + resume "
            f"mechanic still demonstrates the value prop.[/yellow]"
        )

    # ---- Run 1: crash at the chosen step ---------------------------------
    _print_phase_header(f"▶ Run 1 — will crash at step '{args.crash_at}'")
    run1 = _run_subprocess(
        run_label="run-1-crash",
        args=[
            "--workspace",
            workspace_name,
            "--topic",
            args.topic,
            "--crash-at",
            args.crash_at,
        ],
    )
    console.print(f"  → exit code {run1.return_code}")

    if run1.return_code != 99 or not run1.crashed:
        console.print(
            f"[red]Expected crash with exit code 99 (crashed=True), "
            f"got rc={run1.return_code}, crashed={run1.crashed}.[/red]"
        )
        return 1

    # ---- Inspection between runs -----------------------------------------
    console.print()
    console.print("[bold]  Workflow state after crash:[/bold]")
    inspect_workflow(workspace_name)

    # ---- Run 2: resume ---------------------------------------------------
    _print_phase_header("▶ Run 2 — resume from snapshot")
    run2 = _run_subprocess(
        run_label="run-2-resume",
        args=[
            "--workspace",
            workspace_name,
            "--topic",
            args.topic,
        ],
    )
    console.print(f"  → exit code {run2.return_code}")

    if run2.return_code != 0:
        console.print(
            f"[red]Resume run failed with exit code {run2.return_code}.[/red]"
        )
        return 1

    # ---- Final inspection ------------------------------------------------
    console.print()
    console.print("[bold]  Final workflow state:[/bold]")
    inspect_workflow(workspace_name)

    # ---- Summary ---------------------------------------------------------
    print_final_summary(
        topic=args.topic, workspace_name=workspace_name, run1=run1, run2=run2
    )

    # ---- JSON report -----------------------------------------------------
    path = _save_json_report(
        topic=args.topic, workspace_name=workspace_name, run1=run1, run2=run2
    )
    console.print(f"[dim]  Report saved: {path.relative_to(HERE)}[/dim]")
    return 0


if __name__ == "__main__":
    sys.exit(main())
