# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 The Plinth Authors
"""Generic resource-lock primitive for the workspace service (v0.6).

These locks are intentionally distinct from the v0.5 workflow-step
:class:`~plinth_workspace.leases.Lease` primitive. A workflow lease is
*coupled* to a workflow step row: acquiring a lease flips the step row's
status and is bound to the durable executor. A *resource lock* is just a
named soft-lock on an opaque resource — useful for protecting an Agent A
vs Agent B race over a KV key, file path, or external resource handle
without forcing those operations through the workflow executor.

Lifecycle:

* :py:meth:`ResourceLockStore.acquire` performs a race-safe upsert.
  If a row exists for ``(workspace_id, name)`` and is not yet expired
  it raises :class:`~plinth_workspace.exceptions.LockHeld`. If
  ``wait_ms > 0`` we poll every 100 ms until the budget elapses.
* :py:meth:`ResourceLockStore.heartbeat` extends the row's
  ``expires_at`` only if the request's ``holder`` matches the row's
  current holder.
* :py:meth:`ResourceLockStore.release` deletes the row only if the
  caller is the current holder. Always idempotent — a no-op for an
  already-released or never-existed lock.
* :py:meth:`ResourceLockStore.expire_stale_locks` is the reaper hook
  used by :mod:`leases.lease_reaper_loop` so a holder that crashes
  mid-lease doesn't deadlock subsequent acquirers indefinitely.

The acquire path runs inside a ``BEGIN IMMEDIATE`` transaction so two
concurrent acquires serialise on SQLite's write lock. The losing writer
either sees ``SQLITE_BUSY`` (mapped to :class:`LockHeld`) or observes the
existing row and raises :class:`LockHeld` directly. Tests verify exactly
one of N concurrent callers wins.
"""

from __future__ import annotations

import asyncio
import math
from datetime import datetime, timedelta
from pathlib import Path

import aiosqlite
import structlog

from .coordination import CoordinationBackend, MemoryBackend
from .db import connect, iso, now_utc, parse_ts
from .exceptions import (
    LockHeld,
    LockNotFound,
    LockNotHeld,
    WorkspaceNotFound,
)
from .models import Lock

log = structlog.get_logger("plinth_workspace.resource_locks")


# Polling cadence for ``wait_ms``-based acquires. 100 ms matches the
# documented contract — small enough that callers see freshly released
# locks quickly, large enough that we don't hammer SQLite.
_POLL_INTERVAL_SECONDS = 0.1


# ---------------------------------------------------------------------------
# Row helpers
# ---------------------------------------------------------------------------


def _row_to_lock(row: aiosqlite.Row) -> Lock:
    return Lock(
        name=row["name"],
        workspace_id=row["workspace_id"],
        holder=row["holder"],
        acquired_at=parse_ts(row["acquired_at"]),  # type: ignore[arg-type]
        expires_at=parse_ts(row["expires_at"]),  # type: ignore[arg-type]
        heartbeat_at=parse_ts(row["heartbeat_at"]),  # type: ignore[arg-type]
        waiters=0,
    )


# ---------------------------------------------------------------------------
# Store
# ---------------------------------------------------------------------------


class ResourceLockStore:
    """CRUD + acquire/heartbeat/release for generic resource locks.

    Holds no in-memory state beyond ``db_path``. Mirrors the rest of the
    workspace service: every method opens a fresh connection scoped to
    the request.

    v1.3 — accepts an optional :class:`CoordinationBackend`. When the
    backend is non-memory (typically Redis), the acquire path takes a
    cluster-shared distributed lock first so multiple workspace replicas
    cannot all win the race for the same resource. ``MemoryBackend`` /
    ``None`` short-circuit the cluster gate to preserve v1.2 behaviour.
    """

    def __init__(
        self,
        db_path: Path,
        *,
        coordination: CoordinationBackend | None = None,
        coordination_prefix: str = "plinth:workspace:resource_lock",
    ) -> None:
        self.db_path = db_path
        self.coordination = coordination
        self.coordination_prefix = coordination_prefix.rstrip(":")

    # ------------------------------------------------------------------
    # Coordination helpers
    # ------------------------------------------------------------------

    def _cluster_enabled(self) -> bool:
        """Return ``True`` when the cluster gate should run."""

        if self.coordination is None:
            return False
        return not isinstance(self.coordination, MemoryBackend)

    def _cluster_lock_key(self, workspace_id: str, name: str) -> str:
        return f"{self.coordination_prefix}:{workspace_id}:{name}"

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

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

    # ------------------------------------------------------------------
    # Acquire
    # ------------------------------------------------------------------

    async def acquire(
        self,
        workspace_id: str,
        name: str,
        *,
        holder: str,
        ttl_seconds: int,
        wait_ms: int = 0,
    ) -> Lock:
        """Acquire (or steal an expired) lock on ``(workspace_id, name)``.

        On success returns the persisted :class:`Lock`. On contention
        either raises :class:`LockHeld` immediately (when ``wait_ms == 0``)
        or polls for ``wait_ms`` milliseconds before raising.

        The acquire path is race-safe: two callers attempting to acquire
        the same name simultaneously will see exactly one winner — the
        loser observes the freshly-inserted row inside a ``BEGIN IMMEDIATE``
        transaction and raises :class:`LockHeld`.
        """

        # Validate inputs early.
        if ttl_seconds <= 0:
            raise ValueError("ttl_seconds must be positive")
        if wait_ms < 0:
            raise ValueError("wait_ms must be non-negative")

        deadline = now_utc() + timedelta(milliseconds=wait_ms) if wait_ms > 0 else None

        # Workspace existence is checked once before any polling so we
        # don't loop on a 404.
        async with connect(self.db_path) as conn:
            await self._assert_workspace(conn, workspace_id)

        # First acquire attempt.
        try:
            return await self._try_acquire_once(
                workspace_id, name, holder=holder, ttl_seconds=ttl_seconds
            )
        except LockHeld as initial_exc:
            if wait_ms == 0:
                raise
            last_exc: LockHeld = initial_exc

        # Poll until the budget elapses.
        while True:
            await asyncio.sleep(_POLL_INTERVAL_SECONDS)
            try:
                return await self._try_acquire_once(
                    workspace_id, name, holder=holder, ttl_seconds=ttl_seconds
                )
            except LockHeld as exc:
                last_exc = exc
                if deadline is not None and now_utc() >= deadline:
                    raise last_exc from None

    async def _try_acquire_once(
        self,
        workspace_id: str,
        name: str,
        *,
        holder: str,
        ttl_seconds: int,
    ) -> Lock:
        """Single race-safe upsert attempt.

        Wraps the acquire in ``BEGIN IMMEDIATE`` so concurrent attempts
        serialise on SQLite's database-level write lock. The losing
        attempt either observes the winner's row directly (raising
        :class:`LockHeld`) or maps ``SQLITE_BUSY`` to the same exception
        so callers see a uniform error shape.

        v1.3 — cluster-gates the acquire when a non-memory coordination
        backend is configured, so that multiple replicas race-coordinate
        through Redis before touching the per-replica DB.
        """

        # v1.3 — cluster gate. Skip for ``MemoryBackend`` / ``None``.
        cluster_acquired = False
        cluster_key: str | None = None
        if self._cluster_enabled():
            assert self.coordination is not None
            cluster_key = self._cluster_lock_key(workspace_id, name)
            cluster_acquired = await self.coordination.acquire_lock(
                cluster_key,
                holder=holder,
                ttl_seconds=ttl_seconds,
            )
            if not cluster_acquired:
                raise LockHeld(
                    workspace_id,
                    name,
                    retry_after_seconds=1,
                )

        try:
            return await self._try_acquire_once_local(
                workspace_id,
                name,
                holder=holder,
                ttl_seconds=ttl_seconds,
            )
        except Exception:
            if cluster_acquired and cluster_key is not None:
                assert self.coordination is not None
                try:
                    await self.coordination.release_lock(
                        cluster_key, holder=holder
                    )
                except Exception as exc:  # noqa: BLE001 - best effort
                    log.warning(
                        "workspace.resource_lock.cluster_release_failed",
                        cluster_key=cluster_key,
                        error=str(exc),
                    )
            raise

    async def _try_acquire_once_local(
        self,
        workspace_id: str,
        name: str,
        *,
        holder: str,
        ttl_seconds: int,
    ) -> Lock:
        """Original v1.2 single race-safe upsert path."""

        async with connect(self.db_path) as conn:
            try:
                await conn.execute("BEGIN IMMEDIATE")
            except aiosqlite.OperationalError as exc:
                msg = str(exc).lower()
                if "locked" in msg or "busy" in msg:
                    raise LockHeld(
                        workspace_id,
                        name,
                        retry_after_seconds=1,
                    ) from exc
                raise

            try:
                ts = now_utc()
                expires = ts + timedelta(seconds=ttl_seconds)

                cur = await conn.execute(
                    "SELECT * FROM resource_locks WHERE workspace_id=? AND name=?",
                    (workspace_id, name),
                )
                existing = await cur.fetchone()
                await cur.close()

                if existing is not None:
                    existing_expires = parse_ts(existing["expires_at"])
                    if existing_expires is not None and existing_expires > ts:
                        # Active lock — refuse, regardless of holder.
                        # (Re-acquire by current holder still fails with
                        # 409 by design; use heartbeat() to extend a held
                        # lock.)
                        await conn.rollback()
                        retry_after = max(
                            1,
                            int(
                                math.ceil(
                                    (existing_expires - ts).total_seconds()
                                )
                            ),
                        )
                        raise LockHeld(
                            workspace_id,
                            name,
                            current_holder=existing["holder"],
                            retry_after_seconds=retry_after,
                            expires_at=existing["expires_at"],
                        )

                # No row, or row exists and is past its expiry — upsert.
                # Use INSERT … ON CONFLICT to make the operation atomic
                # against the rare interleaving where another caller
                # creates the row between our SELECT and the write.
                await conn.execute(
                    """
                    INSERT INTO resource_locks
                        (workspace_id, name, holder, acquired_at,
                         expires_at, heartbeat_at)
                    VALUES (?, ?, ?, ?, ?, ?)
                    ON CONFLICT(workspace_id, name) DO UPDATE SET
                        holder       = excluded.holder,
                        acquired_at  = excluded.acquired_at,
                        expires_at   = excluded.expires_at,
                        heartbeat_at = excluded.heartbeat_at
                    WHERE resource_locks.expires_at < excluded.acquired_at
                    """,
                    (
                        workspace_id,
                        name,
                        holder,
                        iso(ts),
                        iso(expires),
                        iso(ts),
                    ),
                )

                # Read back the row. If the upsert was a no-op (because
                # someone else won), we'll see their winning row instead
                # of ours — detect by comparing the holder we just sent.
                cur = await conn.execute(
                    "SELECT * FROM resource_locks WHERE workspace_id=? AND name=?",
                    (workspace_id, name),
                )
                row = await cur.fetchone()
                await cur.close()

                if row is None:
                    # Should not happen — we just inserted. Defensive.
                    await conn.rollback()
                    raise LockHeld(workspace_id, name)  # pragma: no cover

                if row["holder"] != holder or row["acquired_at"] != iso(ts):
                    # Another caller won — surface their row as the
                    # current holder.
                    await conn.rollback()
                    row_expires = parse_ts(row["expires_at"])
                    retry_after = (
                        max(
                            1,
                            int(
                                math.ceil(
                                    (row_expires - ts).total_seconds()
                                )
                            ),
                        )
                        if row_expires is not None
                        else 1
                    )
                    raise LockHeld(
                        workspace_id,
                        name,
                        current_holder=row["holder"],
                        retry_after_seconds=retry_after,
                        expires_at=row["expires_at"],
                    )

                await conn.commit()
                return _row_to_lock(row)
            except LockHeld:
                raise
            except Exception:
                try:
                    await conn.rollback()
                except Exception:  # pragma: no cover - defensive
                    pass
                raise

    # ------------------------------------------------------------------
    # Heartbeat
    # ------------------------------------------------------------------

    async def heartbeat(
        self,
        workspace_id: str,
        name: str,
        *,
        holder: str,
        ttl_seconds: int | None = None,
    ) -> Lock:
        """Extend ``expires_at`` on a held lock.

        Only the current holder may extend. Raises:
        - :class:`LockNotFound` if no row exists,
        - :class:`LockNotHeld` if the row's holder differs.

        Note that a heartbeat from the *current* holder succeeds even if
        the row's TTL has elapsed but the reaper hasn't yet swept it —
        the holder still owns the row. A second caller that races a
        steal-via-acquire wins on first-writer-wins, after which the
        original holder's heartbeat will fail with :class:`LockNotHeld`.
        """

        async with connect(self.db_path) as conn:
            await self._assert_workspace(conn, workspace_id)

            cur = await conn.execute(
                "SELECT * FROM resource_locks WHERE workspace_id=? AND name=?",
                (workspace_id, name),
            )
            row = await cur.fetchone()
            await cur.close()

            if row is None:
                raise LockNotFound(workspace_id, name)
            if row["holder"] != holder:
                raise LockNotHeld(
                    workspace_id,
                    name,
                    holder=holder,
                    actual_holder=row["holder"],
                )

            ts = now_utc()
            old_acquired = parse_ts(row["acquired_at"])
            old_expires = parse_ts(row["expires_at"])
            if ttl_seconds is None:
                if old_acquired and old_expires:
                    ttl = max(int((old_expires - old_acquired).total_seconds()), 1)
                else:
                    ttl = 60
            else:
                ttl = int(ttl_seconds)
            new_expires = ts + timedelta(seconds=ttl)

            await conn.execute(
                "UPDATE resource_locks SET expires_at=?, heartbeat_at=? "
                "WHERE workspace_id=? AND name=? AND holder=?",
                (iso(new_expires), iso(ts), workspace_id, name, holder),
            )
            await conn.commit()

            cur = await conn.execute(
                "SELECT * FROM resource_locks WHERE workspace_id=? AND name=?",
                (workspace_id, name),
            )
            updated = await cur.fetchone()
            await cur.close()
            assert updated is not None

        # v1.3 — refresh the cluster lock TTL alongside the local row.
        if self._cluster_enabled():
            assert self.coordination is not None
            cluster_key = self._cluster_lock_key(workspace_id, name)
            try:
                await self.coordination.acquire_lock(
                    cluster_key,
                    holder=holder,
                    ttl_seconds=ttl,
                )
            except Exception as exc:  # noqa: BLE001 - best effort
                log.warning(
                    "workspace.resource_lock.cluster_heartbeat_failed",
                    cluster_key=cluster_key,
                    error=str(exc),
                )

        return _row_to_lock(updated)

    # ------------------------------------------------------------------
    # Release
    # ------------------------------------------------------------------

    async def release(
        self,
        workspace_id: str,
        name: str,
        *,
        holder: str,
    ) -> None:
        """Release a held lock (idempotent).

        Always succeeds — releasing a lock you don't hold (or one that
        was already swept by the reaper, or one that never existed) is a
        silent no-op. Ensures cleanup paths in caller code don't have to
        special-case the "lost the race" outcome.

        Workspace 404s still raise :class:`WorkspaceNotFound` so the SDK
        sees a clear error if the workspace itself was deleted.
        """

        async with connect(self.db_path) as conn:
            await self._assert_workspace(conn, workspace_id)

            await conn.execute(
                "DELETE FROM resource_locks "
                "WHERE workspace_id=? AND name=? AND holder=?",
                (workspace_id, name, holder),
            )
            await conn.commit()

        # v1.3 — drop the cluster-shared lock so a different replica can
        # acquire the next lease on this resource. Best-effort; the lock
        # has its own TTL as a safety net.
        if self._cluster_enabled():
            assert self.coordination is not None
            cluster_key = self._cluster_lock_key(workspace_id, name)
            try:
                await self.coordination.release_lock(
                    cluster_key, holder=holder
                )
            except Exception as exc:  # noqa: BLE001 - best effort
                log.warning(
                    "workspace.resource_lock.cluster_release_failed",
                    cluster_key=cluster_key,
                    error=str(exc),
                )

    # ------------------------------------------------------------------
    # Read
    # ------------------------------------------------------------------

    async def get(
        self,
        workspace_id: str,
        name: str,
    ) -> Lock | None:
        """Return the lock row, or ``None`` if no row exists.

        Does *not* honour expiry — an expired-but-unswept lock still
        returns its persisted row so operators can see what's pending in
        the reaper queue. The :class:`Lock.expires_at` field tells you
        whether the row is still authoritative.
        """

        async with connect(self.db_path) as conn:
            await self._assert_workspace(conn, workspace_id)

            cur = await conn.execute(
                "SELECT * FROM resource_locks WHERE workspace_id=? AND name=?",
                (workspace_id, name),
            )
            row = await cur.fetchone()
            await cur.close()
            return _row_to_lock(row) if row is not None else None

    async def list_locks(self, workspace_id: str) -> list[Lock]:
        """List every lock currently persisted for the workspace."""

        async with connect(self.db_path) as conn:
            await self._assert_workspace(conn, workspace_id)

            cur = await conn.execute(
                "SELECT * FROM resource_locks WHERE workspace_id=? "
                "ORDER BY name ASC",
                (workspace_id,),
            )
            rows = await cur.fetchall()
            await cur.close()
            return [_row_to_lock(r) for r in rows]

    # ------------------------------------------------------------------
    # Reaper hook
    # ------------------------------------------------------------------

    async def expire_stale_locks(self, *, now: datetime | None = None) -> int:
        """Sweep expired lock rows.

        Returns the number of rows deleted. Used by
        :func:`leases.lease_reaper_loop` so a single background task
        keeps both the workflow-step lease table and the resource-lock
        table tidy.

        v1.3 — also drops the cluster-shared lock for each swept row
        when a non-memory coordination backend is configured. Cluster
        locks expire naturally on their TTL anyway; the explicit release
        just lets pending acquirers see availability immediately.
        """

        ts = now or now_utc()
        async with connect(self.db_path) as conn:
            stale: list[tuple[str, str, str]] = []
            if self._cluster_enabled():
                # Snapshot the rows we're about to delete so we can release
                # their cluster locks afterwards.
                cur = await conn.execute(
                    "SELECT workspace_id, name, holder FROM resource_locks "
                    "WHERE expires_at < ?",
                    (iso(ts),),
                )
                rows = await cur.fetchall()
                await cur.close()
                stale = [
                    (r["workspace_id"], r["name"], r["holder"]) for r in rows
                ]
            cur = await conn.execute(
                "DELETE FROM resource_locks WHERE expires_at < ?",
                (iso(ts),),
            )
            count = cur.rowcount or 0
            await cur.close()
            if count:
                await conn.commit()

        if self._cluster_enabled() and stale:
            assert self.coordination is not None
            for workspace_id, name, holder in stale:
                cluster_key = self._cluster_lock_key(workspace_id, name)
                try:
                    await self.coordination.release_lock(
                        cluster_key, holder=holder
                    )
                except Exception as exc:  # noqa: BLE001 - best effort
                    log.warning(
                        "workspace.resource_lock.cluster_reap_release_failed",
                        cluster_key=cluster_key,
                        error=str(exc),
                    )
        return count


__all__ = [
    "ResourceLockStore",
]
