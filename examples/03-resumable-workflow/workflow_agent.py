# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 The Plinth Authors
"""The long-running research-workflow agent with checkpoint logic.

Runs a 6-step research pipeline on a topic:

    1. discover  — search 5 sources
    2. fetch     — fetch content for each source
    3. extract   — extract facts per source (5 LLM calls)
    4. outline   — produce a structured outline
    5. write     — write a long-form report
    6. polish    — review and finalise

Each step:

* reads workspace state (KV/files) from prior completed steps
* does its work (LLM calls + mock tool invocations)
* writes results back to the workspace
* takes a snapshot
* calls ``workflow.complete_step(name, snapshot_id=snap.id)``

If invoked with ``--crash-at <step>``, the agent deliberately
``sys.exit(99)``s before completing that step's snapshot+complete pair,
leaving the workflow in a "running" state that the resume agent will
pick up. The exit code 99 is the wire signal to ``crash_resume.py``.

Subsequent invocations on the same workspace name with no ``--crash-at``
will:

* call ``ws.workflows.get_or_create("deep-research", steps=...)`` —
  idempotent, returns the existing workflow.
* call ``wf.resume_info()`` to find the next pending step.
* skip already-completed steps and start from the first pending one.
* complete the workflow.

Service detection follows the same pattern as ``examples/01-research-agent``:
if the Plinth services are reachable, the real SDK is used (real
workspace, real workflow API, real snapshots). Otherwise an in-process
file-backed simulation is used; the crash semantics still hold across
the subprocess boundary because the simulation persists JSON to disk.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from dataclasses import dataclass
from typing import Any, Protocol

from shared import (
    RunRecord,
    SimulatedWorkflowStore,
    SimulatedWorkspaceStore,
    StepRecord,
    get_fixture_sources,
    llm_call,
    services_available,
    slugify,
)


# ---------------------------------------------------------------------------
# Workflow manifest — the 6 steps
# ---------------------------------------------------------------------------

WORKFLOW_NAME = "deep-research"
WORKFLOW_STEPS = ["discover", "fetch", "extract", "outline", "write", "polish"]


# ---------------------------------------------------------------------------
# Logging — stderr is for humans, stdout is for the parent's JSON parsing.
# ---------------------------------------------------------------------------


def _log(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)


# ---------------------------------------------------------------------------
# Workspace + Workflow facade
# ---------------------------------------------------------------------------
#
# Two backends:
#
# * "sdk"        — real Plinth services + real SDK (ws.workflows etc.).
# * "simulated"  — file-backed in-process stores under SIM_DATA_DIR.
#
# Both expose the same shape so the step functions don't care which one
# is in use.


class _WorkspaceProto(Protocol):
    """Minimal workspace surface the step functions need."""

    @property
    def kv(self) -> Any: ...

    @property
    def files(self) -> Any: ...

    def snapshot(self, name: str, *, message: str | None = None) -> Any: ...


class _WorkflowProto(Protocol):
    """Minimal workflow surface the agent loop uses."""

    id: str

    @property
    def steps_manifest(self) -> list[str]: ...

    @property
    def status(self) -> str: ...

    @property
    def steps(self) -> list[Any]: ...

    def start_step(self, name: str, *, input: Any = None) -> Any: ...

    def complete_step(
        self, step_id: str, *, output: Any = None, snapshot_id: str | None = None
    ) -> Any: ...

    def fail_step(self, step_id: str, *, error: str) -> Any: ...

    def resume_info(self) -> Any: ...


@dataclass
class _Backend:
    """Bag of (workspace, workflow, kind) used by the run loop."""

    workspace: _WorkspaceProto
    workflow: _WorkflowProto
    kind: str  # "sdk" or "simulated"


def _get_backend(workspace_name: str) -> _Backend:
    """Try real SDK; fall back to simulation."""
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
            # ws.workflows.get_or_create is idempotent on (workspace, name).
            wf_handle = ws.workflows.get_or_create(
                WORKFLOW_NAME, steps=WORKFLOW_STEPS
            )
            return _Backend(
                workspace=ws,
                workflow=_SDKWorkflowAdapter(wf_handle),
                kind="sdk",
            )
        except Exception as exc:  # noqa: BLE001
            _log(
                f"[agent] SDK path failed ({exc}); falling back to simulated stores."
            )
    # Simulated path
    sim_ws = SimulatedWorkspaceStore(workspace_name)
    sim_store = SimulatedWorkflowStore(workspace_name)
    sim_wf = sim_store.get_or_create(WORKFLOW_NAME, steps=WORKFLOW_STEPS)
    return _Backend(workspace=sim_ws, workflow=sim_wf, kind="simulated")


class _SDKWorkflowAdapter:
    """Adapter mapping the SDK's WorkflowHandle to the agent's Protocol.

    The SDK returns ``WorkflowStep`` Pydantic models that we want to
    iterate over alongside the simulated ``StepInfo`` dataclasses.
    Both have ``name``, ``status``, ``snapshot_id``, ``finished_at`` —
    the adapter is purely cosmetic.
    """

    def __init__(self, handle: Any) -> None:
        self._h = handle

    @property
    def id(self) -> str:
        return self._h.id

    @property
    def steps_manifest(self) -> list[str]:
        return list(self._h.steps_manifest)

    @property
    def status(self) -> str:
        return self._h.status

    @property
    def steps(self) -> list[Any]:
        """Return the latest step log.

        The SDK exposes ``steps`` as a property in current versions but as
        a method in some pre-release versions. Try both.
        """
        # Refresh the server view first (best-effort).
        if hasattr(self._h, "refresh"):
            try:
                self._h.refresh()
            except Exception:  # noqa: BLE001
                pass
        steps_attr = getattr(self._h, "steps")
        if callable(steps_attr):
            try:
                return list(steps_attr())
            except Exception:  # noqa: BLE001
                pass
        return list(steps_attr)

    def start_step(self, name: str, *, input: Any = None) -> Any:  # noqa: A002
        return self._h.start_step(name, input=input)

    def complete_step(
        self, step_id: str, *, output: Any = None, snapshot_id: str | None = None
    ) -> Any:
        return self._h.complete_step(step_id, output=output, snapshot_id=snapshot_id)

    def fail_step(self, step_id: str, *, error: str) -> Any:
        return self._h.fail_step(step_id, error)

    def resume_info(self) -> Any:
        return self._h.resume_info()


# ---------------------------------------------------------------------------
# Step implementations
# ---------------------------------------------------------------------------


def _step_discover(ws: _WorkspaceProto, topic: str, record: RunRecord) -> dict[str, Any]:
    """Search 5 sources, write index + per-source meta to KV.

    ~50 tokens of LLM use (a single short reasoning step).
    """
    history: list[tuple[str, str]] = [
        (
            "user",
            f"Search for sources on '{topic}' using web.search.",
        ),
    ]
    llm_call(
        history,
        step="discover",
        purpose="short",
        topic=topic,
        record=record,
    )

    sources = get_fixture_sources(topic)
    urls = [src["url"] for src in sources]
    ws.kv.set("topic", topic)
    ws.kv.set("sources", urls)
    for src in sources:
        ws.kv.set(
            f"sources/meta/{src['url']}",
            {"title": src["title"], "snippet": src["snippet"], "fetched": False},
        )
    _log(f"  [discover] indexed {len(urls)} sources")
    return {"sources_count": len(urls)}


def _step_fetch(ws: _WorkspaceProto, topic: str, record: RunRecord) -> dict[str, Any]:
    """Fetch each source's content and write to files. No LLM cost."""
    urls: list[str] = ws.kv.get("sources")
    sources = {src["url"]: src for src in get_fixture_sources(topic)}
    fetched = 0
    for url in urls:
        path = f"sources/{slugify(url)}.txt"
        # ws.files for the SDK has no .exists; we just always write.
        # (idempotent under last-write-wins).
        src = sources.get(url)
        content = src["content"] if src else f"[mock] no content for {url}"
        ws.files.write(path, content)
        meta = ws.kv.get(f"sources/meta/{url}")
        meta["fetched"] = True
        ws.kv.set(f"sources/meta/{url}", meta)
        fetched += 1
    _log(f"  [fetch] fetched {fetched} sources")
    return {"fetched": fetched}


def _step_extract(ws: _WorkspaceProto, topic: str, record: RunRecord) -> dict[str, Any]:
    """Per-source LLM extraction, ~5 calls × ~1500 tokens each."""
    urls: list[str] = ws.kv.get("sources")
    extracted = 0
    for url in urls:
        meta = ws.kv.get(f"sources/meta/{url}")
        title = meta.get("title", url)
        path = f"sources/{slugify(url)}.txt"
        content = _read_file(ws, path)
        history: list[tuple[str, str]] = [
            (
                "user",
                f"Extract 3-5 key facts from the following source.\n\n"
                f"Source title: {title}\n"
                f"Source URL: {url}\n\n"
                f"---\n{content}\n---",
            ),
        ]
        response = llm_call(
            history,
            step=f"extract:{url}",
            purpose="extraction",
            topic=topic,
            record=record,
        )
        ws.kv.set(f"facts/{url}", response)
        extracted += 1
    _log(f"  [extract] extracted facts for {extracted} sources")
    return {"extracted": extracted}


def _step_outline(ws: _WorkspaceProto, topic: str, record: RunRecord) -> dict[str, Any]:
    """One LLM call producing a structured outline (~2000 tokens)."""
    urls: list[str] = ws.kv.get("sources")
    facts_by_url: dict[str, str] = {}
    for url in urls:
        try:
            facts_by_url[url] = ws.kv.get(f"facts/{url}")
        except KeyError:
            continue
    facts_summary = "\n\n".join(
        f"### Facts from {url}\n{facts}" for url, facts in facts_by_url.items()
    )
    history: list[tuple[str, str]] = [
        (
            "user",
            f"Produce a structured outline for a long-form research report on "
            f"'{topic}', drawing on the following facts.\n\n{facts_summary}",
        ),
    ]
    outline = llm_call(
        history,
        step="outline",
        purpose="outline",
        topic=topic,
        record=record,
    )
    ws.kv.set("outline", outline)
    _log(f"  [outline] wrote outline ({len(outline):,} chars)")
    return {"outline_chars": len(outline)}


def _step_write(ws: _WorkspaceProto, topic: str, record: RunRecord) -> dict[str, Any]:
    """One LLM call producing the long-form report (~5000 tokens)."""
    outline = ws.kv.get("outline")
    urls: list[str] = ws.kv.get("sources")
    facts_by_url: dict[str, str] = {}
    for url in urls:
        try:
            facts_by_url[url] = ws.kv.get(f"facts/{url}")
        except KeyError:
            continue
    facts_summary = "\n\n".join(
        f"### Facts from {url}\n{facts}" for url, facts in facts_by_url.items()
    )
    history: list[tuple[str, str]] = [
        (
            "user",
            f"Write a long-form research report on '{topic}' that follows the "
            f"outline below, drawing on the supplied facts. Cite each source by URL.\n\n"
            f"## Outline\n{outline}\n\n## Facts\n{facts_summary}",
        ),
    ]
    report = llm_call(
        history,
        step="write",
        purpose="write",
        topic=topic,
        record=record,
    )
    ws.files.write("report.md", report)
    _log(f"  [write] wrote report.md ({len(report):,} chars)")
    return {"report_chars": len(report)}


def _step_polish(ws: _WorkspaceProto, topic: str, record: RunRecord) -> dict[str, Any]:
    """One LLM call producing the polished/final report (~3000 tokens)."""
    draft = _read_file(ws, "report.md")
    history: list[tuple[str, str]] = [
        (
            "user",
            f"Polish and finalise the following research report on '{topic}'. "
            f"Tighten phrasing, soften any unsupported claims, and lead the "
            f"recommendations section with the most actionable items.\n\n{draft}",
        ),
    ]
    final = llm_call(
        history,
        step="polish",
        purpose="polish",
        topic=topic,
        record=record,
    )
    ws.files.write("final.md", final)
    _log(f"  [polish] wrote final.md ({len(final):,} chars)")
    return {"final_chars": len(final), "word_count": len(final.split())}


_STEP_FNS = {
    "discover": _step_discover,
    "fetch": _step_fetch,
    "extract": _step_extract,
    "outline": _step_outline,
    "write": _step_write,
    "polish": _step_polish,
}


def _read_file(ws: _WorkspaceProto, path: str) -> str:
    """Read a file as text, handling both SDK FilesProxy and SimulatedFiles."""
    try:
        # Simulated path supports the keyword argument and returns str.
        return ws.files.read(path, as_text=True)
    except TypeError:
        # SDK FilesProxy.read takes ``as_text=True`` too. Older signatures
        # without it return bytes; decode if so.
        result = ws.files.read(path)
        if isinstance(result, bytes):
            return result.decode("utf-8")
        return result


# ---------------------------------------------------------------------------
# Run loop
# ---------------------------------------------------------------------------


def run_agent(
    workspace_name: str,
    topic: str,
    *,
    crash_at: str | None = None,
) -> dict[str, Any]:
    """Run the workflow.

    If ``crash_at`` is set, do the work for that step but ``sys.exit(99)``
    before the snapshot + complete pair, leaving the workflow's step in
    the ``running`` state.

    On normal completion returns a dict with status and metrics. The
    agent's final stdout line is the JSON-serialised metrics dict, parsed
    by the parent ``crash_resume.py``.
    """
    backend = _get_backend(workspace_name)
    ws = backend.workspace
    wf = backend.workflow

    record = RunRecord(
        topic=topic,
        workspace_name=workspace_name,
        workflow_id=wf.id,
        crash_at=crash_at,
        backend=backend.kind,
    )

    info = wf.resume_info()
    next_step: str | None = info.next_step

    if next_step is None:
        record.completed = True
        record.finished_at = time.time()
        _log(
            f"[agent] workflow {wf.id} already complete; nothing to do "
            f"(backend={backend.kind})"
        )
        _emit_metrics(record)
        return record.to_dict()

    if info.snapshot_id:
        _log(
            f"[agent] resuming from snapshot {info.snapshot_id} at step "
            f"'{next_step}' (backend={backend.kind})"
        )
    else:
        _log(
            f"[agent] starting fresh at step '{next_step}' (backend={backend.kind})"
        )

    # Walk the manifest from the next pending step forward.
    manifest = list(wf.steps_manifest)
    start_idx = manifest.index(next_step)
    remaining = manifest[start_idx:]
    for step_name in remaining:
        # Crash trigger
        if step_name == crash_at:
            _log(f"  [crash] simulating crash before completing step '{step_name}'")
            step = wf.start_step(step_name, input={"topic": topic})
            tokens_before = record.total_tokens
            in_before = record.total_input_tokens
            out_before = record.total_output_tokens
            started = time.time()
            try:
                _STEP_FNS[step_name](ws, topic, record)
            except Exception as exc:  # noqa: BLE001
                _step_id = getattr(step, "id", None)
                if _step_id:
                    wf.fail_step(_step_id, error=str(exc))
                _log(f"[agent] step {step_name!r} FAILED: {exc}")
                raise
            # Record what we did do, but DO NOT snapshot or complete.
            record.steps.append(
                StepRecord(
                    name=step_name,
                    started_at=started,
                    finished_at=time.time(),
                    snapshot_id=None,
                    skipped=False,
                    skipped_reason=None,
                    input_tokens=record.total_input_tokens - in_before,
                    output_tokens=record.total_output_tokens - out_before,
                )
            )
            record.crashed = True
            record.finished_at = time.time()
            added = record.total_tokens - tokens_before
            _log(
                f"[agent] CRASH at step '{step_name}' "
                f"(work done but not committed; +{added:,} tokens lost)"
            )
            _emit_metrics(record)
            sys.exit(99)

        # Normal path
        step_fn = _STEP_FNS.get(step_name)
        if step_fn is None:
            raise SystemExit(f"unknown step: {step_name}")

        _log(f"[agent] starting step '{step_name}'")
        step = wf.start_step(step_name, input={"topic": topic})
        in_before = record.total_input_tokens
        out_before = record.total_output_tokens
        started = time.time()
        try:
            output = step_fn(ws, topic, record)
        except Exception as exc:  # noqa: BLE001
            _step_id = getattr(step, "id", None)
            if _step_id:
                wf.fail_step(_step_id, error=str(exc))
            _log(f"[agent] step {step_name!r} FAILED: {exc}")
            raise

        snap = ws.snapshot(
            f"after-{step_name}",
            message=f"workflow {wf.id} step {step_name}",
        )
        wf.complete_step(step.id, output=output, snapshot_id=snap.id)
        record.steps.append(
            StepRecord(
                name=step_name,
                started_at=started,
                finished_at=time.time(),
                snapshot_id=snap.id,
                input_tokens=record.total_input_tokens - in_before,
                output_tokens=record.total_output_tokens - out_before,
            )
        )
        added = record.steps[-1].input_tokens + record.steps[-1].output_tokens
        _log(
            f"[agent] step '{step_name}' complete "
            f"(snap={snap.id}, +{added:,} tokens)"
        )

    record.completed = True
    record.finished_at = time.time()
    _log(
        f"[agent] workflow {wf.id} complete: "
        f"{len(record.steps)} steps, {record.total_tokens:,} tokens"
    )
    _emit_metrics(record)
    return record.to_dict()


def _emit_metrics(record: RunRecord) -> None:
    """Write a single JSON line to stdout for the parent process."""
    sys.stdout.write(json.dumps(record.to_dict()) + "\n")
    sys.stdout.flush()


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Plinth resumable-workflow agent.")
    parser.add_argument(
        "--workspace",
        required=True,
        help="Workspace name (idempotent get-or-create).",
    )
    parser.add_argument(
        "--topic",
        required=True,
        help="Research topic.",
    )
    parser.add_argument(
        "--crash-at",
        default=None,
        help="If set, simulate a crash before completing that step.",
    )
    args = parser.parse_args(argv)

    if args.crash_at and args.crash_at not in WORKFLOW_STEPS:
        raise SystemExit(
            f"--crash-at must be one of {WORKFLOW_STEPS}; got {args.crash_at!r}"
        )

    services = services_available()
    reachable = [k for k, v in services.items() if v]
    missing = [k for k, v in services.items() if not v]
    if missing:
        _log(
            f"[agent] services not reachable: {missing}; using simulated stores. "
            f"(Reachable: {reachable})"
        )
    else:
        _log("[agent] all Plinth services reachable")

    run_agent(args.workspace, args.topic, crash_at=args.crash_at)
    return 0


if __name__ == "__main__":
    sys.exit(main())
