# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 The Plinth Authors
"""Postgres driver implementing :class:`Database`.

Backed by :mod:`asyncpg` with a connection pool. SQL written in SQLite
``?``-placeholder style is translated to ``$1, $2, ...`` on the way to
the wire.

Advisory locks use ``pg_try_advisory_lock(hashtext(key))``. We hold the
session lock on a single dedicated connection per key for the lifetime
of the lock — that's the only way ``pg_advisory_unlock`` can find the
lock in Postgres.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

try:
    import asyncpg  # type: ignore[import-not-found]
except ImportError:  # pragma: no cover -- optional dep
    asyncpg = None  # type: ignore[assignment]

from ._translate import translate_placeholders_to_postgres
from .schema import POSTGRES_SCHEMA


def _normalise_dsn(database_url: str) -> str:
    """Strip ``postgresql+asyncpg://`` prefix to a vanilla DSN."""

    if database_url.startswith("postgresql+asyncpg://"):
        return "postgresql://" + database_url[len("postgresql+asyncpg://") :]
    return database_url


class PostgresDriver:
    """:class:`Database` implementation backed by ``asyncpg``."""

    driver: str = "postgres"

    def __init__(
        self,
        database_url: str,
        *,
        min_size: int = 5,
        max_size: int = 20,
    ) -> None:
        if asyncpg is None:  # pragma: no cover -- import guard
            raise RuntimeError(
                "asyncpg is not installed. Install it with: "
                "pip install 'asyncpg>=0.29'."
            )
        if not database_url:
            raise ValueError(
                "PostgresDriver requires a non-empty database_url. Set "
                "PLINTH_DATABASE_URL or the per-service override."
            )
        self._dsn = _normalise_dsn(database_url)
        self._min_size = max(1, int(min_size))
        self._max_size = max(self._min_size, int(max_size))
        self._pool: Any | None = None
        # Lock-holding connections, keyed by lock key. We pin one connection
        # per held lock so the matching unlock can find it.
        self._lock_conns: dict[str, Any] = {}

    async def connect(self) -> None:
        if self._pool is not None:
            return
        self._pool = await asyncpg.create_pool(
            self._dsn,
            min_size=self._min_size,
            max_size=self._max_size,
        )

    async def close(self) -> None:
        # Release any held advisory locks before closing.
        for key in list(self._lock_conns.keys()):
            await self.release_advisory_lock(key)
        if self._pool is not None:
            await self._pool.close()
            self._pool = None

    async def init_schema(self) -> None:
        await self.connect()
        assert self._pool is not None  # noqa: S101
        async with self._pool.acquire() as conn:
            # asyncpg's execute can run multi-statement scripts directly.
            await conn.execute(POSTGRES_SCHEMA)

    async def execute(self, sql: str, params: tuple = ()) -> None:
        await self.connect()
        assert self._pool is not None  # noqa: S101
        translated = translate_placeholders_to_postgres(sql)
        async with self._pool.acquire() as conn:
            await conn.execute(translated, *params)

    async def executemany(self, sql: str, params_list: list[tuple]) -> None:
        await self.connect()
        assert self._pool is not None  # noqa: S101
        translated = translate_placeholders_to_postgres(sql)
        async with self._pool.acquire() as conn:
            await conn.executemany(translated, params_list)

    async def fetchone(self, sql: str, params: tuple = ()) -> tuple | None:
        await self.connect()
        assert self._pool is not None  # noqa: S101
        translated = translate_placeholders_to_postgres(sql)
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(translated, *params)
        return tuple(row) if row is not None else None

    async def fetchall(self, sql: str, params: tuple = ()) -> list[tuple]:
        await self.connect()
        assert self._pool is not None  # noqa: S101
        translated = translate_placeholders_to_postgres(sql)
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(translated, *params)
        return [tuple(r) for r in rows]

    @asynccontextmanager
    async def transaction(self) -> AsyncIterator[Any]:
        """Yield a connection-scoped transaction wrapper.

        Inside the with-block, callers must use the yielded ``TxnHandle``
        rather than the driver itself, because the txn lives on a single
        pinned connection.
        """

        await self.connect()
        assert self._pool is not None  # noqa: S101
        async with self._pool.acquire() as conn:
            async with conn.transaction():
                yield _TxnHandle(conn)

    async def try_advisory_lock(self, key: str) -> bool:
        await self.connect()
        assert self._pool is not None  # noqa: S101
        if key in self._lock_conns:
            # Same process already holds it. Return False so callers see
            # contention exactly as they would with another process.
            return False
        # Take a dedicated connection and pin it for the lifetime of the
        # lock. ``hashtext`` collapses arbitrary strings to a 32-bit int —
        # collisions are possible in theory but harmless because the lock
        # is just a coarse mutex.
        conn = await self._pool.acquire()
        try:
            row = await conn.fetchrow(
                "SELECT pg_try_advisory_lock(hashtext($1))", key
            )
        except Exception:
            await self._pool.release(conn)
            raise
        acquired = bool(row[0]) if row is not None else False
        if not acquired:
            await self._pool.release(conn)
            return False
        self._lock_conns[key] = conn
        return True

    async def release_advisory_lock(self, key: str) -> None:
        conn = self._lock_conns.pop(key, None)
        if conn is None:
            return
        try:
            await conn.fetchrow(
                "SELECT pg_advisory_unlock(hashtext($1))", key
            )
        finally:
            assert self._pool is not None  # noqa: S101
            await self._pool.release(conn)


class _TxnHandle:
    """Adapter wrapping an asyncpg connection inside a transaction."""

    driver: str = "postgres"

    def __init__(self, conn: Any) -> None:
        self._conn = conn

    async def execute(self, sql: str, params: tuple = ()) -> None:
        await self._conn.execute(translate_placeholders_to_postgres(sql), *params)

    async def executemany(self, sql: str, params_list: list[tuple]) -> None:
        await self._conn.executemany(
            translate_placeholders_to_postgres(sql), params_list
        )

    async def fetchone(self, sql: str, params: tuple = ()) -> tuple | None:
        row = await self._conn.fetchrow(
            translate_placeholders_to_postgres(sql), *params
        )
        return tuple(row) if row is not None else None

    async def fetchall(self, sql: str, params: tuple = ()) -> list[tuple]:
        rows = await self._conn.fetch(
            translate_placeholders_to_postgres(sql), *params
        )
        return [tuple(r) for r in rows]


__all__ = ["PostgresDriver"]
