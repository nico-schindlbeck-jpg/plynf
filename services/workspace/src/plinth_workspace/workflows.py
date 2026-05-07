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
from pathlib import Path
from typing import Any

import aiosqlite
from ulid import ULID

from .db import connect, iso, now_utc, parse_ts
from .exceptions import (
    InvalidArguments,
    InvalidWorkflowStep,
    WorkflowNotFound,
    WorkflowStepNotFound,
    WorkspaceNotFound,
)
from .models import ResumeInfo, Workflow, WorkflowStep

TERMINAL_STEP_STATUSES = {"completed", "failed", "cancelled"}


def _new_workflow_id() -> str:
    return f"wf_{ULID()}"


def _new_step_id() -> str:
    return f"step_{ULID()}"


def _row_to_step(row: aiosqlite.Row) -> WorkflowStep:
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
    ) -> WorkflowStep:
        """Create a new step row.

        ``initial_status`` defaults to ``running`` (the v0.2 in-process
        flow where the agent starts work immediately). Pass ``pending``
        to create a step in the durable-executor pool that workers will
        pick up via :py:meth:`LeaseStore.acquire_lease`.
        """

        if initial_status not in {"running", "pending"}:
            raise InvalidArguments(
                f"initial_status must be 'running' or 'pending', "
                f"got {initial_status!r}"
            )

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
                " finished_at, input, output, error, snapshot_id, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, NULL, ?, NULL, NULL, ?, ?)",
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
    ) -> WorkflowStep:
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
            terminal = status in TERMINAL_STEP_STATUSES
            new_finished = iso(ts) if terminal else None

            # Preserve existing snapshot_id if caller didn't pass one.
            new_snapshot = (
                snapshot_id if snapshot_id is not None else step_row["snapshot_id"]
            )

            await conn.execute(
                "UPDATE workflow_steps SET status=?, output=?, error=?, "
                " snapshot_id=?, finished_at=COALESCE(?, finished_at) "
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
