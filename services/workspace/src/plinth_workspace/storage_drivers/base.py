# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 The Plinth Authors
"""Abstract database protocol shared by SQLite + Postgres drivers.

The protocol is deliberately minimal — just enough for callers (notably the
GC engine and any future shared-storage code) to perform CRUD without caring
about the underlying engine. SQL strings are written with ``?`` placeholders
in SQLite style; the Postgres driver translates them to ``$1, $2, ...``.

Type translations between drivers (see CONTRACTS v0.4):

    TEXT      -> TEXT
    INTEGER   -> BIGINT (where overflow is plausible)
    TIMESTAMP -> TIMESTAMPTZ
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import AbstractAsyncContextManager
from typing import Any, Protocol, runtime_checkable


@runtime_checkable
class Database(Protocol):
    """Minimal async DB interface used by Plinth services."""

    @property
    def driver(self) -> str:
        """Driver name — ``"sqlite"`` or ``"postgres"``."""

    async def connect(self) -> None:
        """Open the connection pool (idempotent)."""

    async def close(self) -> None:
        """Tear down the connection pool (idempotent)."""

    async def init_schema(self) -> None:
        """Apply ``CREATE TABLE IF NOT EXISTS`` style schema. Idempotent."""

    async def execute(self, sql: str, params: tuple = ()) -> None:
        """Run a write statement with positional ``?`` params."""

    async def executemany(self, sql: str, params_list: list[tuple]) -> None:
        """Run the same statement once per param tuple."""

    async def fetchone(self, sql: str, params: tuple = ()) -> tuple | None:
        """Fetch a single row as a positional tuple, or ``None`` if empty."""

    async def fetchall(self, sql: str, params: tuple = ()) -> list[tuple]:
        """Fetch all rows as positional tuples."""

    def transaction(self) -> AbstractAsyncContextManager[Any]:
        """Async context manager that wraps multiple ops in a transaction."""

    async def try_advisory_lock(self, key: str) -> bool:
        """Attempt to acquire a per-process advisory lock keyed by ``key``.

        Returns ``True`` if acquired, ``False`` if already held. Implementations
        must be safe to call repeatedly within the same process.
        """

    async def release_advisory_lock(self, key: str) -> None:
        """Release a previously-acquired advisory lock. Idempotent."""


# Re-exported helper for type hints elsewhere.
AsyncRowIterator = AsyncIterator[tuple]


__all__ = ["AsyncRowIterator", "Database"]
