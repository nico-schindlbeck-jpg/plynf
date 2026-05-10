# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 The Plinth Authors
"""Workflow storage + business logic for the workspace service.

A workflow is a manifest (ordered list of step names) plus a log of
``WorkflowStep`` rows. Each step can be retried — re-starting a step
under the same name allocates ``attempt = max+1``. The workflow's
overall status is derived from its step log:

- pending → no step has been started
- running → at least one step has been created
- completed → every manifest entry has a ``completed`` step
- failed   → at least one step is ``failed`` and the workflow has not
  recovered to ``completed`` (we treat failure as terminal in v0.2)
- cancelled → explicit ``POST /cancel`` was called

``ResumeInfo`` answers the only question that matters after a crash:
"what's the next manifest entry I haven't completed, and what snapshot
should I restore from?"
"""

from __future__ import annotations

import json
import random
from datetime import timedelta
from pathlib import Path
from typing import Any

import aiosqlite
from ulid import ULID

from .db import connect, iso, now_utc, parse_ts
from .exceptions import (
    InvalidArguments,
    InvalidWorkflowStep,
    PlinthError,
    WorkflowNotFound,
    WorkflowStepNotFound,
    WorkspaceNotFound,
)
from .models import DLQEntry, ResumeInfo, Workflow, WorkflowStep

TERMINAL_STEP_STATUSES = {"completed", "failed", "cancelled"}


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class DLQEntryNotFound(PlinthError):
    """Raised when a DLQ entry id does not match any row."""

    code = "DLQ_ENTRY_NOT_FOUND"
    status_code = 404
    message = "DLQ entry not found"

    def __init__(self, dlq_id: str) -> None:
        super().__init__(
            f"DLQ entry {dlq_id} not found",
            details={"dlq_id": dlq_id},
        )


# ---------------------------------------------------------------------------
# Retry helpers
# ---------------------------------------------------------------------------


def _new_dlq_id() -> str:
    return f"dlqstep_{ULID()}"


def compute_retry_delay(
    *,
    attempt: int,
    policy: str,
    initial: float,
    max_delay: float,
    jitter: bool,
    rng: random.Random | None = None,
) -> float:
    """Compute the delay before the *next* retry of a failing step.

    ``attempt`` is the just-finished attempt number (1-indexed). The
    returned value is the seconds to wait before the next attempt may
    run.

    * ``policy="none"`` → returns 0 (caller should not be retrying).
    * ``policy="fixed"`` → ``initial`` (capped at ``max_delay``).
    * ``policy="exponential"`` → ``initial * 2^(attempt-1)`` capped at
      ``max_delay``.

    Jitter (when enabled) multiplies the result by a uniform random
    factor in ``[0.75, 1.25]``. The ``rng`` parameter exists so tests
    can pin the random stream for determinism; production callers omit
    it and pick up the module-level :func:`random.random`.
    """

    if policy == "none":
        return 0.0
    if attempt < 1:
        attempt = 1
    if policy == "fixed":
        base = initial
    elif policy == "exponential":
        # 2^(attempt-1). Cap the exponent so we don't overflow on
        # absurd attempt counts; max_delay is the real ceiling.
        exp = max(0, attempt - 1)
        # ``min`` after the multiplication so a small initial doesn't
        # mask the cap.
        base = initial * (2 ** min(exp, 30))
    else:
        # Unknown policy → behave like fixed initial.
        base = initial

    capped = min(base, max_delay)
    if jitter and capped > 0:
        r = rng.random() if rng is not None else random.random()
        # Uniform multiplier in [0.75, 1.25].
        capped = capped * (0.75 + 0.5 * r)
    return float(capped)


def _new_workflow_id() -> str:
    return f"wf_{ULID()}"


def _new_step_id() -> str:
    return f"step_{ULID()}"


def _row_to_step(row: aiosqlite.Row) -> WorkflowStep:
    # v1.1 retry columns are read defensively so older test fixtures that
    # build rows without the new columns still work — `aiosqlite.Row`
    # supports `__contains__` on the underlying description, but legacy
    # tests using sqlite3.Row may not. Using a try/except around column
    # access keeps the helper resilient.
    keys = set(row.keys()) if hasattr(row, "keys") else set()

    def _opt(col: str, default: Any = None) -> Any:
        if col in keys:
            return row[col]
        return default

    return WorkflowStep(
        id=row["id"],
        workflow_id=row["workflow_id"],
        name=row["name"],
        status=row["status"],
        attempt=int(row["attempt"]),
        started_at=parse_ts(row["started_at"]),
        finished_at=parse_ts(row["finished_at"]),
        input=json.loads(row["input"]) if row["input"] is not None else None,
        output=json.loads(row["output"]) if row["output"] is not None else None,
        error=row["error"],
        snapshot_id=row["snapshot_id"],
        created_at=parse_ts(row["created_at"]),  # type: ignore[arg-type]
        max_attempts=int(_opt("max_attempts", 1) or 1),
        retry_policy=_opt("retry_policy", "none") or "none",
        retry_initial_delay_seconds=float(
            _opt("retry_initial_delay_seconds", 1.0) or 1.0
        ),
        retry_max_delay_seconds=float(
            _opt("retry_max_delay_seconds", 60.0) or 60.0
        ),
        retry_jitter=bool(int(_opt("retry_jitter", 1) or 0)),
        next_retry_at=parse_ts(_opt("next_retry_at")),
    )


def _row_to_dlq_entry(row: aiosqlite.Row) -> DLQEntry:
    snapshot_str = row["step_snapshot"]
    snapshot = json.loads(snapshot_str) if snapshot_str else {}
    return DLQEntry(
        id=row["id"],
        step_id=row["step_id"],
        workflow_id=row["workflow_id"],
        workspace_id=row["workspace_id"],
        step_name=row["step_name"],
        attempts=int(row["attempts"]),
        last_error=row["last_error"],
        failed_at=parse_ts(row["failed_at"]),  # type: ignore[arg-type]
        step_snapshot=snapshot,
    )


def _row_to_workflow(
    row: aiosqlite.Row,
    steps: list[WorkflowStep],
) -> Workflow:
    return Workflow(
        id=row["id"],
        workspace_id=row["workspace_id"],
        name=row["name"],
        steps_manifest=json.loads(row["steps_manifest"]),
        steps=steps,
        status=row["status"],
        metadata=json.loads(row["metadata"] or "{}"),
        created_at=parse_ts(row["created_at"]),  # type: ignore[arg-type]
        started_at=parse_ts(row["started_at"]),
        finished_at=parse_ts(row["finished_at"]),
    )


class WorkflowStore:
    """CRUD + lifecycle logic for workflows."""

    def __init__(self, db_path: Path) -> None:
        self.db_path = db_path

    # ------------------------------------------------------------------ helpers

    @staticmethod
    async def _assert_workspace(
        conn: aiosqlite.Connection,
        workspace_id: str,
    ) -> None:
        cur = await conn.execute(
            "SELECT 1 FROM workspaces WHERE id=?",
            (workspace_id,),
        )
        row = await cur.fetchone()
        await cur.close()
        if row is None:
            raise WorkspaceNotFound(workspace_id)

    @staticmethod
    async def _workflow_row(
        conn: aiosqlite.Connection,
        workspace_id: str,
        workflow_id: str,
    ) -> aiosqlite.Row | None:
        cur = await conn.execute(
            "SELECT * FROM workflows WHERE id=? AND workspace_id=?",
            (workflow_id, workspace_id),
        )
        row = await cur.fetchone()
        await cur.close()
        return row

    @staticmethod
    async def _step_rows(
        conn: aiosqlite.Connection,
        workflow_id: str,
    ) -> list[aiosqlite.Row]:
        cur = await conn.execute(
            "SELECT * FROM workflow_steps WHERE workflow_id=? "
            "ORDER BY created_at ASC, id ASC",
            (workflow_id,),
        )
        rows = await cur.fetchall()
        await cur.close()
        return list(rows)

    # ------------------------------------------------------------------ create

    async def create_workflow(
        self,
        workspace_id: str,
        name: str,
        steps: list[str],
        *,
        metadata: dict[str, Any] | None = None,
    ) -> Workflow:
        if not name:
            raise InvalidArguments("workflow name must be non-empty")
        if not steps:
            raise InvalidArguments("workflow must have at least one step")
        if any(not s for s in steps):
            raise InvalidArguments("step names must be non-empty strings")
        if len(set(steps)) != len(steps):
            raise InvalidArguments("step names must be unique")

        async with connect(self.db_path) as conn:
            await self._assert_workspace(conn, workspace_id)
            wf_id = _new_workflow_id()
            ts = now_utc()
            await conn.execute(
                "INSERT INTO workflows "
                "(id, workspace_id, name, steps_manifest, metadata, "
                " status, created_at) "
                "VALUES (?, ?, ?, ?, ?, 'pending', ?)",
                (
                    wf_id,
                    workspace_id,
                    name,
                    json.dumps(list(steps)),
                    json.dumps(metadata or {}, sort_keys=True),
                    iso(ts),
                ),
            )
            await conn.commit()

            return Workflow(
                id=wf_id,
                workspace_id=workspace_id,
                name=name,
                steps_manifest=list(steps),
                steps=[],
                status="pending",
                metadata=metadata or {},
                created_at=ts,
                started_at=None,
                finished_at=None,
            )

    # ------------------------------------------------------------------ list / get

    async def list_workflows(self, workspace_id: str) -> list[Workflow]:
        async with connect(self.db_path) as conn:
            await self._assert_workspace(conn, workspace_id)

            cur = await conn.execute(
                "SELECT * FROM workflows WHERE workspace_id=? "
                "ORDER BY created_at ASC",
                (workspace_id,),
            )
            wf_rows = await cur.fetchall()
            await cur.close()

            out: list[Workflow] = []
            for wf_row in wf_rows:
                step_rows = await self._step_rows(conn, wf_row["id"])
                steps = [_row_to_step(r) for r in step_rows]
                out.append(_row_to_workflow(wf_row, steps))
            return out

    async def get_workflow(
        self,
        workspace_id: str,
        workflow_id: str,
    ) -> Workflow:
        async with connect(self.db_path) as conn:
            await self._assert_workspace(conn, workspace_id)

            wf_row = await self._workflow_row(conn, workspace_id, workflow_id)
            if wf_row is None:
                raise WorkflowNotFound(workflow_id)
            step_rows = await self._step_rows(conn, workflow_id)
            steps = [_row_to_step(r) for r in step_rows]
            return _row_to_workflow(wf_row, steps)

    # ------------------------------------------------------------------ create step

    async def create_step(
        self,
        workspace_id: str,
        workflow_id: str,
        name: str,
        *,
        snapshot_id: str | None = None,
        input_: Any | None = None,
        initial_status: str = "running",
        max_attempts: int = 1,
        retry_policy: str = "none",
        retry_initial_delay_seconds: float = 1.0,
        retry_max_delay_seconds: float = 60.0,
        retry_jitter: bool = True,
    ) -> WorkflowStep:
        """Create a new step row.

        ``initial_status`` defaults to ``running`` (the v0.2 in-process
        flow where the agent starts work immediately). Pass ``pending``
        to create a step in the durable-executor pool that workers will
        pick up via :py:meth:`LeaseStore.acquire_lease`.

        v1.1: ``max_attempts`` / ``retry_policy`` / ``retry_initial_delay_seconds``
        / ``retry_max_delay_seconds`` / ``retry_jitter`` configure the
        per-step retry policy. Defaults preserve v1.0 behaviour
        (``max_attempts=1``).
        """

        if initial_status not in {"running", "pending"}:
            raise InvalidArguments(
                f"initial_status must be 'running' or 'pending', "
                f"got {initial_status!r}"
            )
        if max_attempts < 1:
            raise InvalidArguments("max_attempts must be >= 1")
        if retry_policy not in {"none", "exponential", "fixed"}:
            raise InvalidArguments(
                f"retry_policy must be 'none', 'exponential', or 'fixed', "
                f"got {retry_policy!r}"
            )
        if retry_initial_delay_seconds < 0:
            raise InvalidArguments("retry_initial_delay_seconds must be >= 0")
        if retry_max_delay_seconds < 0:
            raise InvalidArguments("retry_max_delay_seconds must be >= 0")

        async with connect(self.db_path) as conn:
            await self._assert_workspace(conn, workspace_id)

            wf_row = await self._workflow_row(conn, workspace_id, workflow_id)
            if wf_row is None:
                raise WorkflowNotFound(workflow_id)
            manifest = json.loads(wf_row["steps_manifest"])
            if name not in manifest:
                raise InvalidWorkflowStep(workflow_id, name, manifest)

            cur = await conn.execute(
                "SELECT MAX(attempt) FROM workflow_steps "
                "WHERE workflow_id=? AND name=?",
                (workflow_id, name),
            )
            attempt_row = await cur.fetchone()
            await cur.close()
            attempt = int((attempt_row[0] or 0) + 1)

            step_id = _new_step_id()
            ts = now_utc()
            started_at_value = iso(ts) if initial_status == "running" else None
            await conn.execute(
                "INSERT INTO workflow_steps "
                "(id, workflow_id, name, status, attempt, started_at, "
                " finished_at, input, output, error, snapshot_id, created_at, "
                " max_attempts, retry_policy, retry_initial_delay_seconds, "
                " retry_max_delay_seconds, retry_jitter, next_retry_at) "
                "VALUES (?, ?, ?, ?, ?, ?, NULL, ?, NULL, NULL, ?, ?, "
                " ?, ?, ?, ?, ?, NULL)",
                (
                    step_id,
                    workflow_id,
                    name,
                    initial_status,
                    attempt,
                    started_at_value,
                    json.dumps(input_) if input_ is not None else None,
                    snapshot_id,
                    iso(ts),
                    int(max_attempts),
                    retry_policy,
                    float(retry_initial_delay_seconds),
                    float(retry_max_delay_seconds),
                    1 if retry_jitter else 0,
                ),
            )

            # Bump the workflow into running state on first running step.
            # Pending steps don't bump the workflow — the lease acquire
            # path does that when a worker actually starts work.
            if wf_row["status"] == "pending" and initial_status == "running":
                await conn.execute(
                    "UPDATE workflows SET status='running', started_at=? "
                    "WHERE id=?",
                    (iso(ts), workflow_id),
                )

            await conn.commit()
            return WorkflowStep(
                id=step_id,
                workflow_id=workflow_id,
                name=name,
                status=initial_status,  # type: ignore[arg-type]
                attempt=attempt,
                started_at=ts if initial_status == "running" else None,
                finished_at=None,
                input=input_,
                output=None,
                error=None,
                snapshot_id=snapshot_id,
                created_at=ts,
                max_attempts=int(max_attempts),
                retry_policy=retry_policy,  # type: ignore[arg-type]
                retry_initial_delay_seconds=float(retry_initial_delay_seconds),
                retry_max_delay_seconds=float(retry_max_delay_seconds),
                retry_jitter=bool(retry_jitter),
                next_retry_at=None,
            )

    # ------------------------------------------------------------------ update step

    async def update_step(
        self,
        workspace_id: str,
        workflow_id: str,
        step_id: str,
        *,
        status: str,
        output: Any | None = None,
        error: str | None = None,
        snapshot_id: str | None = None,
        rng: random.Random | None = None,
    ) -> WorkflowStep:
        """Patch a step row to a new ``status`` (and optional output/error).

        v1.1: when ``status='failed'`` and the step has more attempts
        remaining (``attempt < max_attempts`` AND policy != 'none'), the
        step row is reverted to ``pending`` with ``next_retry_at`` set
        ``now + delay``. The ``pending_steps`` query honours
        ``next_retry_at`` so workers won't pick up a retry-pending row
        until the timer has elapsed. When the final attempt fails, the
        step is copied to ``workflow_dlq`` in the same transaction.
        """
        if status not in {"running", "completed", "failed", "cancelled"}:
            raise InvalidArguments(f"invalid status {status!r}")

        async with connect(self.db_path) as conn:
            await self._assert_workspace(conn, workspace_id)

            wf_row = await self._workflow_row(conn, workspace_id, workflow_id)
            if wf_row is None:
                raise WorkflowNotFound(workflow_id)

            cur = await conn.execute(
                "SELECT * FROM workflow_steps WHERE id=? AND workflow_id=?",
                (step_id, workflow_id),
            )
            step_row = await cur.fetchone()
            await cur.close()
            if step_row is None:
                raise WorkflowStepNotFound(workflow_id, step_id)

            ts = now_utc()

            # Preserve existing snapshot_id if caller didn't pass one.
            new_snapshot = (
                snapshot_id if snapshot_id is not None else step_row["snapshot_id"]
            )

            # v1.1 — retry routing for `failed`.
            keys = set(step_row.keys()) if hasattr(step_row, "keys") else set()
            attempt = int(step_row["attempt"])
            max_attempts = int(
                (step_row["max_attempts"] if "max_attempts" in keys else 1) or 1
            )
            policy = (
                step_row["retry_policy"] if "retry_policy" in keys else "none"
            ) or "none"
            initial = float(
                (step_row["retry_initial_delay_seconds"]
                 if "retry_initial_delay_seconds" in keys else 1.0)
                or 0.0
            )
            cap = float(
                (step_row["retry_max_delay_seconds"]
                 if "retry_max_delay_seconds" in keys else 60.0)
                or 0.0
            )
            jitter = bool(int(
                (step_row["retry_jitter"]
                 if "retry_jitter" in keys else 1) or 0
            ))

            should_retry = (
                status == "failed"
                and policy != "none"
                and attempt < max_attempts
            )

            if should_retry:
                delay = compute_retry_delay(
                    attempt=attempt,
                    policy=policy,
                    initial=initial,
                    max_delay=cap,
                    jitter=jitter,
                    rng=rng,
                )
                next_retry = ts + timedelta(seconds=delay)
                # Revert to pending. The next pending_steps poll will
                # exclude this row until next_retry_at <= now. We bump
                # the attempt counter so the next failure correctly
                # tracks position toward max_attempts.
                await conn.execute(
                    "UPDATE workflow_steps SET status='pending', "
                    "started_at=NULL, finished_at=NULL, error=?, "
                    "attempt=?, next_retry_at=? WHERE id=?",
                    (error, attempt + 1, iso(next_retry), step_id),
                )
            else:
                terminal = status in TERMINAL_STEP_STATUSES
                new_finished = iso(ts) if terminal else None

                await conn.execute(
                    "UPDATE workflow_steps SET status=?, output=?, error=?, "
                    " snapshot_id=?, finished_at=COALESCE(?, finished_at), "
                    " next_retry_at=NULL "
                    "WHERE id=?",
                    (
                        status,
                        json.dumps(output) if output is not None else None,
                        error,
                        new_snapshot,
                        new_finished,
                        step_id,
                    ),
                )

                # On the *terminal* failure (attempts exhausted, or no
                # retry policy), copy the step row into the DLQ.
                if status == "failed":
                    await self._copy_step_to_dlq(
                        conn,
                        step_id=step_id,
                        workflow_id=workflow_id,
                        workspace_id=workspace_id,
                        step_row=step_row,
                        attempts=attempt,
                        last_error=error,
                        failed_at=ts,
                    )

            # Now derive workflow status from the updated step set.
            await self._maybe_update_workflow_status(
                conn,
                workflow_id,
                wf_row,
                ts,
            )

            await conn.commit()

            cur = await conn.execute(
                "SELECT * FROM workflow_steps WHERE id=?",
                (step_id,),
            )
            updated = await cur.fetchone()
            await cur.close()
            assert updated is not None  # we just updated it
            return _row_to_step(updated)

    # ------------------------------------------------------------------ DLQ

    async def _copy_step_to_dlq(
        self,
        conn: aiosqlite.Connection,
        *,
        step_id: str,
        workflow_id: str,
        workspace_id: str,
        step_row: aiosqlite.Row,
        attempts: int,
        last_error: str | None,
        failed_at,
    ) -> str:
        """Insert a workflow_dlq row for the failing step.

        Builds a JSON snapshot from the step row's columns so the DLQ
        entry survives subsequent mutations to the step table. Caller
        owns the transaction.
        """
        snapshot = {
            "id": step_row["id"],
            "workflow_id": step_row["workflow_id"],
            "name": step_row["name"],
            "status": "failed",
            "attempt": attempts,
            "input": json.loads(step_row["input"]) if step_row["input"] else None,
            "output": json.loads(step_row["output"]) if step_row["output"] else None,
            "error": last_error,
            "snapshot_id": step_row["snapshot_id"],
            "started_at": step_row["started_at"],
            "finished_at": iso(failed_at),
            "created_at": step_row["created_at"],
        }
        dlq_id = _new_dlq_id()
        await conn.execute(
            "INSERT INTO workflow_dlq "
            "(id, step_id, workflow_id, workspace_id, step_name, attempts, "
            " last_error, failed_at, step_snapshot) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                dlq_id,
                step_id,
                workflow_id,
                workspace_id,
                step_row["name"],
                int(attempts),
                last_error,
                iso(failed_at),
                json.dumps(snapshot, sort_keys=True),
            ),
        )
        return dlq_id

    async def list_dlq(
        self,
        workspace_id: str,
        workflow_id: str,
    ) -> list[DLQEntry]:
        """List all DLQ entries for a workflow (failed_at desc)."""

        async with connect(self.db_path) as conn:
            await self._assert_workspace(conn, workspace_id)
            wf_row = await self._workflow_row(conn, workspace_id, workflow_id)
            if wf_row is None:
                raise WorkflowNotFound(workflow_id)
            cur = await conn.execute(
                "SELECT * FROM workflow_dlq WHERE workflow_id=? "
                "ORDER BY failed_at DESC, id DESC",
                (workflow_id,),
            )
            rows = await cur.fetchall()
            await cur.close()
            return [_row_to_dlq_entry(r) for r in rows]

    async def replay_dlq(
        self,
        workspace_id: str,
        workflow_id: str,
        dlq_id: str,
    ) -> WorkflowStep:
        """Re-queue a DLQ entry as a fresh attempt of the same step name.

        Reads the DLQ row's ``step_snapshot``, creates a new step in the
        workflow with ``attempts=0`` (the existing attempt counter logic
        derives ``attempt`` from the running max), then deletes the DLQ
        row. The new step inherits the original step's ``input`` and
        ``snapshot_id`` so the worker that picks it up can resume from
        the same checkpoint.

        The new step starts with ``status='pending'`` so a worker can
        immediately lease it.
        """

        async with connect(self.db_path) as conn:
            await self._assert_workspace(conn, workspace_id)
            wf_row = await self._workflow_row(conn, workspace_id, workflow_id)
            if wf_row is None:
                raise WorkflowNotFound(workflow_id)

            cur = await conn.execute(
                "SELECT * FROM workflow_dlq WHERE id=? AND workflow_id=?",
                (dlq_id, workflow_id),
            )
            dlq_row = await cur.fetchone()
            await cur.close()
            if dlq_row is None:
                raise DLQEntryNotFound(dlq_id)

        snapshot = json.loads(dlq_row["step_snapshot"])
        # Recreate the step using the standard create_step path so the
        # attempt counter and workflow-status logic stay in sync. We
        # request ``initial_status='pending'`` because a replay is
        # explicitly handing the step back to a worker.
        return await self.create_step(
            workspace_id,
            workflow_id,
            dlq_row["step_name"],
            snapshot_id=snapshot.get("snapshot_id"),
            input_=snapshot.get("input"),
            initial_status="pending",
            # Replays default to no retry — the operator is in the loop.
            # If they want retries on the replay, they can re-send via
            # the SDK's `replay_dlq(...)` with retry params (future).
            max_attempts=1,
            retry_policy="none",
        )

    async def delete_dlq(
        self,
        workspace_id: str,
        workflow_id: str,
        dlq_id: str,
    ) -> None:
        """Delete a DLQ row (no replay). Idempotent: re-call → 404."""

        async with connect(self.db_path) as conn:
            await self._assert_workspace(conn, workspace_id)
            wf_row = await self._workflow_row(conn, workspace_id, workflow_id)
            if wf_row is None:
                raise WorkflowNotFound(workflow_id)

            cur = await conn.execute(
                "SELECT id FROM workflow_dlq WHERE id=? AND workflow_id=?",
                (dlq_id, workflow_id),
            )
            row = await cur.fetchone()
            await cur.close()
            if row is None:
                raise DLQEntryNotFound(dlq_id)

            await conn.execute(
                "DELETE FROM workflow_dlq WHERE id=?",
                (dlq_id,),
            )
            await conn.commit()

    async def delete_replayed_dlq(
        self,
        workflow_id: str,
        dlq_id: str,
    ) -> None:
        """Internal: delete a DLQ row after a successful replay.

        Distinct from :meth:`delete_dlq` so the public API can
        differentiate "operator dismissed" from "replayed". The two are
        identical at the SQL level today, but keeping the seam open
        leaves room for an audit trail later.
        """
        async with connect(self.db_path) as conn:
            await conn.execute(
                "DELETE FROM workflow_dlq WHERE id=? AND workflow_id=?",
                (dlq_id, workflow_id),
            )
            await conn.commit()

    async def _maybe_update_workflow_status(
        self,
        conn: aiosqlite.Connection,
        workflow_id: str,
        wf_row: aiosqlite.Row,
        ts,
    ) -> None:
        """Recompute the workflow status from its step rows."""

        if wf_row["status"] == "cancelled":
            return  # cancelled is final.

        manifest: list[str] = json.loads(wf_row["steps_manifest"])

        cur = await conn.execute(
            "SELECT name, status FROM workflow_steps WHERE workflow_id=?",
            (workflow_id,),
        )
        rows = await cur.fetchall()
        await cur.close()

        # Map manifest name → set of statuses observed.
        statuses_by_name: dict[str, set[str]] = {n: set() for n in manifest}
        any_failed = False
        for r in rows:
            statuses_by_name.setdefault(r["name"], set()).add(r["status"])
            if r["status"] == "failed":
                any_failed = True

        all_completed = all("completed" in statuses_by_name[n] for n in manifest)

        # Failure cascade — if there's at least one failed step AND no
        # subsequent attempt has yet completed that step, the workflow
        # is failed (terminal).
        any_failed_unrecovered = False
        if any_failed:
            for n, observed in statuses_by_name.items():
                if "failed" in observed and "completed" not in observed:
                    any_failed_unrecovered = True
                    break

        if all_completed:
            await conn.execute(
                "UPDATE workflows SET status='completed', finished_at=? "
                "WHERE id=?",
                (iso(ts), workflow_id),
            )
        elif any_failed_unrecovered:
            await conn.execute(
                "UPDATE workflows SET status='failed', finished_at=? "
                "WHERE id=?",
                (iso(ts), workflow_id),
            )

    # ------------------------------------------------------------------ resume

    async def resume_info(
        self,
        workspace_id: str,
        workflow_id: str,
    ) -> ResumeInfo:
        async with connect(self.db_path) as conn:
            await self._assert_workspace(conn, workspace_id)

            wf_row = await self._workflow_row(conn, workspace_id, workflow_id)
            if wf_row is None:
                raise WorkflowNotFound(workflow_id)

            manifest: list[str] = json.loads(wf_row["steps_manifest"])

            # Most recent completed step (by finished_at desc, fallback to created_at).
            cur = await conn.execute(
                "SELECT * FROM workflow_steps "
                "WHERE workflow_id=? AND status='completed' "
                "ORDER BY finished_at DESC, created_at DESC LIMIT 1",
                (workflow_id,),
            )
            last_completed_row = await cur.fetchone()
            await cur.close()
            last_completed = (
                _row_to_step(last_completed_row) if last_completed_row else None
            )

            # Set of manifest entries that have at least one completed attempt.
            cur = await conn.execute(
                "SELECT DISTINCT name FROM workflow_steps "
                "WHERE workflow_id=? AND status='completed'",
                (workflow_id,),
            )
            completed_names = {r["name"] for r in await cur.fetchall()}
            await cur.close()

            next_step: str | None = None
            for n in manifest:
                if n not in completed_names:
                    next_step = n
                    break

            snapshot_id = last_completed.snapshot_id if last_completed else None

            return ResumeInfo(
                workflow_id=workflow_id,
                workflow_status=wf_row["status"],
                next_step=next_step,
                last_completed=last_completed,
                snapshot_id=snapshot_id,
            )

    # ------------------------------------------------------------------ cancel

    async def cancel_workflow(
        self,
        workspace_id: str,
        workflow_id: str,
    ) -> Workflow:
        async with connect(self.db_path) as conn:
            await self._assert_workspace(conn, workspace_id)

            wf_row = await self._workflow_row(conn, workspace_id, workflow_id)
            if wf_row is None:
                raise WorkflowNotFound(workflow_id)

            ts = now_utc()
            # Cancel any running steps.
            await conn.execute(
                "UPDATE workflow_steps SET status='cancelled', finished_at=? "
                "WHERE workflow_id=? AND status='running'",
                (iso(ts), workflow_id),
            )
            await conn.execute(
                "UPDATE workflows SET status='cancelled', finished_at=? "
                "WHERE id=?",
                (iso(ts), workflow_id),
            )
            await conn.commit()

            cur = await conn.execute(
                "SELECT * FROM workflows WHERE id=?", (workflow_id,)
            )
            updated = await cur.fetchone()
            await cur.close()
            assert updated is not None

            step_rows = await self._step_rows(conn, workflow_id)
            steps = [_row_to_step(r) for r in step_rows]
            return _row_to_workflow(updated, steps)
