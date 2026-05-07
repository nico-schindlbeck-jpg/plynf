# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 The Plinth Authors
"""Workspace garbage collection (GC) engine + retention CRUD.

A workspace accumulates immutable versions over time (every PUT or DELETE
on KV / files appends a row). Without trimming, query plans degrade and
disk grows linearly. The GC engine implements three retention dimensions:

1. ``keep_versions = N``   — keep the latest *N* per (key|path, branch).
2. ``keep_days = D``       — keep versions newer than *D* days.
3. ``keep_snapshots = N``  — keep the latest *N* snapshots (older ones
   are eligible for deletion provided they're not the head of an active
   branch).

GC is **safe by construction**: any version listed in any non-deleted
snapshot is preserved unconditionally. The "most permissive of the
active rules wins" semantics matches the spec: if either rule says
"keep", we keep.

Locking
-------
Each pass takes a per-workspace advisory lock so concurrent calls don't
corrupt the working set. SQLite uses an in-process ``asyncio.Lock``
(one process always owns the SQLite file). Postgres uses
``pg_try_advisory_lock(hashtext(ws_id))``.

Errors
------
- :class:`GCInProgress` — another GC pass holds the lock for this
  workspace. The HTTP layer maps this to 409.
- :class:`WorkspaceNotFound` — the workspace doesn't exist (or is in a
  different tenant) — the API layer raises this before we ever start GC.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Iterable
from datetime import timedelta
from pathlib import Path
from typing import Any

import aiosqlite

from .db import connect, iso, now_utc, parse_ts
from .exceptions import PlinthError
from .models import GCResult, RetentionPolicy


# ---------------------------------------------------------------------------
# Errors


class GCInProgress(PlinthError):
    code = "GC_IN_PROGRESS"
    status_code = 409
    message = "Garbage collection is already running for this workspace"

    def __init__(self, workspace_id: str) -> None:
        super().__init__(
            f"GC is already running for workspace {workspace_id}",
            details={"workspace_id": workspace_id},
        )


# ---------------------------------------------------------------------------
# Retention policy CRUD (a thin store, kept here next to the GC engine that
# reads them)


def _row_to_policy(row: aiosqlite.Row) -> RetentionPolicy:
    updated_at = parse_ts(row["updated_at"])
    assert updated_at is not None  # noqa: S101
    return RetentionPolicy(
        workspace_id=row["workspace_id"],
        keep_versions=row["keep_versions"],
        keep_days=row["keep_days"],
        keep_snapshots=row["keep_snapshots"],
        delete_unreferenced_blobs=bool(row["delete_unreferenced_blobs"]),
        updated_at=updated_at,
    )


def _default_policy(workspace_id: str) -> RetentionPolicy:
    """Return the implicit policy for a workspace that has no row.

    Defaults are "infinite retention, blobs cleaned" — which is what an
    operator probably means by "retention not configured": don't trim
    versions, but do reclaim blobs we'd otherwise leak.
    """

    return RetentionPolicy(
        workspace_id=workspace_id,
        keep_versions=None,
        keep_days=None,
        keep_snapshots=None,
        delete_unreferenced_blobs=True,
        updated_at=now_utc(),
    )


class RetentionStore:
    """CRUD for ``retention_policies`` rows."""

    def __init__(self, db_path: Path) -> None:
        self._db_path = db_path

    @property
    def db_path(self) -> Path:
        return self._db_path

    async def get(self, workspace_id: str) -> RetentionPolicy:
        """Return the saved policy or the implicit default."""

        async with connect(self._db_path) as conn:
            cur = await conn.execute(
                "SELECT * FROM retention_policies WHERE workspace_id=?",
                (workspace_id,),
            )
            row = await cur.fetchone()
            await cur.close()
        if row is None:
            return _default_policy(workspace_id)
        return _row_to_policy(row)

    async def upsert(
        self,
        workspace_id: str,
        *,
        keep_versions: int | None,
        keep_days: int | None,
        keep_snapshots: int | None,
        delete_unreferenced_blobs: bool,
    ) -> RetentionPolicy:
        """Insert-or-update a policy row."""

        ts = now_utc()
        async with connect(self._db_path) as conn:
            await conn.execute(
                "INSERT INTO retention_policies "
                "(workspace_id, keep_versions, keep_days, keep_snapshots, "
                " delete_unreferenced_blobs, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(workspace_id) DO UPDATE SET "
                "  keep_versions=excluded.keep_versions, "
                "  keep_days=excluded.keep_days, "
                "  keep_snapshots=excluded.keep_snapshots, "
                "  delete_unreferenced_blobs=excluded.delete_unreferenced_blobs, "
                "  updated_at=excluded.updated_at",
                (
                    workspace_id,
                    keep_versions,
                    keep_days,
                    keep_snapshots,
                    int(bool(delete_unreferenced_blobs)),
                    iso(ts),
                ),
            )
            await conn.commit()
        return RetentionPolicy(
            workspace_id=workspace_id,
            keep_versions=keep_versions,
            keep_days=keep_days,
            keep_snapshots=keep_snapshots,
            delete_unreferenced_blobs=delete_unreferenced_blobs,
            updated_at=ts,
        )

    async def workspaces_with_policies(self) -> list[str]:
        """List workspace IDs that have an explicit policy row."""

        async with connect(self._db_path) as conn:
            cur = await conn.execute(
                "SELECT workspace_id FROM retention_policies ORDER BY workspace_id"
            )
            rows = await cur.fetchall()
            await cur.close()
        return [r["workspace_id"] for r in rows]


# ---------------------------------------------------------------------------
# GC engine


class GCEngine:
    """Run a single GC pass over one workspace.

    Construction is cheap; one engine can run many passes. Per-workspace
    advisory locking keeps concurrent passes from trampling each other.
    """

    def __init__(self, db_path: Path, blobs_dir: Path) -> None:
        self.db_path = db_path
        self.blobs_dir = blobs_dir
        # In-process per-workspace lock map. SQLite is single-writer per
        # process, so this is sufficient for v0.4 scale. Postgres callers
        # should layer ``pg_try_advisory_lock`` on top via the storage
        # driver if they need cross-process safety.
        self._locks: dict[str, asyncio.Lock] = {}
        # Track the set of currently held workspace ids (so we can return a
        # 409 instead of blocking on a held lock).
        self._held: set[str] = set()

    async def run(
        self,
        workspace_id: str,
        policy: RetentionPolicy,
    ) -> GCResult:
        """Run GC under the given policy. Caller must check ws existence first."""

        lock = self._locks.setdefault(workspace_id, asyncio.Lock())
        if lock.locked():
            raise GCInProgress(workspace_id)
        await lock.acquire()
        self._held.add(workspace_id)
        try:
            return await self._run_locked(workspace_id, policy)
        finally:
            self._held.discard(workspace_id)
            lock.release()

    # ------------------------------------------------------------------ core

    async def _run_locked(
        self,
        workspace_id: str,
        policy: RetentionPolicy,
    ) -> GCResult:
        started_at = now_utc()
        result = GCResult(
            workspace_id=workspace_id,
            started_at=started_at,
            finished_at=started_at,
            duration_ms=0,
        )

        async with connect(self.db_path) as conn:
            # 1) Build the "protected" sets up-front.
            referenced_kv = await self._referenced_kv(conn, workspace_id)
            referenced_files = await self._referenced_files(conn, workspace_id)
            active_branch_snapshots = await self._active_branch_snapshots(
                conn, workspace_id
            )

            # 2) Trim KV versions.
            kv_deleted = await self._gc_kv(conn, workspace_id, policy, referenced_kv)
            result.kv_versions_deleted = kv_deleted

            # 3) Trim file versions (also returns blob hashes that lost a
            #    reference — used in step 4).
            file_deleted, freed_blob_refs = await self._gc_files(
                conn, workspace_id, policy, referenced_files
            )
            result.file_versions_deleted = file_deleted

            # 4) Snapshots: drop oldest beyond ``keep_snapshots`` unless
            #    they're the parent of an active branch.
            snap_deleted = await self._gc_snapshots(
                conn, workspace_id, policy, active_branch_snapshots
            )
            result.snapshots_deleted = snap_deleted

            # 5) Branches: merged + older-than-keep_days are deleted.
            br_deleted = await self._gc_branches(conn, workspace_id, policy)
            result.branches_deleted = br_deleted

            await conn.commit()

            # 6) Blob cleanup runs *after* the DB pass committed so we
            #    don't unlink a file we still need on rollback.
            if policy.delete_unreferenced_blobs:
                blobs_deleted, bytes_freed = await self._gc_blobs(
                    conn, workspace_id, freed_blob_refs
                )
                result.blob_files_deleted = blobs_deleted
                result.bytes_freed = bytes_freed

        finished_at = now_utc()
        result.finished_at = finished_at
        result.duration_ms = int(
            (finished_at - started_at).total_seconds() * 1000
        )
        return result

    # ------------------------------------------------------------------ kv

    async def _gc_kv(
        self,
        conn: aiosqlite.Connection,
        workspace_id: str,
        policy: RetentionPolicy,
        referenced: set[tuple[str, int]],
    ) -> int:
        """Trim KV versions per policy. Returns the number of rows deleted."""

        # If no policy active, leave KV alone.
        if policy.keep_versions is None and policy.keep_days is None:
            return 0

        cutoff = (
            now_utc() - timedelta(days=policy.keep_days)
            if policy.keep_days is not None
            else None
        )

        cur = await conn.execute(
            "SELECT id, key, version, branch_id, created_at "
            "FROM kv_entries WHERE workspace_id=?",
            (workspace_id,),
        )
        rows = await cur.fetchall()
        await cur.close()

        # Group rows by (key, branch_id), sorted by version DESC.
        groups: dict[tuple[str, str | None], list[aiosqlite.Row]] = {}
        for row in rows:
            groups.setdefault((row["key"], row["branch_id"]), []).append(row)
        for entries in groups.values():
            entries.sort(key=lambda r: r["version"], reverse=True)

        ids_to_delete: list[int] = []
        for entries in groups.values():
            for idx, row in enumerate(entries):
                # Always keep if referenced by a snapshot.
                if (row["key"], row["version"]) in referenced:
                    continue
                # ``keep_versions`` keeps the top-N rows of this group.
                if policy.keep_versions is not None and idx < policy.keep_versions:
                    continue
                # ``keep_days`` keeps any row created within the window.
                if cutoff is not None:
                    created = parse_ts(row["created_at"])
                    if created is not None and created >= cutoff:
                        continue
                ids_to_delete.append(int(row["id"]))

        if not ids_to_delete:
            return 0

        # Batch deletes — SQLite ``IN`` placeholder list is fine for our
        # scale (a workspace rarely has > 100k versions).
        await self._delete_many(
            conn,
            "DELETE FROM kv_entries WHERE id IN ({placeholders})",
            ids_to_delete,
        )
        return len(ids_to_delete)

    # ------------------------------------------------------------------ files

    async def _gc_files(
        self,
        conn: aiosqlite.Connection,
        workspace_id: str,
        policy: RetentionPolicy,
        referenced: set[tuple[str, int]],
    ) -> tuple[int, set[str]]:
        """Trim file versions per policy.

        Returns ``(rows_deleted, freed_blob_refs)`` — the second item is
        the set of blob hashes whose reference count *might* have dropped
        to zero (the blob-GC pass re-checks before unlinking).
        """

        if policy.keep_versions is None and policy.keep_days is None:
            return 0, set()

        cutoff = (
            now_utc() - timedelta(days=policy.keep_days)
            if policy.keep_days is not None
            else None
        )

        cur = await conn.execute(
            "SELECT id, path, version, branch_id, created_at, blob_sha256, size "
            "FROM file_entries WHERE workspace_id=?",
            (workspace_id,),
        )
        rows = await cur.fetchall()
        await cur.close()

        groups: dict[tuple[str, str | None], list[aiosqlite.Row]] = {}
        for row in rows:
            groups.setdefault((row["path"], row["branch_id"]), []).append(row)
        for entries in groups.values():
            entries.sort(key=lambda r: r["version"], reverse=True)

        ids_to_delete: list[int] = []
        freed_blob_refs: set[str] = set()
        for entries in groups.values():
            for idx, row in enumerate(entries):
                if (row["path"], row["version"]) in referenced:
                    continue
                if policy.keep_versions is not None and idx < policy.keep_versions:
                    continue
                if cutoff is not None:
                    created = parse_ts(row["created_at"])
                    if created is not None and created >= cutoff:
                        continue
                ids_to_delete.append(int(row["id"]))
                # Tombstones carry empty sha — skip those.
                if row["blob_sha256"]:
                    freed_blob_refs.add(row["blob_sha256"])

        if not ids_to_delete:
            return 0, freed_blob_refs

        await self._delete_many(
            conn,
            "DELETE FROM file_entries WHERE id IN ({placeholders})",
            ids_to_delete,
        )
        return len(ids_to_delete), freed_blob_refs

    # ------------------------------------------------------------------ snapshots

    async def _gc_snapshots(
        self,
        conn: aiosqlite.Connection,
        workspace_id: str,
        policy: RetentionPolicy,
        active_branch_snapshots: set[str],
    ) -> int:
        """Drop the oldest snapshots beyond ``keep_snapshots``."""

        if policy.keep_snapshots is None:
            return 0
        keep = max(0, int(policy.keep_snapshots))

        cur = await conn.execute(
            "SELECT id FROM snapshots WHERE workspace_id=? "
            "ORDER BY created_at DESC, id DESC",
            (workspace_id,),
        )
        rows = await cur.fetchall()
        await cur.close()

        all_ids = [r["id"] for r in rows]
        if len(all_ids) <= keep:
            return 0

        # Candidates are the rows past the first ``keep`` (which we keep
        # because they are the freshest). Filter out anything still
        # referenced by an active branch.
        candidates = [
            sid for sid in all_ids[keep:] if sid not in active_branch_snapshots
        ]
        if not candidates:
            return 0

        await self._delete_many(
            conn,
            "DELETE FROM snapshots WHERE id IN ({placeholders})",
            candidates,
        )
        return len(candidates)

    # ------------------------------------------------------------------ branches

    async def _gc_branches(
        self,
        conn: aiosqlite.Connection,
        workspace_id: str,
        policy: RetentionPolicy,
    ) -> int:
        """Drop merged branches older than ``keep_days``."""

        if policy.keep_days is None:
            return 0
        cutoff = now_utc() - timedelta(days=policy.keep_days)

        cur = await conn.execute(
            "SELECT id, merged, merged_at, created_at FROM branches "
            "WHERE workspace_id=?",
            (workspace_id,),
        )
        rows = await cur.fetchall()
        await cur.close()

        ids_to_delete: list[str] = []
        for row in rows:
            if not bool(row["merged"]):
                continue
            # Use merged_at when available; fall back to created_at.
            ts = parse_ts(row["merged_at"]) or parse_ts(row["created_at"])
            if ts is None or ts < cutoff:
                ids_to_delete.append(row["id"])

        if not ids_to_delete:
            return 0

        # Cascade like ``delete_branch``: drop branch entries first, then
        # the row itself.
        for br_id in ids_to_delete:
            await conn.execute(
                "DELETE FROM kv_entries WHERE workspace_id=? AND branch_id=?",
                (workspace_id, br_id),
            )
            await conn.execute(
                "DELETE FROM file_entries WHERE workspace_id=? AND branch_id=?",
                (workspace_id, br_id),
            )
        await self._delete_many(
            conn,
            "DELETE FROM branches WHERE id IN ({placeholders})",
            ids_to_delete,
        )
        return len(ids_to_delete)

    # ------------------------------------------------------------------ blobs

    async def _gc_blobs(
        self,
        conn: aiosqlite.Connection,
        workspace_id: str,
        candidate_hashes: Iterable[str],
    ) -> tuple[int, int]:
        """Unlink blob files no longer referenced by any file entry.

        ``candidate_hashes`` narrows the scan: those are the only blobs
        that *might* have had their refcount drop to zero. We re-check
        each via a SELECT before unlinking — the blob is safe to drop
        only if no remaining row references it.
        """

        deleted = 0
        bytes_freed = 0
        ws_blob_dir = self.blobs_dir / workspace_id
        if not ws_blob_dir.exists():
            return 0, 0

        candidates = list(set(candidate_hashes))
        # Always include any orphans on disk that no row references at
        # all — covers the case where a previous run was interrupted.
        for path in ws_blob_dir.iterdir():
            if path.is_file() and not path.name.endswith(".tmp"):
                candidates.append(path.name)
        candidates = list(set(candidates))

        for sha in candidates:
            cur = await conn.execute(
                "SELECT 1 FROM file_entries "
                "WHERE workspace_id=? AND blob_sha256=? LIMIT 1",
                (workspace_id, sha),
            )
            row = await cur.fetchone()
            await cur.close()
            if row is not None:
                continue  # still referenced
            blob_path = ws_blob_dir / sha
            if not blob_path.exists() or not blob_path.is_file():
                continue
            try:
                size = blob_path.stat().st_size
            except OSError:
                size = 0
            try:
                blob_path.unlink()
            except OSError:
                continue
            deleted += 1
            bytes_freed += size

        return deleted, bytes_freed

    # ------------------------------------------------------------------ helpers

    async def _referenced_kv(
        self,
        conn: aiosqlite.Connection,
        workspace_id: str,
    ) -> set[tuple[str, int]]:
        """Build ``{(key, version)}`` referenced by any non-deleted snapshot."""

        cur = await conn.execute(
            "SELECT kv_versions FROM snapshots WHERE workspace_id=?",
            (workspace_id,),
        )
        rows = await cur.fetchall()
        await cur.close()
        out: set[tuple[str, int]] = set()
        for row in rows:
            mapping = json.loads(row["kv_versions"] or "{}")
            for key, version in mapping.items():
                out.add((key, int(version)))
        return out

    async def _referenced_files(
        self,
        conn: aiosqlite.Connection,
        workspace_id: str,
    ) -> set[tuple[str, int]]:
        cur = await conn.execute(
            "SELECT file_versions FROM snapshots WHERE workspace_id=?",
            (workspace_id,),
        )
        rows = await cur.fetchall()
        await cur.close()
        out: set[tuple[str, int]] = set()
        for row in rows:
            mapping = json.loads(row["file_versions"] or "{}")
            for path, version in mapping.items():
                out.add((path, int(version)))
        return out

    async def _active_branch_snapshots(
        self,
        conn: aiosqlite.Connection,
        workspace_id: str,
    ) -> set[str]:
        """Snapshot IDs that are the ``from_snapshot`` of an unmerged branch."""

        cur = await conn.execute(
            "SELECT from_snapshot_id FROM branches "
            "WHERE workspace_id=? AND merged=0",
            (workspace_id,),
        )
        rows = await cur.fetchall()
        await cur.close()
        return {row["from_snapshot_id"] for row in rows}

    async def _delete_many(
        self,
        conn: aiosqlite.Connection,
        sql_template: str,
        ids: list[Any],
    ) -> None:
        """Run a parameterised ``DELETE ... WHERE id IN (...)`` in chunks.

        SQLite caps to 999 parameters by default — chunking keeps us under
        that bar even for large collections.
        """

        if not ids:
            return
        chunk_size = 500
        for start in range(0, len(ids), chunk_size):
            chunk = ids[start : start + chunk_size]
            placeholders = ",".join(["?"] * len(chunk))
            sql = sql_template.format(placeholders=placeholders)
            await conn.execute(sql, tuple(chunk))


__all__ = [
    "GCEngine",
    "GCInProgress",
    "RetentionStore",
]
