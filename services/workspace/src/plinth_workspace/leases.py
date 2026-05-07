# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 The Plinth Authors
"""Durable workflow executor: leases, workers, and the lease reaper.

A *lease* is a soft lock a worker acquires over a single ``workflow_step``
row before executing it. While the lease is ``running`` and its
``expires_at`` is in the future, no other worker may take the step. The
worker is expected to call :py:meth:`LeaseStore.heartbeat` periodically
to extend ``expires_at``; if it stops (because it crashed), the **lease
reaper** background task expires the lease and reverts the step back to
``pending`` so another worker can take over.

The semantics are deliberately race-safe:

* :py:meth:`LeaseStore.acquire_lease` only succeeds if the step row is
  currently ``pending`` AND there is no active lease (or an existing
  lease is already past its ``expires_at`` or ``released`` / ``expired``).
  The acquire path runs inside a single transaction with the step-status
  flip from ``pending → running`` so two workers cannot concurrently
  observe the step as available.
* :py:meth:`LeaseStore.release_lease` flips the lease to ``released``
  and the workflow step to its requested terminal status (or back to
  ``pending`` for retries) inside one transaction.
* :py:meth:`LeaseStore.expire_stale_leases` is the reaper hook — it
  finds running leases past their TTL, marks them ``expired``, and
  reverts the corresponding step back to ``pending``.

The reaper task is launched from the FastAPI lifespan so it runs only
inside the workspace service process. Tests can call the helpers
directly to avoid timing dependencies.
"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import aiosqlite
import structlog
from ulid import ULID

from .db import connect, iso, now_utc, parse_ts
from .exceptions import (
    InvalidArguments,
    PlinthError,
    WorkflowNotFound,
    WorkflowStepNotFound,
    WorkspaceNotFound,
)
from .models import Lease, Worker
from .workflows import WorkflowStore

log = structlog.get_logger("plinth_workspace.leases")


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class LeaseConflict(PlinthError):
    """Raised on a race-losing lease acquire (409)."""

    code = "LEASE_CONFLICT"
    status_code = 409
    message = "step is already leased by another worker"


class LeaseNotHeld(PlinthError):
    """Raised when a worker heartbeats/releases a lease it does not hold."""

    code = "LEASE_NOT_HELD"
    status_code = 409
    message = "no active lease held by this worker"


class WorkerNotFound(PlinthError):
    code = "WORKER_NOT_FOUND"
    status_code = 404
    message = "worker not found"

    def __init__(self, worker_id: str) -> None:
        super().__init__(
            f"Worker {worker_id} does not exist",
            details={"worker_id": worker_id},
        )


# ---------------------------------------------------------------------------
# IDs
# ---------------------------------------------------------------------------


def _new_worker_id() -> str:
    return f"worker_{ULID()}"


# ---------------------------------------------------------------------------
# Row helpers
# ---------------------------------------------------------------------------


def _row_to_lease(row: aiosqlite.Row) -> Lease:
    return Lease(
        step_id=row["step_id"],
        worker_id=row["worker_id"],
        acquired_at=parse_ts(row["acquired_at"]),  # type: ignore[arg-type]
        expires_at=parse_ts(row["expires_at"]),  # type: ignore[arg-type]
        heartbeat_at=parse_ts(row["heartbeat_at"]),  # type: ignore[arg-type]
        status=row["status"],
    )


def _row_to_worker(row: aiosqlite.Row) -> Worker:
    return Worker(
        id=row["id"],
        hostname=row["hostname"],
        pid=int(row["pid"]) if row["pid"] is not None else None,
        started_at=parse_ts(row["started_at"]),  # type: ignore[arg-type]
        last_heartbeat_at=parse_ts(row["last_heartbeat_at"]),  # type: ignore[arg-type]
        status=row["status"],
    )


# ---------------------------------------------------------------------------
# LeaseStore — leases + workers + reaper helpers
# ---------------------------------------------------------------------------


class LeaseStore:
    """Coordinates leases on workflow steps and worker registrations.

    Holds no in-memory state beyond ``db_path``; every method opens a
    fresh connection, mirroring the pattern in the rest of the workspace
    service.
    """

    def __init__(self, db_path: Path) -> None:
        self.db_path = db_path

    # ------------------------------------------------------------------
    # Worker lifecycle
    # ------------------------------------------------------------------

    async def register_worker(
        self,
        *,
        hostname: str | None = None,
        pid: int | None = None,
    ) -> Worker:
        worker_id = _new_worker_id()
        ts = now_utc()
        async with connect(self.db_path) as conn:
            await conn.execute(
                "INSERT INTO workers (id, hostname, pid, started_at, "
                "last_heartbeat_at, status) VALUES (?, ?, ?, ?, ?, 'active')",
                (worker_id, hostname, pid, iso(ts), iso(ts)),
            )
            await conn.commit()
        return Worker(
            id=worker_id,
            hostname=hostname,
            pid=pid,
            started_at=ts,
            last_heartbeat_at=ts,
            status="active",
        )

    async def heartbeat_worker(self, worker_id: str) -> Worker:
        ts = now_utc()
        async with connect(self.db_path) as conn:
            cur = await conn.execute(
                "SELECT * FROM workers WHERE id=?",
                (worker_id,),
            )
            row = await cur.fetchone()
            await cur.close()
            if row is None:
                raise WorkerNotFound(worker_id)
            # Drained workers stay drained — heartbeats just refresh the
            # last_heartbeat_at column (so the reaper doesn't sweep them
            # to ``gone`` while a graceful shutdown is in flight).
            await conn.execute(
                "UPDATE workers SET last_heartbeat_at=? WHERE id=?",
                (iso(ts), worker_id),
            )
            await conn.commit()

            cur = await conn.execute(
                "SELECT * FROM workers WHERE id=?",
                (worker_id,),
            )
            updated = await cur.fetchone()
            await cur.close()
            assert updated is not None
            return _row_to_worker(updated)

    async def drain_worker(self, worker_id: str) -> Worker:
        ts = now_utc()
        async with connect(self.db_path) as conn:
            cur = await conn.execute(
                "SELECT * FROM workers WHERE id=?",
                (worker_id,),
            )
            row = await cur.fetchone()
            await cur.close()
            if row is None:
                raise WorkerNotFound(worker_id)
            await conn.execute(
                "UPDATE workers SET status='draining', last_heartbeat_at=? "
                "WHERE id=?",
                (iso(ts), worker_id),
            )
            await conn.commit()

            cur = await conn.execute(
                "SELECT * FROM workers WHERE id=?",
                (worker_id,),
            )
            updated = await cur.fetchone()
            await cur.close()
            assert updated is not None
            return _row_to_worker(updated)

    async def list_workers(self, status: str | None = None) -> list[Worker]:
        async with connect(self.db_path) as conn:
            if status is None:
                cur = await conn.execute(
                    "SELECT * FROM workers ORDER BY started_at ASC"
                )
            else:
                cur = await conn.execute(
                    "SELECT * FROM workers WHERE status=? "
                    "ORDER BY started_at ASC",
                    (status,),
                )
            rows = await cur.fetchall()
            await cur.close()
            return [_row_to_worker(r) for r in rows]

    async def get_worker(self, worker_id: str) -> Worker:
        async with connect(self.db_path) as conn:
            cur = await conn.execute(
                "SELECT * FROM workers WHERE id=?",
                (worker_id,),
            )
            row = await cur.fetchone()
            await cur.close()
            if row is None:
                raise WorkerNotFound(worker_id)
            return _row_to_worker(row)

    # ------------------------------------------------------------------
    # Lease acquire / heartbeat / release
    # ------------------------------------------------------------------

    async def acquire_lease(
        self,
        workspace_id: str,
        workflow_id: str,
        step_id: str,
        *,
        worker_id: str,
        ttl_seconds: int = 60,
    ) -> Lease:
        """Atomically acquire a lease on a pending step.

        The acquire path is a single transaction:

        1. Ensure the workspace + workflow + step exist (404 otherwise).
        2. Ensure the step is currently ``pending``. ``running`` /
           ``completed`` / ``failed`` / ``cancelled`` cannot be leased.
        3. Inspect any existing lease on the step. If it's ``running`` and
           ``expires_at`` is in the future, raise :class:`LeaseConflict`.
           If it's ``released`` / ``expired`` / past its TTL, overwrite
           it via ``INSERT OR REPLACE``.
        4. Flip the step row from ``pending → running`` and record the
           lease.

        On success returns the freshly inserted :class:`Lease`.
        """

        if ttl_seconds <= 0:
            raise InvalidArguments("ttl_seconds must be > 0")

        async with connect(self.db_path) as conn:
            await WorkflowStore._assert_workspace(conn, workspace_id)

            wf_row = await WorkflowStore._workflow_row(
                conn, workspace_id, workflow_id
            )
            if wf_row is None:
                raise WorkflowNotFound(workflow_id)

            # Race-safe acquire: open an IMMEDIATE transaction so concurrent
            # acquires serialise on SQLite's database-level write lock. The
            # losing writers see SQLITE_BUSY and back off (we map to
            # LeaseConflict so callers see "someone else got it" rather than
            # an obscure DB error).
            try:
                await conn.execute("BEGIN IMMEDIATE")
            except aiosqlite.OperationalError as exc:
                if "locked" in str(exc).lower() or "busy" in str(exc).lower():
                    raise LeaseConflict(
                        f"Step {step_id} is being leased concurrently",
                        details={"step_id": step_id},
                    ) from exc
                raise

            try:
                cur = await conn.execute(
                    "SELECT * FROM workflow_steps WHERE id=? AND workflow_id=?",
                    (step_id, workflow_id),
                )
                step_row = await cur.fetchone()
                await cur.close()
                if step_row is None:
                    await conn.rollback()
                    raise WorkflowStepNotFound(workflow_id, step_id)

                # Step lifecycle gate: only ``pending`` steps may be leased.
                if step_row["status"] != "pending":
                    await conn.rollback()
                    raise LeaseConflict(
                        f"Step {step_id} is in status "
                        f"{step_row['status']!r}, not 'pending'",
                        details={
                            "step_id": step_id,
                            "step_status": step_row["status"],
                        },
                    )

                ts = now_utc()
                expires = ts + timedelta(seconds=ttl_seconds)

                # Existing lease check.
                cur = await conn.execute(
                    "SELECT * FROM workflow_step_leases WHERE step_id=?",
                    (step_id,),
                )
                lease_row = await cur.fetchone()
                await cur.close()
                if lease_row is not None:
                    lease_expires = parse_ts(lease_row["expires_at"])
                    # Active lease — refuse.
                    if (
                        lease_row["status"] == "running"
                        and lease_expires is not None
                        and lease_expires > ts
                    ):
                        await conn.rollback()
                        raise LeaseConflict(
                            f"Step {step_id} already leased by "
                            f"{lease_row['worker_id']}",
                            details={
                                "step_id": step_id,
                                "worker_id": lease_row["worker_id"],
                                "expires_at": lease_row["expires_at"],
                            },
                        )

                # Insert-or-replace: handles the "expired" / "released" /
                # "past TTL" cases uniformly.
                await conn.execute(
                    "INSERT OR REPLACE INTO workflow_step_leases "
                    "(step_id, worker_id, acquired_at, expires_at, "
                    " heartbeat_at, status) "
                    "VALUES (?, ?, ?, ?, ?, 'running')",
                    (
                        step_id,
                        worker_id,
                        iso(ts),
                        iso(expires),
                        iso(ts),
                    ),
                )

                # Flip the step from pending → running. We do this last so a
                # uniqueness violation on an unexpected race causes the step
                # row to remain unchanged.
                await conn.execute(
                    "UPDATE workflow_steps SET status='running', "
                    "started_at=COALESCE(started_at, ?) WHERE id=?",
                    (iso(ts), step_id),
                )

                # Bump workflow → running on first leased step.
                if wf_row["status"] == "pending":
                    await conn.execute(
                        "UPDATE workflows SET status='running', "
                        "started_at=COALESCE(started_at, ?) WHERE id=?",
                        (iso(ts), workflow_id),
                    )

                await conn.commit()
            except Exception:
                # Best-effort rollback on any error inside the txn.
                try:
                    await conn.rollback()
                except Exception:  # pragma: no cover - defensive
                    pass
                raise

        return Lease(
            step_id=step_id,
            worker_id=worker_id,
            acquired_at=ts,
            expires_at=expires,
            heartbeat_at=ts,
            status="running",
        )

    async def heartbeat_lease(
        self,
        workspace_id: str,
        workflow_id: str,
        step_id: str,
        *,
        worker_id: str,
        ttl_seconds: int | None = None,
    ) -> Lease:
        """Extend ``expires_at`` for a running lease.

        Only the worker that holds the lease may extend it (no transfers).
        ``ttl_seconds`` defaults to the original TTL (computed from
        ``expires_at - acquired_at``); callers may supply a fresh value.
        """

        async with connect(self.db_path) as conn:
            await WorkflowStore._assert_workspace(conn, workspace_id)

            wf_row = await WorkflowStore._workflow_row(
                conn, workspace_id, workflow_id
            )
            if wf_row is None:
                raise WorkflowNotFound(workflow_id)

            cur = await conn.execute(
                "SELECT * FROM workflow_step_leases WHERE step_id=?",
                (step_id,),
            )
            lease_row = await cur.fetchone()
            await cur.close()
            if lease_row is None:
                raise LeaseNotHeld(
                    f"No lease exists for step {step_id}",
                    details={"step_id": step_id},
                )
            if lease_row["worker_id"] != worker_id:
                raise LeaseNotHeld(
                    f"Lease for step {step_id} held by another worker",
                    details={
                        "step_id": step_id,
                        "actual_worker_id": lease_row["worker_id"],
                    },
                )
            if lease_row["status"] != "running":
                raise LeaseNotHeld(
                    f"Lease for step {step_id} is "
                    f"{lease_row['status']!r}, not 'running'",
                    details={
                        "step_id": step_id,
                        "status": lease_row["status"],
                    },
                )

            ts = now_utc()
            acquired = parse_ts(lease_row["acquired_at"])
            old_expires = parse_ts(lease_row["expires_at"])
            if ttl_seconds is None:
                # Preserve the original TTL.
                if acquired and old_expires:
                    ttl = max(int((old_expires - acquired).total_seconds()), 1)
                else:
                    ttl = 60
            else:
                ttl = int(ttl_seconds)
            new_expires = ts + timedelta(seconds=ttl)

            await conn.execute(
                "UPDATE workflow_step_leases SET expires_at=?, "
                "heartbeat_at=? WHERE step_id=?",
                (iso(new_expires), iso(ts), step_id),
            )
            await conn.commit()

        return Lease(
            step_id=step_id,
            worker_id=worker_id,
            acquired_at=acquired or ts,
            expires_at=new_expires,
            heartbeat_at=ts,
            status="running",
        )

    async def release_lease(
        self,
        workspace_id: str,
        workflow_id: str,
        step_id: str,
        *,
        worker_id: str,
        step_status: str = "completed",
        output: Any | None = None,
        error: str | None = None,
        snapshot_id: str | None = None,
    ) -> Lease:
        """Release the lease and update the step in one transaction.

        ``step_status`` may be ``completed`` / ``failed`` / ``cancelled``
        (terminal) or ``pending`` (re-queue for another worker).
        """

        if step_status not in {"completed", "failed", "cancelled", "pending"}:
            raise InvalidArguments(
                f"invalid step_status {step_status!r}; expected "
                "completed | failed | cancelled | pending"
            )

        async with connect(self.db_path) as conn:
            await WorkflowStore._assert_workspace(conn, workspace_id)

            wf_row = await WorkflowStore._workflow_row(
                conn, workspace_id, workflow_id
            )
            if wf_row is None:
                raise WorkflowNotFound(workflow_id)

            cur = await conn.execute(
                "SELECT * FROM workflow_step_leases WHERE step_id=?",
                (step_id,),
            )
            lease_row = await cur.fetchone()
            await cur.close()
            if lease_row is None:
                raise LeaseNotHeld(
                    f"No lease exists for step {step_id}",
                    details={"step_id": step_id},
                )
            if lease_row["worker_id"] != worker_id:
                raise LeaseNotHeld(
                    f"Lease for step {step_id} held by another worker",
                    details={
                        "step_id": step_id,
                        "actual_worker_id": lease_row["worker_id"],
                    },
                )

            cur = await conn.execute(
                "SELECT * FROM workflow_steps WHERE id=? AND workflow_id=?",
                (step_id, workflow_id),
            )
            step_row = await cur.fetchone()
            await cur.close()
            if step_row is None:
                raise WorkflowStepNotFound(workflow_id, step_id)

            ts = now_utc()
            terminal = step_status in {"completed", "failed", "cancelled"}
            new_finished = iso(ts) if terminal else None
            new_snapshot = (
                snapshot_id if snapshot_id is not None else step_row["snapshot_id"]
            )

            if step_status == "pending":
                # Re-queue: keep the snapshot/output blanks intact, but
                # rewind the started_at so we don't double-count time.
                await conn.execute(
                    "UPDATE workflow_steps SET status='pending', "
                    "started_at=NULL, error=? WHERE id=?",
                    (error, step_id),
                )
            else:
                await conn.execute(
                    "UPDATE workflow_steps SET status=?, output=?, "
                    "error=?, snapshot_id=?, "
                    "finished_at=COALESCE(?, finished_at) WHERE id=?",
                    (
                        step_status,
                        json.dumps(output) if output is not None else None,
                        error,
                        new_snapshot,
                        new_finished,
                        step_id,
                    ),
                )

            # Mark the lease released regardless of step status so the
            # row history accurately reflects "this worker stopped working
            # on this step".
            await conn.execute(
                "UPDATE workflow_step_leases SET status='released', "
                "heartbeat_at=? WHERE step_id=?",
                (iso(ts), step_id),
            )

            # Recompute workflow status using the existing helper.
            store = WorkflowStore(self.db_path)
            await store._maybe_update_workflow_status(
                conn, workflow_id, wf_row, ts
            )

            await conn.commit()

            cur = await conn.execute(
                "SELECT * FROM workflow_step_leases WHERE step_id=?",
                (step_id,),
            )
            updated = await cur.fetchone()
            await cur.close()
            assert updated is not None
            return _row_to_lease(updated)

    # ------------------------------------------------------------------
    # Pending / expired discovery
    # ------------------------------------------------------------------

    async def list_pending_steps(
        self,
        workspace_id: str,
        workflow_id: str,
    ) -> list[aiosqlite.Row]:
        """Return all pending step rows for a workflow.

        The store returns ``aiosqlite.Row`` objects rather than
        :class:`WorkflowStep` instances so the API layer can apply the
        same row-to-model translation it uses elsewhere; cleaner than
        importing a step parser here.
        """

        async with connect(self.db_path) as conn:
            await WorkflowStore._assert_workspace(conn, workspace_id)

            wf_row = await WorkflowStore._workflow_row(
                conn, workspace_id, workflow_id
            )
            if wf_row is None:
                raise WorkflowNotFound(workflow_id)

            cur = await conn.execute(
                "SELECT * FROM workflow_steps WHERE workflow_id=? "
                "AND status='pending' ORDER BY created_at ASC, id ASC",
                (workflow_id,),
            )
            rows = await cur.fetchall()
            await cur.close()
            return list(rows)

    async def list_expired_leases(
        self,
        workspace_id: str,
        workflow_id: str,
    ) -> list[Lease]:
        """Return leases past their expiry that still claim ``running``.

        Surfaces races where a worker's heartbeat has lapsed but the
        reaper hasn't run yet; an operator (or test) can use this to
        force a reclaim.
        """

        async with connect(self.db_path) as conn:
            await WorkflowStore._assert_workspace(conn, workspace_id)

            wf_row = await WorkflowStore._workflow_row(
                conn, workspace_id, workflow_id
            )
            if wf_row is None:
                raise WorkflowNotFound(workflow_id)

            ts = now_utc()
            cur = await conn.execute(
                "SELECT l.* FROM workflow_step_leases l "
                "JOIN workflow_steps s ON s.id = l.step_id "
                "WHERE s.workflow_id=? AND l.status='running' "
                "AND l.expires_at < ?",
                (workflow_id, iso(ts)),
            )
            rows = await cur.fetchall()
            await cur.close()
            return [_row_to_lease(r) for r in rows]

    # ------------------------------------------------------------------
    # Reaper helpers
    # ------------------------------------------------------------------

    async def expire_stale_leases(self, *, now: datetime | None = None) -> int:
        """Sweep stale leases.

        For every ``running`` lease whose ``expires_at`` is past ``now``:

        1. Flip the lease to ``expired``.
        2. If the corresponding step is still ``running``, revert it to
           ``pending`` (so another worker can pick it up). Steps that
           have moved on (``completed`` / ``failed`` / ``cancelled``)
           are left alone — those are races where the worker released
           the lease just before the reaper ran.

        Returns the number of leases flipped.
        """

        ts = now or now_utc()
        async with connect(self.db_path) as conn:
            cur = await conn.execute(
                "SELECT * FROM workflow_step_leases "
                "WHERE status='running' AND expires_at < ?",
                (iso(ts),),
            )
            rows = await cur.fetchall()
            await cur.close()
            count = 0
            for row in rows:
                step_id = row["step_id"]
                await conn.execute(
                    "UPDATE workflow_step_leases SET status='expired' "
                    "WHERE step_id=?",
                    (step_id,),
                )
                # Revert the step iff it's still running. We look it up
                # in the same transaction so a concurrent "release just
                # happened" race resolves naturally — we observe the new
                # status and leave it alone.
                await conn.execute(
                    "UPDATE workflow_steps SET status='pending', "
                    "started_at=NULL "
                    "WHERE id=? AND status='running'",
                    (step_id,),
                )
                count += 1
            if count:
                await conn.commit()
            return count

    async def mark_inactive_workers(
        self,
        *,
        timeout_seconds: int,
        now: datetime | None = None,
    ) -> int:
        """Mark workers ``gone`` if they haven't heartbeat in ``timeout``."""

        ts = now or now_utc()
        threshold = ts - timedelta(seconds=timeout_seconds)
        async with connect(self.db_path) as conn:
            cur = await conn.execute(
                "UPDATE workers SET status='gone' "
                "WHERE status='active' AND last_heartbeat_at < ?",
                (iso(threshold),),
            )
            await conn.commit()
            return cur.rowcount or 0


# ---------------------------------------------------------------------------
# Lease reaper background task
# ---------------------------------------------------------------------------


async def lease_reaper_loop(
    store: LeaseStore,
    *,
    interval_seconds: float,
    inactive_timeout_seconds: int,
    stop_event: asyncio.Event,
) -> None:
    """Run the lease reaper until ``stop_event`` is set.

    Sweeps stale leases AND inactive workers on every tick. Errors are
    swallowed and logged so the reaper can't crash the workspace
    process; a worker re-attempting to lease a step it already lost
    will see ``LeaseConflict`` and back off.
    """

    while not stop_event.is_set():
        try:
            expired = await store.expire_stale_leases()
            if expired:
                log.info("workspace.lease_reaper.expired", count=expired)
            inactive = await store.mark_inactive_workers(
                timeout_seconds=inactive_timeout_seconds,
            )
            if inactive:
                log.info("workspace.lease_reaper.workers_gone", count=inactive)
        except Exception as exc:  # noqa: BLE001
            log.warning("workspace.lease_reaper.error", error=str(exc))

        # ``wait_for`` returns False on timeout — that's our pacing knob;
        # ``True`` means stop_event fired.
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=interval_seconds)
            return
        except asyncio.TimeoutError:
            continue


__all__ = [
    "LeaseConflict",
    "LeaseNotHeld",
    "LeaseStore",
    "WorkerNotFound",
    "lease_reaper_loop",
]
