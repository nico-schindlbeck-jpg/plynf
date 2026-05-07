# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 The Plinth Authors
"""The durable-executor worker process.

Top-level loop:

1. Register with the workspace's ``/v1/workers`` endpoint to obtain a
   ``worker_id``.
2. Spawn a worker-level heartbeat task (so the workspace's reaper
   doesn't sweep us to ``gone``).
3. Spawn ``concurrency`` parallel slot tasks. Each slot:
   a. Discovers candidate workspaces + workflows (set of those visible
      to the API key).
   b. Polls ``/v1/workspaces/{ws}/workflows/{wf}/pending`` for steps
      whose ``(workflow.name, step.name)`` matches a registered handler.
   c. Tries to lease the next pending step. On 409 ``LEASE_CONFLICT``,
      pops to the next candidate.
   d. Runs the handler. While running, a per-lease heartbeat task
      bumps ``expires_at`` so the reaper doesn't reclaim the step
      mid-flight.
   e. Releases the lease with the appropriate step status.
4. On graceful shutdown (SIGTERM / SIGINT): set the stop event, wait
   for in-flight slots to drain, drain the worker (``status=draining``).

The worker only executes steps whose ``(workflow_name, step_name)`` is
in the dispatcher's registry. If a workspace contains workflow steps
this worker doesn't recognise, it leaves them alone — another worker
with the right handlers can take them.
"""

from __future__ import annotations

import asyncio
import contextlib
import signal
from collections.abc import Iterable
from datetime import datetime, timezone
from typing import Any

from plinth import Plinth
from plinth.exceptions import LeaseConflict, PlinthError, WorkflowNotFound
from plinth.models import Worker as WorkerModel
from plinth.models import WorkflowStep
from plinth.workflow_runtime import HandlerContext, WorkflowRuntime
from plinth.workflows import WorkflowHandle

from .logging_config import get_logger

UTC = timezone.utc


class Worker:
    """Durable workflow worker.

    Args:
        client: A configured :class:`plinth.Plinth` client. The
            ``client._workflow_runtime`` is read for the dispatch table.
        runtime: Optional override for the runtime; defaults to the
            client's. Useful in tests.
        concurrency: Number of in-flight steps the worker holds at once.
        lease_ttl: TTL passed when acquiring a lease (seconds).
        heartbeat_interval: Seconds between per-lease heartbeats.
        worker_heartbeat_interval: Seconds between worker-level heartbeats.
        poll_interval: Seconds to sleep between empty polls.
        workspace_filter: Optional whitelist of workspace IDs/names. When
            empty (the default), the worker scans every workspace
            visible to the API key.
    """

    def __init__(
        self,
        client: Plinth,
        *,
        runtime: WorkflowRuntime | None = None,
        concurrency: int = 4,
        lease_ttl: int = 60,
        heartbeat_interval: int = 15,
        worker_heartbeat_interval: int = 30,
        poll_interval: float = 2.0,
        workspace_filter: Iterable[str] | None = None,
    ) -> None:
        if heartbeat_interval >= lease_ttl:
            raise ValueError(
                "heartbeat_interval must be < lease_ttl, otherwise the "
                "lease will expire between heartbeats"
            )
        self.client = client
        self.runtime = runtime or client.workflow_runtime
        self.concurrency = concurrency
        self.lease_ttl = lease_ttl
        self.heartbeat_interval = heartbeat_interval
        self.worker_heartbeat_interval = worker_heartbeat_interval
        self.poll_interval = poll_interval
        self.workspace_filter = (
            list(workspace_filter) if workspace_filter is not None else None
        )

        self.worker_id: str | None = None
        # ``_stopping`` is lazily bound to the current running loop on
        # first access. asyncio.Event() implicitly binds to whatever
        # loop is current at construction time, which on Python 3.9 is
        # often "no loop" — leading to a "Future attached to a different
        # loop" crash on first ``.wait()``. Lazy-init sidesteps this.
        self._stopping: asyncio.Event | None = None
        self._stop_requested = False
        self._tasks: list[asyncio.Task] = []
        self._stats: dict[str, int] = {
            "leased": 0,
            "completed": 0,
            "failed": 0,
            "lost": 0,
        }
        self._log = get_logger().bind(worker="plinth-workflow-worker")

    def _ensure_stop_event(self) -> asyncio.Event:
        if self._stopping is None:
            self._stopping = asyncio.Event()
            if self._stop_requested:
                self._stopping.set()
        return self._stopping

    # ------------------------------------------------------------------
    # Public lifecycle
    # ------------------------------------------------------------------

    async def run(self) -> None:
        """Run the worker until :meth:`stop` is called.

        Blocks until the stop event is set. Always tries to drain
        gracefully — even if the slot tasks raise.
        """

        # Bind the asyncio.Event to the running loop now (lazy init).
        stopping = self._ensure_stop_event()

        if not self.runtime.keys():
            self._log.warning(
                "worker.no_handlers",
                hint=(
                    "no @workflow_handler decorations found; the worker "
                    "will register but never claim work"
                ),
            )

        registration = self.client.workers.register()
        self.worker_id = registration.id
        self._log = self._log.bind(worker_id=self.worker_id)
        self._log.info(
            "worker.registered",
            handlers=[list(k) for k in self.runtime.keys()],
            concurrency=self.concurrency,
        )

        # Worker-level heartbeat keeps the reaper from sweeping us.
        self._tasks.append(
            asyncio.create_task(self._worker_heartbeat_loop(), name="worker-heartbeat")
        )

        # ``concurrency`` slot tasks each independently poll + lease + run.
        for i in range(self.concurrency):
            self._tasks.append(
                asyncio.create_task(self._slot_loop(i), name=f"slot-{i}")
            )

        try:
            await stopping.wait()
        finally:
            await self._shutdown()

    def stop(self) -> None:
        """Signal a graceful shutdown.

        Idempotent — calling it twice has no effect. Safe to call before
        :meth:`run` (the request is queued and applied when the run
        loop wires up its event).
        """
        self._stop_requested = True
        if self._stopping is not None and not self._stopping.is_set():
            self._log.info("worker.stopping")
            self._stopping.set()

    def install_signal_handlers(self) -> None:
        """Install SIGTERM + SIGINT → :meth:`stop` (Unix only)."""
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            try:
                loop.add_signal_handler(sig, self.stop)
            except (NotImplementedError, RuntimeError):  # pragma: no cover - non-unix
                pass

    @property
    def stats(self) -> dict[str, int]:
        """Snapshot of execution counters."""
        return dict(self._stats)

    # ------------------------------------------------------------------
    # Internal: worker heartbeat
    # ------------------------------------------------------------------

    async def _worker_heartbeat_loop(self) -> None:
        while not self._stopping.is_set():
            try:
                if self.worker_id:
                    self.client.workers.heartbeat(self.worker_id)
            except Exception as exc:  # noqa: BLE001
                self._log.warning("worker.heartbeat.error", error=str(exc))
            try:
                await asyncio.wait_for(
                    self._stopping.wait(),
                    timeout=self.worker_heartbeat_interval,
                )
                return  # stop signalled
            except asyncio.TimeoutError:
                continue

    # ------------------------------------------------------------------
    # Internal: slot loop
    # ------------------------------------------------------------------

    async def _slot_loop(self, slot_idx: int) -> None:
        log = self._log.bind(slot=slot_idx)
        while not self._stopping.is_set():
            try:
                claimed = await self._poll_lease_and_execute(log)
            except Exception as exc:  # noqa: BLE001
                log.warning("worker.slot.error", error=str(exc))
                claimed = False

            if not claimed and not self._stopping.is_set():
                # Idle backoff so the loop doesn't spin when there's no work.
                try:
                    await asyncio.wait_for(
                        self._stopping.wait(),
                        timeout=self.poll_interval,
                    )
                    return
                except asyncio.TimeoutError:
                    continue

    async def _poll_lease_and_execute(self, log) -> bool:
        """Poll for one pending step + execute it. Returns ``True`` if
        a step was claimed this iteration."""

        candidate = await self._next_candidate()
        if candidate is None:
            return False

        ws, wf, step = candidate
        log = log.bind(
            workspace_id=ws.id,
            workflow_id=wf.id,
            step_id=step.id,
            step_name=step.name,
        )

        lease = wf.lease_step(step.id, self.worker_id, ttl=self.lease_ttl)
        if lease is None:
            log.debug("worker.lease.lost")
            self._stats["lost"] = self._stats.get("lost", 0) + 1
            return False

        self._stats["leased"] += 1
        log.info("worker.lease.acquired")

        # Re-fetch step now that it's running so handlers see the
        # updated status. ``pending_steps()`` returned a snapshot taken
        # before the lease.
        wf.refresh()
        for s in wf.steps:
            if s.id == step.id:
                step = s
                break

        ctx = HandlerContext(
            client=self.client,
            workspace=ws,
            workflow=wf,
            step=step,
            worker_id=self.worker_id or "",
        )

        # Per-lease heartbeat keeps expires_at fresh while the handler runs.
        heartbeat_stop = asyncio.Event()
        heartbeat_task = asyncio.create_task(
            self._lease_heartbeat_loop(wf, step.id, heartbeat_stop),
            name=f"hb-{step.id}",
        )

        try:
            try:
                output = await self.runtime.dispatch(wf.name, step.name, ctx)
            except Exception as exc:  # noqa: BLE001
                log.warning("worker.handler.failed", error=str(exc))
                self._stats["failed"] += 1
                heartbeat_stop.set()
                with contextlib.suppress(asyncio.CancelledError):
                    await heartbeat_task
                self._safe_release(wf, step.id, status="failed", error=str(exc))
                return True

            heartbeat_stop.set()
            with contextlib.suppress(asyncio.CancelledError):
                await heartbeat_task

            self._safe_release(wf, step.id, status="completed", output=output)
            self._stats["completed"] += 1
            log.info("worker.step.completed")
            return True
        except Exception as exc:  # pragma: no cover - defensive
            log.warning("worker.slot.unexpected", error=str(exc))
            heartbeat_stop.set()
            with contextlib.suppress(asyncio.CancelledError):
                await heartbeat_task
            self._safe_release(wf, step.id, status="failed", error=str(exc))
            return True

    def _safe_release(
        self,
        wf: WorkflowHandle,
        step_id: str,
        *,
        status: str,
        output: Any = None,
        error: str | None = None,
    ) -> None:
        try:
            wf.release_step(
                step_id,
                self.worker_id or "",
                status=status,
                output=output,
                error=error,
            )
        except PlinthError as exc:
            self._log.warning(
                "worker.release.error",
                step_id=step_id,
                status=status,
                error=str(exc),
            )

    async def _lease_heartbeat_loop(
        self,
        wf: WorkflowHandle,
        step_id: str,
        stop_event: asyncio.Event,
    ) -> None:
        while not stop_event.is_set():
            try:
                await asyncio.wait_for(
                    stop_event.wait(),
                    timeout=self.heartbeat_interval,
                )
                return
            except asyncio.TimeoutError:
                pass

            try:
                wf.heartbeat_step(step_id, self.worker_id or "")
            except PlinthError as exc:
                self._log.warning(
                    "worker.lease.heartbeat.error",
                    step_id=step_id,
                    error=str(exc),
                )
                # Heartbeat failed — likely the reaper claimed our lease.
                # Stop trying; the slot loop will see the failure on
                # release.
                return

    # ------------------------------------------------------------------
    # Internal: candidate discovery
    # ------------------------------------------------------------------

    async def _next_candidate(
        self,
    ) -> tuple[Any, WorkflowHandle, WorkflowStep] | None:
        """Find one pending step whose ``(workflow_name, step_name)`` is
        in the dispatch table.

        Returns ``(workspace, workflow_handle, step)`` or ``None``.
        Filters by :attr:`workspace_filter` when set.
        """

        try:
            workspaces = self.client.list_workspaces()
        except PlinthError as exc:
            self._log.warning("worker.list_workspaces.error", error=str(exc))
            return None

        for ws in workspaces:
            if self.workspace_filter and (
                ws.id not in self.workspace_filter
                and ws.name not in self.workspace_filter
            ):
                continue
            try:
                workflows = ws.workflows.list()
            except PlinthError:
                continue
            for wf_summary in workflows:
                # Only bother polling if at least one step name matches
                # a handler we own.
                handler_steps = {
                    sn
                    for (wn, sn) in self.runtime.keys()
                    if wn == wf_summary.name
                }
                if not handler_steps:
                    continue
                try:
                    wf = ws.workflows.get(wf_summary.id)
                except WorkflowNotFound:
                    continue
                try:
                    pending = wf.pending_steps()
                except PlinthError:
                    continue
                for step in pending:
                    if step.name in handler_steps:
                        return ws, wf, step
        return None

    # ------------------------------------------------------------------
    # Internal: shutdown
    # ------------------------------------------------------------------

    async def _shutdown(self) -> None:
        # Cancel all slot tasks; each one drains its own in-flight work
        # via the heartbeat-stop pattern. We only cancel the OUTER tasks,
        # not the heartbeat or release calls.
        for task in self._tasks:
            if not task.done():
                task.cancel()
        for task in self._tasks:
            try:
                await task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass

        if self.worker_id:
            try:
                self.client.workers.drain(self.worker_id)
                self._log.info("worker.drained")
            except Exception as exc:  # noqa: BLE001
                self._log.warning("worker.drain.error", error=str(exc))


__all__ = ["Worker"]
