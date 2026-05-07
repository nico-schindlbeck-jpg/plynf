# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 The Plinth Authors
"""Snapshot, branch, diff, and merge logic.

Snapshots
---------
A snapshot stores ``{key: version}`` and ``{path: version}`` dicts pointing
into the existing ``kv_entries`` / ``file_entries`` rows. Capturing a
snapshot is therefore O(workspace) but cheap in bytes — only metadata.

Branches
--------
A branch is created from a snapshot. It carries its own ``branch_id``
attached to KV/file rows. ``WorkspaceStore`` handles the read fall-through;
this module just owns the lifecycle (create / list / merge / delete).

Merging
-------
Merging copies every visible-on-the-branch latest version (including
tombstones, so deletes carry across) into ``branch_id IS NULL`` with fresh
version numbers. The merged-in entries become the new latest on main.
The branch is then marked ``merged=1`` and rejects further writes.
"""

from __future__ import annotations

import json
from pathlib import Path

import aiosqlite
from ulid import ULID

from .db import connect, iso, now_utc, parse_ts
from .exceptions import (
    BranchAlreadyMerged,
    BranchNotFound,
    SnapshotNotFound,
    WorkspaceNotFound,
)
from .models import Branch, DiffResult, MergeResult, Snapshot
from .storage import WorkspaceStore, resolve_branch_context


def _new_snapshot_id() -> str:
    return f"snap_{ULID()}"


def _new_branch_id() -> str:
    return f"br_{ULID()}"


def _row_to_snapshot(row: aiosqlite.Row) -> Snapshot:
    return Snapshot(
        id=row["id"],
        workspace_id=row["workspace_id"],
        name=row["name"],
        message=row["message"],
        parent_snapshot_id=row["parent_snapshot_id"],
        kv_versions=json.loads(row["kv_versions"] or "{}"),
        file_versions=json.loads(row["file_versions"] or "{}"),
        created_at=parse_ts(row["created_at"]),  # type: ignore[arg-type]
    )


def _row_to_branch(row: aiosqlite.Row) -> Branch:
    return Branch(
        id=row["id"],
        workspace_id=row["workspace_id"],
        name=row["name"],
        from_snapshot_id=row["from_snapshot_id"],
        merged=bool(row["merged"]),
        merged_at=parse_ts(row["merged_at"]),
        created_at=parse_ts(row["created_at"]),  # type: ignore[arg-type]
    )


class SnapshotStore:
    """Snapshot + branch lifecycle, parallel to :class:`WorkspaceStore`."""

    def __init__(self, store: WorkspaceStore) -> None:
        self.store = store
        self.db_path: Path = store.db_path

    # ------------------------------------------------------------------ snapshots

    async def create_snapshot(
        self,
        workspace_id: str,
        name: str,
        *,
        message: str | None = None,
        branch_id: str | None = None,
    ) -> Snapshot:
        """Capture the current latest version of every key/file."""

        async with connect(self.db_path) as conn:
            await self._assert_workspace(conn, workspace_id)
            ctx = await resolve_branch_context(conn, workspace_id, branch_id)

            kv_versions = await self._capture_kv_versions(conn, workspace_id, ctx)
            file_versions = await self._capture_file_versions(conn, workspace_id, ctx)

            parent_snapshot_id: str | None = None
            if branch_id is not None:
                cur = await conn.execute(
                    "SELECT from_snapshot_id FROM branches "
                    "WHERE id=? AND workspace_id=?",
                    (branch_id, workspace_id),
                )
                row = await cur.fetchone()
                await cur.close()
                if row is not None:
                    parent_snapshot_id = row["from_snapshot_id"]

            snap_id = _new_snapshot_id()
            ts = now_utc()
            await conn.execute(
                "INSERT INTO snapshots "
                "(id, workspace_id, name, message, parent_snapshot_id, "
                " kv_versions, file_versions, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    snap_id,
                    workspace_id,
                    name,
                    message,
                    parent_snapshot_id,
                    json.dumps(kv_versions, sort_keys=True),
                    json.dumps(file_versions, sort_keys=True),
                    iso(ts),
                ),
            )
            await conn.commit()
            return Snapshot(
                id=snap_id,
                workspace_id=workspace_id,
                name=name,
                message=message,
                parent_snapshot_id=parent_snapshot_id,
                kv_versions=kv_versions,
                file_versions=file_versions,
                created_at=ts,
            )

    async def list_snapshots(self, workspace_id: str) -> list[Snapshot]:
        async with connect(self.db_path) as conn:
            await self._assert_workspace(conn, workspace_id)
            cur = await conn.execute(
                "SELECT * FROM snapshots WHERE workspace_id=? "
                "ORDER BY created_at DESC, id DESC",
                (workspace_id,),
            )
            rows = await cur.fetchall()
            await cur.close()
            return [_row_to_snapshot(r) for r in rows]

    async def get_snapshot(self, workspace_id: str, snapshot_id: str) -> Snapshot:
        async with connect(self.db_path) as conn:
            await self._assert_workspace(conn, workspace_id)
            cur = await conn.execute(
                "SELECT * FROM snapshots WHERE id=? AND workspace_id=?",
                (snapshot_id, workspace_id),
            )
            row = await cur.fetchone()
            await cur.close()
            if row is None:
                raise SnapshotNotFound(snapshot_id)
            return _row_to_snapshot(row)

    async def diff_snapshots(
        self,
        workspace_id: str,
        a_id: str,
        b_id: str,
    ) -> DiffResult:
        """Diff snapshot ``a`` against snapshot ``b``.

        Conventions: items in ``b`` and not in ``a`` are *added*; items in
        ``a`` and not in ``b`` are *deleted*; items present in both with
        different versions are *modified*.
        """

        a = await self.get_snapshot(workspace_id, a_id)
        b = await self.get_snapshot(workspace_id, b_id)

        kv_added = sorted(set(b.kv_versions) - set(a.kv_versions))
        kv_deleted = sorted(set(a.kv_versions) - set(b.kv_versions))
        kv_modified = sorted(
            k for k in set(a.kv_versions) & set(b.kv_versions)
            if a.kv_versions[k] != b.kv_versions[k]
        )

        files_added = sorted(set(b.file_versions) - set(a.file_versions))
        files_deleted = sorted(set(a.file_versions) - set(b.file_versions))
        files_modified = sorted(
            p for p in set(a.file_versions) & set(b.file_versions)
            if a.file_versions[p] != b.file_versions[p]
        )

        return DiffResult(
            kv_added=kv_added,
            kv_modified=kv_modified,
            kv_deleted=kv_deleted,
            files_added=files_added,
            files_modified=files_modified,
            files_deleted=files_deleted,
        )

    # ------------------------------------------------------------------ branches

    async def create_branch(
        self,
        workspace_id: str,
        name: str,
        from_snapshot_id: str,
    ) -> Branch:
        async with connect(self.db_path) as conn:
            await self._assert_workspace(conn, workspace_id)
            cur = await conn.execute(
                "SELECT id FROM snapshots WHERE id=? AND workspace_id=?",
                (from_snapshot_id, workspace_id),
            )
            snap_row = await cur.fetchone()
            await cur.close()
            if snap_row is None:
                raise SnapshotNotFound(from_snapshot_id)

            br_id = _new_branch_id()
            ts = now_utc()
            await conn.execute(
                "INSERT INTO branches "
                "(id, workspace_id, name, from_snapshot_id, merged, merged_at, created_at) "
                "VALUES (?, ?, ?, ?, 0, NULL, ?)",
                (br_id, workspace_id, name, from_snapshot_id, iso(ts)),
            )
            await conn.commit()
            return Branch(
                id=br_id,
                workspace_id=workspace_id,
                name=name,
                from_snapshot_id=from_snapshot_id,
                merged=False,
                merged_at=None,
                created_at=ts,
            )

    async def list_branches(self, workspace_id: str) -> list[Branch]:
        async with connect(self.db_path) as conn:
            await self._assert_workspace(conn, workspace_id)
            cur = await conn.execute(
                "SELECT * FROM branches WHERE workspace_id=? "
                "ORDER BY created_at ASC, id ASC",
                (workspace_id,),
            )
            rows = await cur.fetchall()
            await cur.close()
            return [_row_to_branch(r) for r in rows]

    async def get_branch(self, workspace_id: str, branch_id: str) -> Branch:
        async with connect(self.db_path) as conn:
            await self._assert_workspace(conn, workspace_id)
            cur = await conn.execute(
                "SELECT * FROM branches WHERE id=? AND workspace_id=?",
                (branch_id, workspace_id),
            )
            row = await cur.fetchone()
            await cur.close()
            if row is None:
                raise BranchNotFound(branch_id)
            return _row_to_branch(row)

    async def delete_branch(self, workspace_id: str, branch_id: str) -> None:
        async with connect(self.db_path) as conn:
            await self._assert_workspace(conn, workspace_id)
            cur = await conn.execute(
                "SELECT id FROM branches WHERE id=? AND workspace_id=?",
                (branch_id, workspace_id),
            )
            row = await cur.fetchone()
            await cur.close()
            if row is None:
                raise BranchNotFound(branch_id)
            await conn.execute(
                "DELETE FROM kv_entries WHERE workspace_id=? AND branch_id=?",
                (workspace_id, branch_id),
            )
            await conn.execute(
                "DELETE FROM file_entries WHERE workspace_id=? AND branch_id=?",
                (workspace_id, branch_id),
            )
            await conn.execute(
                "DELETE FROM branches WHERE id=? AND workspace_id=?",
                (branch_id, workspace_id),
            )
            await conn.commit()

    async def merge_branch(self, workspace_id: str, branch_id: str) -> MergeResult:
        """Copy a branch's latest entries into main as new versions."""

        branch = await self.get_branch(workspace_id, branch_id)
        if branch.merged:
            raise BranchAlreadyMerged(branch_id)

        ts = now_utc()
        kv_merged: list[str] = []
        files_merged: list[str] = []

        async with connect(self.db_path) as conn:
            # KV — pull every (key, latest version) for this branch.
            cur = await conn.execute(
                "SELECT key, MAX(version) AS v FROM kv_entries "
                "WHERE workspace_id=? AND branch_id=? GROUP BY key",
                (workspace_id, branch_id),
            )
            kv_pairs = [(r["key"], r["v"]) for r in await cur.fetchall()]
            await cur.close()

            for key, version in kv_pairs:
                cur = await conn.execute(
                    "SELECT * FROM kv_entries "
                    "WHERE workspace_id=? AND branch_id=? AND key=? AND version=?",
                    (workspace_id, branch_id, key, version),
                )
                row = await cur.fetchone()
                await cur.close()
                if row is None:
                    continue
                next_version = await self._next_main_kv_version(conn, workspace_id, key)
                await conn.execute(
                    "INSERT INTO kv_entries "
                    "(workspace_id, key, value, version, branch_id, deleted, created_at) "
                    "VALUES (?, ?, ?, ?, NULL, ?, ?)",
                    (
                        workspace_id,
                        key,
                        row["value"],
                        next_version,
                        int(bool(row["deleted"])),
                        iso(ts),
                    ),
                )
                kv_merged.append(key)

            # Files — same shape.
            cur = await conn.execute(
                "SELECT path, MAX(version) AS v FROM file_entries "
                "WHERE workspace_id=? AND branch_id=? GROUP BY path",
                (workspace_id, branch_id),
            )
            file_pairs = [(r["path"], r["v"]) for r in await cur.fetchall()]
            await cur.close()

            for path, version in file_pairs:
                cur = await conn.execute(
                    "SELECT * FROM file_entries "
                    "WHERE workspace_id=? AND branch_id=? AND path=? AND version=?",
                    (workspace_id, branch_id, path, version),
                )
                row = await cur.fetchone()
                await cur.close()
                if row is None:
                    continue
                next_version = await self._next_main_file_version(conn, workspace_id, path)
                await conn.execute(
                    "INSERT INTO file_entries "
                    "(workspace_id, path, blob_sha256, size, content_type, "
                    " version, branch_id, deleted, created_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, NULL, ?, ?)",
                    (
                        workspace_id,
                        path,
                        row["blob_sha256"],
                        row["size"],
                        row["content_type"],
                        next_version,
                        int(bool(row["deleted"])),
                        iso(ts),
                    ),
                )
                files_merged.append(path)

            await conn.execute(
                "UPDATE branches SET merged=1, merged_at=? WHERE id=? AND workspace_id=?",
                (iso(ts), branch_id, workspace_id),
            )
            await conn.execute(
                "UPDATE workspaces SET updated_at=? WHERE id=?",
                (iso(ts), workspace_id),
            )
            await conn.commit()

        return MergeResult(
            branch_id=branch_id,
            workspace_id=workspace_id,
            kv_merged=sorted(kv_merged),
            files_merged=sorted(files_merged),
            merged_at=ts,
        )

    # ------------------------------------------------------------------ internals

    async def _assert_workspace(
        self,
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

    async def _capture_kv_versions(
        self,
        conn: aiosqlite.Connection,
        workspace_id: str,
        ctx,
    ) -> dict[str, int]:
        """Capture latest non-tombstone version per key visible on this branch."""

        result: dict[str, int] = {}
        if ctx.branch_id is None:
            cur = await conn.execute(
                "SELECT key, MAX(version) AS v FROM kv_entries "
                "WHERE workspace_id=? AND branch_id IS NULL GROUP BY key",
                (workspace_id,),
            )
            for row in await cur.fetchall():
                key, version = row["key"], row["v"]
                inner = await conn.execute(
                    "SELECT deleted FROM kv_entries "
                    "WHERE workspace_id=? AND key=? AND version=? AND branch_id IS NULL",
                    (workspace_id, key, version),
                )
                latest = await inner.fetchone()
                await inner.close()
                if latest is None or bool(latest["deleted"]):
                    continue
                result[key] = version
            await cur.close()
            return result

        # Branch case — combine branch entries with the from_snapshot floor.
        # Start with from_snapshot's captures; branch overrides will take
        # priority.
        result.update(ctx.from_snapshot_kv)

        cur = await conn.execute(
            "SELECT key, MAX(version) AS v FROM kv_entries "
            "WHERE workspace_id=? AND branch_id=? GROUP BY key",
            (workspace_id, ctx.branch_id),
        )
        for row in await cur.fetchall():
            key, version = row["key"], row["v"]
            inner = await conn.execute(
                "SELECT deleted FROM kv_entries "
                "WHERE workspace_id=? AND key=? AND version=? AND branch_id=?",
                (workspace_id, key, version, ctx.branch_id),
            )
            latest = await inner.fetchone()
            await inner.close()
            if latest is None:
                continue
            if bool(latest["deleted"]):
                # Tombstone on the branch removes the inherited capture.
                result.pop(key, None)
                continue
            result[key] = version
        await cur.close()
        return result

    async def _capture_file_versions(
        self,
        conn: aiosqlite.Connection,
        workspace_id: str,
        ctx,
    ) -> dict[str, int]:
        result: dict[str, int] = {}
        if ctx.branch_id is None:
            cur = await conn.execute(
                "SELECT path, MAX(version) AS v FROM file_entries "
                "WHERE workspace_id=? AND branch_id IS NULL GROUP BY path",
                (workspace_id,),
            )
            for row in await cur.fetchall():
                path, version = row["path"], row["v"]
                inner = await conn.execute(
                    "SELECT deleted FROM file_entries "
                    "WHERE workspace_id=? AND path=? AND version=? AND branch_id IS NULL",
                    (workspace_id, path, version),
                )
                latest = await inner.fetchone()
                await inner.close()
                if latest is None or bool(latest["deleted"]):
                    continue
                result[path] = version
            await cur.close()
            return result

        result.update(ctx.from_snapshot_files)

        cur = await conn.execute(
            "SELECT path, MAX(version) AS v FROM file_entries "
            "WHERE workspace_id=? AND branch_id=? GROUP BY path",
            (workspace_id, ctx.branch_id),
        )
        for row in await cur.fetchall():
            path, version = row["path"], row["v"]
            inner = await conn.execute(
                "SELECT deleted FROM file_entries "
                "WHERE workspace_id=? AND path=? AND version=? AND branch_id=?",
                (workspace_id, path, version, ctx.branch_id),
            )
            latest = await inner.fetchone()
            await inner.close()
            if latest is None:
                continue
            if bool(latest["deleted"]):
                result.pop(path, None)
                continue
            result[path] = version
        await cur.close()
        return result

    async def _next_main_kv_version(
        self,
        conn: aiosqlite.Connection,
        workspace_id: str,
        key: str,
    ) -> int:
        cur = await conn.execute(
            "SELECT MAX(version) FROM kv_entries "
            "WHERE workspace_id=? AND key=? AND branch_id IS NULL",
            (workspace_id, key),
        )
        row = await cur.fetchone()
        await cur.close()
        return (row[0] or 0) + 1

    async def _next_main_file_version(
        self,
        conn: aiosqlite.Connection,
        workspace_id: str,
        path: str,
    ) -> int:
        cur = await conn.execute(
            "SELECT MAX(version) FROM file_entries "
            "WHERE workspace_id=? AND path=? AND branch_id IS NULL",
            (workspace_id, path),
        )
        row = await cur.fetchone()
        await cur.close()
        return (row[0] or 0) + 1
