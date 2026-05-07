# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 The Plinth Authors
"""Workspace + KV + file storage with versioning and branch fall-through.

Read semantics on a branch
--------------------------
A branch is rooted at a snapshot. Reads target a key/path on that branch and
fall through in this order:

1. Latest non-tombstone entry written **on the branch** (``branch_id = br_*``).
2. The version captured by the branch's ``from_snapshot`` on **main**
   (``branch_id IS NULL``) — but only if it has not been tombstoned on the
   branch.

If a key has a tombstone on the branch, reads on the branch see "deleted",
even if main still has it.

Write semantics
---------------
Versions are monotonic per ``(workspace, key/path, branch_id)``. A write on a
branch never bumps the version on main. A write on main never bumps the
version on a branch.
"""

from __future__ import annotations

import hashlib
import json
import mimetypes
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import aiosqlite
from ulid import ULID

from .db import connect, iso, now_utc, parse_ts
from .exceptions import (
    FileNotFound,
    KeyNotFound,
    SnapshotNotFound,
    WorkspaceNotFound,
)
from .models import FileEntry, KVEntry, Workspace

# ---------------------------------------------------------------------------
# Helpers


def _new_workspace_id() -> str:
    return f"ws_{ULID()}"


def _row_to_workspace(row: aiosqlite.Row) -> Workspace:
    # ``tenant_id`` is a v0.3 column. ``aiosqlite.Row`` raises IndexError if a
    # column doesn't exist (older DB pre-migration), so guard the lookup.
    try:
        tenant_id = row["tenant_id"] or "default"
    except (IndexError, KeyError):
        tenant_id = "default"
    return Workspace(
        id=row["id"],
        name=row["name"],
        tenant_id=tenant_id,
        created_at=parse_ts(row["created_at"]),  # type: ignore[arg-type]
        updated_at=parse_ts(row["updated_at"]),  # type: ignore[arg-type]
        metadata=json.loads(row["metadata"] or "{}"),
    )


def _row_to_kv(row: aiosqlite.Row) -> KVEntry:
    return KVEntry(
        workspace_id=row["workspace_id"],
        key=row["key"],
        value=json.loads(row["value"]),
        version=row["version"],
        created_at=parse_ts(row["created_at"]),  # type: ignore[arg-type]
        deleted=bool(row["deleted"]),
        branch_id=row["branch_id"],
    )


def _row_to_file(row: aiosqlite.Row) -> FileEntry:
    return FileEntry(
        workspace_id=row["workspace_id"],
        path=row["path"],
        size=row["size"],
        sha256=row["blob_sha256"],
        content_type=row["content_type"],
        version=row["version"],
        created_at=parse_ts(row["created_at"]),  # type: ignore[arg-type]
        deleted=bool(row["deleted"]),
        branch_id=row["branch_id"],
    )


# Small overrides for content-types Python's stdlib mimetypes is patchy on,
# especially on minimal Linux containers where /etc/mime.types is absent.
_CONTENT_TYPE_OVERRIDES = {
    ".md": "text/markdown",
    ".markdown": "text/markdown",
    ".jsonl": "application/x-ndjson",
    ".log": "text/plain",
    ".yaml": "application/yaml",
    ".yml": "application/yaml",
    ".toml": "application/toml",
}


def _detect_content_type(path: str, default: str = "application/octet-stream") -> str:
    lower = path.lower()
    for ext, mime in _CONTENT_TYPE_OVERRIDES.items():
        if lower.endswith(ext):
            return mime
    guess, _ = mimetypes.guess_type(path)
    return guess or default


def _sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


# ---------------------------------------------------------------------------
# Branch helpers (kept here, not in snapshots.py, because storage reads need
# them — and snapshots.py imports storage). This avoids a circular import.


@dataclass
class _BranchContext:
    """Resolved branch state for a read/write."""

    branch_id: str | None
    from_snapshot_kv: dict[str, int]
    from_snapshot_files: dict[str, int]


async def resolve_branch_context(
    conn: aiosqlite.Connection,
    workspace_id: str,
    branch_id: str | None,
) -> _BranchContext:
    """Look up branch + its snapshot capture so reads can fall through.

    Raises:
        BranchNotFound: if ``branch_id`` does not exist (or wrong workspace).
        SnapshotNotFound: if the branch's ``from_snapshot`` is missing.
    """

    if branch_id is None:
        return _BranchContext(branch_id=None, from_snapshot_kv={}, from_snapshot_files={})

    cur = await conn.execute(
        "SELECT id, from_snapshot_id FROM branches WHERE id=? AND workspace_id=?",
        (branch_id, workspace_id),
    )
    row = await cur.fetchone()
    await cur.close()
    if row is None:
        # Local import to avoid a circular import with snapshots.py.
        from .exceptions import BranchNotFound

        raise BranchNotFound(branch_id)

    snap_cur = await conn.execute(
        "SELECT kv_versions, file_versions FROM snapshots "
        "WHERE id=? AND workspace_id=?",
        (row["from_snapshot_id"], workspace_id),
    )
    snap_row = await snap_cur.fetchone()
    await snap_cur.close()
    if snap_row is None:
        raise SnapshotNotFound(row["from_snapshot_id"])

    return _BranchContext(
        branch_id=branch_id,
        from_snapshot_kv=json.loads(snap_row["kv_versions"] or "{}"),
        from_snapshot_files=json.loads(snap_row["file_versions"] or "{}"),
    )


# ---------------------------------------------------------------------------
# Workspace storage


class WorkspaceStore:
    """Workspace CRUD + KV/file versioning over SQLite + content-addressed blobs."""

    def __init__(self, db_path: Path, blobs_dir: Path) -> None:
        self.db_path = db_path
        self.blobs_dir = blobs_dir

    # ------------------------------------------------------------------ workspaces

    async def create_workspace(
        self,
        name: str,
        metadata: dict[str, Any] | None = None,
        *,
        tenant_id: str = "default",
    ) -> Workspace:
        ws_id = _new_workspace_id()
        ts = now_utc()
        async with connect(self.db_path) as conn:
            await conn.execute(
                "INSERT INTO workspaces "
                "(id, name, metadata, tenant_id, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (
                    ws_id,
                    name,
                    json.dumps(metadata or {}, sort_keys=True),
                    tenant_id,
                    iso(ts),
                    iso(ts),
                ),
            )
            await conn.commit()
        # Create the per-workspace blob dir up-front; cheap and avoids a race
        # the first time someone PUTs a file.
        (self.blobs_dir / ws_id).mkdir(parents=True, exist_ok=True)
        return Workspace(
            id=ws_id,
            name=name,
            tenant_id=tenant_id,
            metadata=metadata or {},
            created_at=ts,
            updated_at=ts,
        )

    async def get_workspace(
        self,
        workspace_id: str,
        *,
        tenant_id: str | None = None,
    ) -> Workspace:
        """Fetch a workspace.

        When ``tenant_id`` is supplied, treat a tenant mismatch as
        :class:`WorkspaceNotFound` — the caller should not learn whether the
        ID exists in another tenant.
        """

        async with connect(self.db_path) as conn:
            cur = await conn.execute(
                "SELECT id, name, metadata, tenant_id, created_at, updated_at "
                "FROM workspaces WHERE id=?",
                (workspace_id,),
            )
            row = await cur.fetchone()
            await cur.close()
            if row is None:
                raise WorkspaceNotFound(workspace_id)
            ws = _row_to_workspace(row)
            if tenant_id is not None and ws.tenant_id != tenant_id:
                raise WorkspaceNotFound(workspace_id)
            return ws

    async def list_workspaces(
        self,
        *,
        tenant_id: str | None = None,
    ) -> list[Workspace]:
        """List workspaces, optionally restricted to one tenant."""

        async with connect(self.db_path) as conn:
            if tenant_id is None:
                cur = await conn.execute(
                    "SELECT id, name, metadata, tenant_id, created_at, updated_at "
                    "FROM workspaces ORDER BY created_at ASC"
                )
            else:
                cur = await conn.execute(
                    "SELECT id, name, metadata, tenant_id, created_at, updated_at "
                    "FROM workspaces WHERE tenant_id=? ORDER BY created_at ASC",
                    (tenant_id,),
                )
            rows = await cur.fetchall()
            await cur.close()
            return [_row_to_workspace(r) for r in rows]

    async def list_tenants(self) -> list[dict[str, Any]]:
        """Distinct tenants and their workspace counts."""

        async with connect(self.db_path) as conn:
            cur = await conn.execute(
                "SELECT tenant_id, COUNT(*) AS workspace_count "
                "FROM workspaces GROUP BY tenant_id ORDER BY tenant_id"
            )
            rows = await cur.fetchall()
            await cur.close()
        return [
            {"id": row["tenant_id"], "workspace_count": int(row["workspace_count"])}
            for row in rows
        ]

    async def delete_workspace(self, workspace_id: str) -> None:
        async with connect(self.db_path) as conn:
            cur = await conn.execute(
                "SELECT id FROM workspaces WHERE id=?",
                (workspace_id,),
            )
            row = await cur.fetchone()
            await cur.close()
            if row is None:
                raise WorkspaceNotFound(workspace_id)

            # Cascade by hand — SQLite ``ON DELETE CASCADE`` would need the
            # FK to be declared with it on every dependent table; doing it
            # explicitly keeps the schema self-evident.
            await conn.execute("DELETE FROM kv_entries WHERE workspace_id=?", (workspace_id,))
            await conn.execute("DELETE FROM file_entries WHERE workspace_id=?", (workspace_id,))
            await conn.execute("DELETE FROM snapshots WHERE workspace_id=?", (workspace_id,))
            await conn.execute("DELETE FROM branches WHERE workspace_id=?", (workspace_id,))
            await conn.execute("DELETE FROM workspaces WHERE id=?", (workspace_id,))
            await conn.commit()

        # Best-effort blob cleanup. Missing dir is fine.
        ws_blob_dir = self.blobs_dir / workspace_id
        if ws_blob_dir.exists():
            for p in ws_blob_dir.iterdir():
                if p.is_file():
                    p.unlink()
            ws_blob_dir.rmdir()

    async def _assert_workspace(self, conn: aiosqlite.Connection, workspace_id: str) -> None:
        cur = await conn.execute(
            "SELECT 1 FROM workspaces WHERE id=?",
            (workspace_id,),
        )
        row = await cur.fetchone()
        await cur.close()
        if row is None:
            raise WorkspaceNotFound(workspace_id)

    # ------------------------------------------------------------------ kv

    async def kv_put(
        self,
        workspace_id: str,
        key: str,
        value: Any,
        *,
        branch_id: str | None = None,
    ) -> KVEntry:
        """Append a new immutable version for ``key``."""

        async with connect(self.db_path) as conn:
            await self._assert_workspace(conn, workspace_id)
            if branch_id is not None:
                await resolve_branch_context(conn, workspace_id, branch_id)
            version = await self._next_kv_version(conn, workspace_id, key, branch_id)
            ts = now_utc()
            await conn.execute(
                "INSERT INTO kv_entries "
                "(workspace_id, key, value, version, branch_id, deleted, created_at) "
                "VALUES (?, ?, ?, ?, ?, 0, ?)",
                (
                    workspace_id,
                    key,
                    json.dumps(value, sort_keys=True),
                    version,
                    branch_id,
                    iso(ts),
                ),
            )
            await self._touch_workspace(conn, workspace_id, ts)
            await conn.commit()
            return KVEntry(
                workspace_id=workspace_id,
                key=key,
                value=value,
                version=version,
                created_at=ts,
                deleted=False,
                branch_id=branch_id,
            )

    async def kv_delete(
        self,
        workspace_id: str,
        key: str,
        *,
        branch_id: str | None = None,
    ) -> KVEntry:
        """Append a tombstone version for ``key``."""

        async with connect(self.db_path) as conn:
            await self._assert_workspace(conn, workspace_id)
            ctx = await resolve_branch_context(conn, workspace_id, branch_id)

            # Make sure the key is currently visible — on the branch, on
            # main directly, or via the branch's snapshot fall-through.
            existing = await self._latest_kv_row(conn, workspace_id, key, branch_id)
            visible = existing is not None and not bool(existing["deleted"])
            if not visible and branch_id is not None and key in ctx.from_snapshot_kv:
                snap_v = ctx.from_snapshot_kv[key]
                main_row = await self._kv_row_at_version(
                    conn, workspace_id, key, snap_v, None
                )
                visible = main_row is not None and not bool(main_row["deleted"])
            if not visible:
                raise KeyNotFound(workspace_id, key)
            version = await self._next_kv_version(conn, workspace_id, key, branch_id)
            ts = now_utc()
            await conn.execute(
                "INSERT INTO kv_entries "
                "(workspace_id, key, value, version, branch_id, deleted, created_at) "
                "VALUES (?, ?, ?, ?, ?, 1, ?)",
                (workspace_id, key, "null", version, branch_id, iso(ts)),
            )
            await self._touch_workspace(conn, workspace_id, ts)
            await conn.commit()
            return KVEntry(
                workspace_id=workspace_id,
                key=key,
                value=None,
                version=version,
                created_at=ts,
                deleted=True,
                branch_id=branch_id,
            )

    async def kv_get(
        self,
        workspace_id: str,
        key: str,
        *,
        version: int | None = None,
        branch_id: str | None = None,
    ) -> KVEntry:
        """Get a single version of ``key`` (latest by default)."""

        async with connect(self.db_path) as conn:
            await self._assert_workspace(conn, workspace_id)
            ctx = await resolve_branch_context(conn, workspace_id, branch_id)

            if version is not None:
                # Specific-version reads always look on the requested branch
                # first, then fall back to main if missing.
                row = await self._kv_row_at_version(conn, workspace_id, key, version, branch_id)
                if row is None and branch_id is not None:
                    row = await self._kv_row_at_version(conn, workspace_id, key, version, None)
                if row is None:
                    raise KeyNotFound(workspace_id, key, version=version)
                return _row_to_kv(row)

            row = await self._latest_kv_row(conn, workspace_id, key, branch_id)
            if row is None:
                # Fall through to the from_snapshot capture on main.
                if branch_id is not None and key in ctx.from_snapshot_kv:
                    snap_v = ctx.from_snapshot_kv[key]
                    main_row = await self._kv_row_at_version(
                        conn, workspace_id, key, snap_v, None
                    )
                    if main_row is not None:
                        return _row_to_kv(main_row)
                raise KeyNotFound(workspace_id, key)
            if bool(row["deleted"]):
                raise KeyNotFound(workspace_id, key)
            return _row_to_kv(row)

    async def kv_history(
        self,
        workspace_id: str,
        key: str,
        *,
        branch_id: str | None = None,
    ) -> list[KVEntry]:
        """All versions of ``key`` ordered ascending by version.

        On a branch, history concatenates the main-line history up to the
        branch's snapshot capture with the branch-specific history.
        """

        async with connect(self.db_path) as conn:
            await self._assert_workspace(conn, workspace_id)
            ctx = await resolve_branch_context(conn, workspace_id, branch_id)

            entries: list[KVEntry] = []

            if branch_id is not None and key in ctx.from_snapshot_kv:
                cur = await conn.execute(
                    "SELECT * FROM kv_entries "
                    "WHERE workspace_id=? AND key=? AND branch_id IS NULL "
                    "AND version <= ? ORDER BY version ASC",
                    (workspace_id, key, ctx.from_snapshot_kv[key]),
                )
                entries.extend(_row_to_kv(r) for r in await cur.fetchall())
                await cur.close()

            if branch_id is None:
                cur = await conn.execute(
                    "SELECT * FROM kv_entries "
                    "WHERE workspace_id=? AND key=? AND branch_id IS NULL "
                    "ORDER BY version ASC",
                    (workspace_id, key),
                )
            else:
                cur = await conn.execute(
                    "SELECT * FROM kv_entries "
                    "WHERE workspace_id=? AND key=? AND branch_id=? "
                    "ORDER BY version ASC",
                    (workspace_id, key, branch_id),
                )
            entries.extend(_row_to_kv(r) for r in await cur.fetchall())
            await cur.close()

            if not entries:
                raise KeyNotFound(workspace_id, key)
            return entries

    async def kv_list(
        self,
        workspace_id: str,
        *,
        branch_id: str | None = None,
    ) -> list[KVEntry]:
        """Latest non-tombstone entry for every key visible on this branch."""

        async with connect(self.db_path) as conn:
            await self._assert_workspace(conn, workspace_id)
            ctx = await resolve_branch_context(conn, workspace_id, branch_id)

            keys = await self._all_kv_keys(conn, workspace_id, ctx)
            entries: list[KVEntry] = []
            for key in keys:
                row = await self._latest_kv_row(conn, workspace_id, key, branch_id)
                if row is None and branch_id is not None and key in ctx.from_snapshot_kv:
                    row = await self._kv_row_at_version(
                        conn, workspace_id, key, ctx.from_snapshot_kv[key], None
                    )
                if row is None:
                    continue
                if bool(row["deleted"]):
                    continue
                entries.append(_row_to_kv(row))
            entries.sort(key=lambda e: e.key)
            return entries

    # ------------------------------------------------------------------ files

    async def file_put(
        self,
        workspace_id: str,
        path: str,
        data: bytes,
        *,
        content_type: str | None = None,
        branch_id: str | None = None,
    ) -> FileEntry:
        """Write a new immutable version of ``path``."""

        async with connect(self.db_path) as conn:
            await self._assert_workspace(conn, workspace_id)
            if branch_id is not None:
                await resolve_branch_context(conn, workspace_id, branch_id)

            sha256 = _sha256_hex(data)
            ct = content_type or _detect_content_type(path)
            version = await self._next_file_version(conn, workspace_id, path, branch_id)
            ts = now_utc()

            blob_path = self._blob_path(workspace_id, sha256)
            blob_path.parent.mkdir(parents=True, exist_ok=True)
            if not blob_path.exists():
                # Write atomically — write to a tmp file and rename.
                tmp_path = blob_path.with_suffix(".tmp")
                tmp_path.write_bytes(data)
                tmp_path.replace(blob_path)

            await conn.execute(
                "INSERT INTO file_entries "
                "(workspace_id, path, blob_sha256, size, content_type, "
                " version, branch_id, deleted, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, 0, ?)",
                (
                    workspace_id,
                    path,
                    sha256,
                    len(data),
                    ct,
                    version,
                    branch_id,
                    iso(ts),
                ),
            )
            await self._touch_workspace(conn, workspace_id, ts)
            await conn.commit()
            return FileEntry(
                workspace_id=workspace_id,
                path=path,
                size=len(data),
                sha256=sha256,
                content_type=ct,
                version=version,
                created_at=ts,
                deleted=False,
                branch_id=branch_id,
            )

    async def file_get_meta(
        self,
        workspace_id: str,
        path: str,
        *,
        version: int | None = None,
        branch_id: str | None = None,
    ) -> FileEntry:
        """Metadata for one version of ``path`` (latest by default)."""

        async with connect(self.db_path) as conn:
            await self._assert_workspace(conn, workspace_id)
            ctx = await resolve_branch_context(conn, workspace_id, branch_id)

            if version is not None:
                row = await self._file_row_at_version(
                    conn, workspace_id, path, version, branch_id
                )
                if row is None and branch_id is not None:
                    row = await self._file_row_at_version(
                        conn, workspace_id, path, version, None
                    )
                if row is None:
                    raise FileNotFound(workspace_id, path, version=version)
                return _row_to_file(row)

            row = await self._latest_file_row(conn, workspace_id, path, branch_id)
            if row is None and branch_id is not None and path in ctx.from_snapshot_files:
                snap_v = ctx.from_snapshot_files[path]
                row = await self._file_row_at_version(conn, workspace_id, path, snap_v, None)
            if row is None:
                raise FileNotFound(workspace_id, path)
            if bool(row["deleted"]):
                raise FileNotFound(workspace_id, path)
            return _row_to_file(row)

    async def file_read(
        self,
        workspace_id: str,
        path: str,
        *,
        version: int | None = None,
        branch_id: str | None = None,
    ) -> tuple[FileEntry, bytes]:
        """Return ``(metadata, bytes)`` for the requested version."""

        meta = await self.file_get_meta(
            workspace_id, path, version=version, branch_id=branch_id
        )
        blob_path = self._blob_path(workspace_id, meta.sha256)
        if not blob_path.exists():
            # The blob is missing on disk — treat as a 404 with extra detail
            # so operators can spot a corrupted store quickly.
            raise FileNotFound(workspace_id, path, version=meta.version)
        data = blob_path.read_bytes()

        # Defensive integrity check. Catches blob corruption / wrong file.
        if _sha256_hex(data) != meta.sha256:
            raise FileNotFound(workspace_id, path, version=meta.version)
        return meta, data

    async def file_delete(
        self,
        workspace_id: str,
        path: str,
        *,
        branch_id: str | None = None,
    ) -> FileEntry:
        async with connect(self.db_path) as conn:
            await self._assert_workspace(conn, workspace_id)
            ctx = await resolve_branch_context(conn, workspace_id, branch_id)

            existing = await self._latest_file_row(conn, workspace_id, path, branch_id)
            content_type_for_tombstone: str | None = None
            visible = existing is not None and not bool(existing["deleted"])
            if visible:
                content_type_for_tombstone = existing["content_type"]
            elif branch_id is not None and path in ctx.from_snapshot_files:
                snap_v = ctx.from_snapshot_files[path]
                main_row = await self._file_row_at_version(
                    conn, workspace_id, path, snap_v, None
                )
                if main_row is not None and not bool(main_row["deleted"]):
                    visible = True
                    content_type_for_tombstone = main_row["content_type"]
            if not visible:
                raise FileNotFound(workspace_id, path)

            version = await self._next_file_version(conn, workspace_id, path, branch_id)
            ts = now_utc()
            await conn.execute(
                "INSERT INTO file_entries "
                "(workspace_id, path, blob_sha256, size, content_type, "
                " version, branch_id, deleted, created_at) "
                "VALUES (?, ?, ?, 0, ?, ?, ?, 1, ?)",
                (
                    workspace_id,
                    path,
                    "",
                    content_type_for_tombstone or "application/octet-stream",
                    version,
                    branch_id,
                    iso(ts),
                ),
            )
            await self._touch_workspace(conn, workspace_id, ts)
            await conn.commit()
            return FileEntry(
                workspace_id=workspace_id,
                path=path,
                size=0,
                sha256="",
                content_type=content_type_for_tombstone or "application/octet-stream",
                version=version,
                created_at=ts,
                deleted=True,
                branch_id=branch_id,
            )

    async def file_list(
        self,
        workspace_id: str,
        *,
        branch_id: str | None = None,
    ) -> list[FileEntry]:
        async with connect(self.db_path) as conn:
            await self._assert_workspace(conn, workspace_id)
            ctx = await resolve_branch_context(conn, workspace_id, branch_id)

            paths = await self._all_file_paths(conn, workspace_id, ctx)
            entries: list[FileEntry] = []
            for path in paths:
                row = await self._latest_file_row(conn, workspace_id, path, branch_id)
                if row is None and branch_id is not None and path in ctx.from_snapshot_files:
                    row = await self._file_row_at_version(
                        conn, workspace_id, path, ctx.from_snapshot_files[path], None
                    )
                if row is None:
                    continue
                if bool(row["deleted"]):
                    continue
                entries.append(_row_to_file(row))
            entries.sort(key=lambda e: e.path)
            return entries

    # ------------------------------------------------------------------ internals

    def _blob_path(self, workspace_id: str, sha256: str) -> Path:
        return self.blobs_dir / workspace_id / sha256

    async def _next_kv_version(
        self,
        conn: aiosqlite.Connection,
        workspace_id: str,
        key: str,
        branch_id: str | None,
    ) -> int:
        if branch_id is None:
            cur = await conn.execute(
                "SELECT MAX(version) FROM kv_entries "
                "WHERE workspace_id=? AND key=? AND branch_id IS NULL",
                (workspace_id, key),
            )
        else:
            cur = await conn.execute(
                "SELECT MAX(version) FROM kv_entries "
                "WHERE workspace_id=? AND key=? AND branch_id=?",
                (workspace_id, key, branch_id),
            )
        row = await cur.fetchone()
        await cur.close()
        return (row[0] or 0) + 1

    async def _next_file_version(
        self,
        conn: aiosqlite.Connection,
        workspace_id: str,
        path: str,
        branch_id: str | None,
    ) -> int:
        if branch_id is None:
            cur = await conn.execute(
                "SELECT MAX(version) FROM file_entries "
                "WHERE workspace_id=? AND path=? AND branch_id IS NULL",
                (workspace_id, path),
            )
        else:
            cur = await conn.execute(
                "SELECT MAX(version) FROM file_entries "
                "WHERE workspace_id=? AND path=? AND branch_id=?",
                (workspace_id, path, branch_id),
            )
        row = await cur.fetchone()
        await cur.close()
        return (row[0] or 0) + 1

    async def _latest_kv_row(
        self,
        conn: aiosqlite.Connection,
        workspace_id: str,
        key: str,
        branch_id: str | None,
    ) -> aiosqlite.Row | None:
        if branch_id is None:
            cur = await conn.execute(
                "SELECT * FROM kv_entries "
                "WHERE workspace_id=? AND key=? AND branch_id IS NULL "
                "ORDER BY version DESC LIMIT 1",
                (workspace_id, key),
            )
        else:
            cur = await conn.execute(
                "SELECT * FROM kv_entries "
                "WHERE workspace_id=? AND key=? AND branch_id=? "
                "ORDER BY version DESC LIMIT 1",
                (workspace_id, key, branch_id),
            )
        row = await cur.fetchone()
        await cur.close()
        return row

    async def _kv_row_at_version(
        self,
        conn: aiosqlite.Connection,
        workspace_id: str,
        key: str,
        version: int,
        branch_id: str | None,
    ) -> aiosqlite.Row | None:
        if branch_id is None:
            cur = await conn.execute(
                "SELECT * FROM kv_entries "
                "WHERE workspace_id=? AND key=? AND version=? AND branch_id IS NULL",
                (workspace_id, key, version),
            )
        else:
            cur = await conn.execute(
                "SELECT * FROM kv_entries "
                "WHERE workspace_id=? AND key=? AND version=? AND branch_id=?",
                (workspace_id, key, version, branch_id),
            )
        row = await cur.fetchone()
        await cur.close()
        return row

    async def _latest_file_row(
        self,
        conn: aiosqlite.Connection,
        workspace_id: str,
        path: str,
        branch_id: str | None,
    ) -> aiosqlite.Row | None:
        if branch_id is None:
            cur = await conn.execute(
                "SELECT * FROM file_entries "
                "WHERE workspace_id=? AND path=? AND branch_id IS NULL "
                "ORDER BY version DESC LIMIT 1",
                (workspace_id, path),
            )
        else:
            cur = await conn.execute(
                "SELECT * FROM file_entries "
                "WHERE workspace_id=? AND path=? AND branch_id=? "
                "ORDER BY version DESC LIMIT 1",
                (workspace_id, path, branch_id),
            )
        row = await cur.fetchone()
        await cur.close()
        return row

    async def _file_row_at_version(
        self,
        conn: aiosqlite.Connection,
        workspace_id: str,
        path: str,
        version: int,
        branch_id: str | None,
    ) -> aiosqlite.Row | None:
        if branch_id is None:
            cur = await conn.execute(
                "SELECT * FROM file_entries "
                "WHERE workspace_id=? AND path=? AND version=? AND branch_id IS NULL",
                (workspace_id, path, version),
            )
        else:
            cur = await conn.execute(
                "SELECT * FROM file_entries "
                "WHERE workspace_id=? AND path=? AND version=? AND branch_id=?",
                (workspace_id, path, version, branch_id),
            )
        row = await cur.fetchone()
        await cur.close()
        return row

    async def _all_kv_keys(
        self,
        conn: aiosqlite.Connection,
        workspace_id: str,
        ctx: _BranchContext,
    ) -> set[str]:
        keys: set[str] = set()
        if ctx.branch_id is None:
            cur = await conn.execute(
                "SELECT DISTINCT key FROM kv_entries "
                "WHERE workspace_id=? AND branch_id IS NULL",
                (workspace_id,),
            )
        else:
            cur = await conn.execute(
                "SELECT DISTINCT key FROM kv_entries "
                "WHERE workspace_id=? AND branch_id=?",
                (workspace_id, ctx.branch_id),
            )
        for row in await cur.fetchall():
            keys.add(row["key"])
        await cur.close()
        if ctx.branch_id is not None:
            keys.update(ctx.from_snapshot_kv.keys())
        return keys

    async def _all_file_paths(
        self,
        conn: aiosqlite.Connection,
        workspace_id: str,
        ctx: _BranchContext,
    ) -> set[str]:
        paths: set[str] = set()
        if ctx.branch_id is None:
            cur = await conn.execute(
                "SELECT DISTINCT path FROM file_entries "
                "WHERE workspace_id=? AND branch_id IS NULL",
                (workspace_id,),
            )
        else:
            cur = await conn.execute(
                "SELECT DISTINCT path FROM file_entries "
                "WHERE workspace_id=? AND branch_id=?",
                (workspace_id, ctx.branch_id),
            )
        for row in await cur.fetchall():
            paths.add(row["path"])
        await cur.close()
        if ctx.branch_id is not None:
            paths.update(ctx.from_snapshot_files.keys())
        return paths

    async def _touch_workspace(
        self,
        conn: aiosqlite.Connection,
        workspace_id: str,
        ts,
    ) -> None:
        await conn.execute(
            "UPDATE workspaces SET updated_at=? WHERE id=?",
            (iso(ts), workspace_id),
        )
