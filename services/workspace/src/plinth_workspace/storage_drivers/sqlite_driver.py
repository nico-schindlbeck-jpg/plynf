# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 The Plinth Authors
"""SQLite driver implementing :class:`Database`.

This wraps :mod:`aiosqlite` with one shared connection (matching
``plinth_gateway.db.Database`` behaviour, since SQLite serialises writes
anyway). Advisory locks are emulated with :class:`asyncio.Lock` instances —
which is enough for the workspace GC engine since SQLite already serialises
the actual writes.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import aiosqlite

from .schema import SQLITE_SCHEMA


class SQLiteDriver:
    """:class:`Database` implementation backed by ``aiosqlite``."""

    driver: str = "sqlite"

    def __init__(self, path: Path | str) -> None:
        self._path = Path(path)
        self._conn: aiosqlite.Connection | None = None
        self._write_lock = asyncio.Lock()
        # Per-key advisory locks (in-process — SQLite WAL serialises writes
        # anyway, so a process-local lock is enough for v0.4 scale.)
        self._advisory_locks: dict[str, asyncio.Lock] = {}
        self._held_advisory: set[str] = set()

    @property
    def path(self) -> Path:
        return self._path

    async def connect(self) -> None:
        if self._conn is not None:
            return
        self._path.parent.mkdir(parents=True, exist_ok=True)
        conn = await aiosqlite.connect(str(self._path))
        conn.row_factory = aiosqlite.Row
        await conn.execute("PRAGMA journal_mode=WAL")
        await conn.execute("PRAGMA foreign_keys=ON")
        self._conn = conn

    async def close(self) -> None:
        if self._conn is not None:
            await self._conn.close()
            self._conn = None

    async def init_schema(self) -> None:
        await self.connect()
        assert self._conn is not None  # noqa: S101
        await self._conn.executescript(SQLITE_SCHEMA)
        await self._conn.commit()

    async def execute(self, sql: str, params: tuple = ()) -> None:
        await self.connect()
        assert self._conn is not None  # noqa: S101
        async with self._write_lock:
            await self._conn.execute(sql, params)
            await self._conn.commit()

    async def executemany(self, sql: str, params_list: list[tuple]) -> None:
        await self.connect()
        assert self._conn is not None  # noqa: S101
        async with self._write_lock:
            await self._conn.executemany(sql, params_list)
            await self._conn.commit()

    async def fetchone(self, sql: str, params: tuple = ()) -> tuple | None:
        await self.connect()
        assert self._conn is not None  # noqa: S101
        async with self._conn.execute(sql, params) as cur:
            row = await cur.fetchone()
        return tuple(row) if row is not None else None

    async def fetchall(self, sql: str, params: tuple = ()) -> list[tuple]:
        await self.connect()
        assert self._conn is not None  # noqa: S101
        async with self._conn.execute(sql, params) as cur:
            rows = await cur.fetchall()
        return [tuple(r) for r in rows]

    @asynccontextmanager
    async def transaction(self) -> AsyncIterator[Any]:
        """Yield a transaction-scoped wrapper.

        ``aiosqlite`` autocommits between ``execute`` calls; we wrap a
        block in BEGIN/COMMIT here so callers in the GC engine can run
        multi-statement updates atomically. The yielded handle has the same
        ``execute``/``fetchone``/``fetchall``/``executemany`` interface as
        the driver, but bypasses the write-lock + autocommit so the txn
        body runs as a single unit.
        """

        await self.connect()
        assert self._conn is not None  # noqa: S101
        async with self._write_lock:
            await self._conn.execute("BEGIN")
            handle = _SQLiteTxnHandle(self._conn)
            try:
                yield handle
            except Exception:
                await self._conn.rollback()
                raise
            await self._conn.commit()

    async def try_advisory_lock(self, key: str) -> bool:
        """Try to acquire an in-process per-key lock without blocking."""

        lock = self._advisory_locks.setdefault(key, asyncio.Lock())
        if lock.locked():
            return False
        await lock.acquire()
        self._held_advisory.add(key)
        return True

    async def release_advisory_lock(self, key: str) -> None:
        if key not in self._held_advisory:
            return
        lock = self._advisory_locks.get(key)
        if lock is not None and lock.locked():
            lock.release()
        self._held_advisory.discard(key)


class _SQLiteTxnHandle:
    """Read/write handle that runs against the existing connection without
    re-acquiring the driver's write-lock or autocommitting.

    Lifetime is the body of :meth:`SQLiteDriver.transaction` only.
    """

    driver: str = "sqlite"

    def __init__(self, conn: aiosqlite.Connection) -> None:
        self._conn = conn

    async def execute(self, sql: str, params: tuple = ()) -> None:
        await self._conn.execute(sql, params)

    async def executemany(self, sql: str, params_list: list[tuple]) -> None:
        await self._conn.executemany(sql, params_list)

    async def fetchone(self, sql: str, params: tuple = ()) -> tuple | None:
        async with self._conn.execute(sql, params) as cur:
            row = await cur.fetchone()
        return tuple(row) if row is not None else None

    async def fetchall(self, sql: str, params: tuple = ()) -> list[tuple]:
        async with self._conn.execute(sql, params) as cur:
            rows = await cur.fetchall()
        return [tuple(r) for r in rows]


__all__ = ["SQLiteDriver"]
