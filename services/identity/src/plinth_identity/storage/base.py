# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 The Plinth Authors
"""Abstract database protocol shared by SQLite + Postgres drivers."""

from __future__ import annotations

from contextlib import AbstractAsyncContextManager
from typing import Any, Protocol, runtime_checkable


@runtime_checkable
class Database(Protocol):
    """Minimal async DB interface used by Plinth services."""

    @property
    def driver(self) -> str:
        """``"sqlite"`` or ``"postgres"``."""

    async def connect(self) -> None: ...
    async def close(self) -> None: ...
    async def init_schema(self) -> None: ...

    async def execute(self, sql: str, params: tuple = ()) -> None: ...
    async def executemany(self, sql: str, params_list: list[tuple]) -> None: ...
    async def fetchone(self, sql: str, params: tuple = ()) -> tuple | None: ...
    async def fetchall(self, sql: str, params: tuple = ()) -> list[tuple]: ...

    def transaction(self) -> AbstractAsyncContextManager[Any]: ...

    async def try_advisory_lock(self, key: str) -> bool: ...
    async def release_advisory_lock(self, key: str) -> None: ...


__all__ = ["Database"]
