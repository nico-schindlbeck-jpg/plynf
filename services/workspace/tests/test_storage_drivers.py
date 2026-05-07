# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 The Plinth Authors
"""Tests for the SQLite driver in :mod:`plinth_workspace.storage_drivers`.

These exercise the abstract ``Database`` API end-to-end. The Postgres
counterpart in ``test_postgres_driver.py`` runs the same scenarios — we
share the parametrised cases via the ``DRIVER_CASES`` matrix.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from plinth_workspace.settings import Settings
from plinth_workspace.storage_drivers import create_database
from plinth_workspace.storage_drivers._translate import (
    translate_placeholders_to_postgres,
)
from plinth_workspace.storage_drivers.sqlite_driver import SQLiteDriver


@pytest.mark.asyncio()
async def test_sqlite_driver_init_schema(tmp_path: Path) -> None:
    db = SQLiteDriver(tmp_path / "db.sqlite")
    await db.connect()
    await db.init_schema()
    # idempotent
    await db.init_schema()
    # tables exist
    rows = await db.fetchall(
        "SELECT name FROM sqlite_master WHERE type='table' AND name=?",
        ("retention_policies",),
    )
    assert rows == [("retention_policies",)]
    await db.close()


@pytest.mark.asyncio()
async def test_sqlite_driver_crud_roundtrip(tmp_path: Path) -> None:
    db = SQLiteDriver(tmp_path / "db.sqlite")
    await db.init_schema()

    await db.execute(
        "INSERT INTO workspaces (id, name, metadata, tenant_id, created_at, updated_at) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        ("ws_1", "n1", "{}", "default", "2026-01-01T00:00:00+00:00",
         "2026-01-01T00:00:00+00:00"),
    )
    one = await db.fetchone("SELECT id, name FROM workspaces WHERE id=?", ("ws_1",))
    assert one is not None
    assert one[0] == "ws_1"
    assert one[1] == "n1"

    rows = await db.fetchall("SELECT id FROM workspaces")
    assert rows == [("ws_1",)]

    await db.executemany(
        "INSERT INTO workspaces (id, name, metadata, tenant_id, created_at, updated_at) "
        "VALUES (?, ?, '{}', 'default', '2026-01-01T00:00:00+00:00', "
        "'2026-01-01T00:00:00+00:00')",
        [("ws_2", "n2"), ("ws_3", "n3")],
    )
    rows = await db.fetchall("SELECT id FROM workspaces ORDER BY id")
    assert [r[0] for r in rows] == ["ws_1", "ws_2", "ws_3"]
    await db.close()


@pytest.mark.asyncio()
async def test_sqlite_driver_transaction_rolls_back(tmp_path: Path) -> None:
    db = SQLiteDriver(tmp_path / "db.sqlite")
    await db.init_schema()

    with pytest.raises(RuntimeError):
        async with db.transaction() as tx:
            await tx.execute(
                "INSERT INTO workspaces (id, name, metadata, tenant_id, "
                "created_at, updated_at) VALUES (?, 'n', '{}', 'default', "
                "'2026-01-01T00:00:00+00:00', '2026-01-01T00:00:00+00:00')",
                ("ws_x",),
            )
            raise RuntimeError("boom")

    rows = await db.fetchall("SELECT id FROM workspaces")
    assert rows == []
    await db.close()


@pytest.mark.asyncio()
async def test_sqlite_driver_advisory_lock_contention(tmp_path: Path) -> None:
    db = SQLiteDriver(tmp_path / "db.sqlite")
    await db.connect()

    assert await db.try_advisory_lock("workspace_id_1") is True
    # Re-entering the same key from the same driver fails — that's the
    # intended GC semantics.
    assert await db.try_advisory_lock("workspace_id_1") is False
    # A different key is fine.
    assert await db.try_advisory_lock("workspace_id_2") is True
    await db.release_advisory_lock("workspace_id_1")
    # Now the original key is free again.
    assert await db.try_advisory_lock("workspace_id_1") is True
    await db.release_advisory_lock("workspace_id_1")
    await db.release_advisory_lock("workspace_id_2")
    # Releasing an un-held lock is a no-op.
    await db.release_advisory_lock("workspace_id_3")
    await db.close()


def test_factory_default_is_sqlite(tmp_path: Path) -> None:
    settings = Settings(data_dir=tmp_path)
    db = create_database(settings)
    assert db.driver == "sqlite"


def test_factory_rejects_unknown_driver(tmp_path: Path) -> None:
    settings = Settings(data_dir=tmp_path, storage_driver="sqlite")
    # Manually mutate to bypass Pydantic's literal validation.
    object.__setattr__(settings, "storage_driver", "mongo")
    with pytest.raises(ValueError, match="Unknown PLINTH_STORAGE_DRIVER"):
        create_database(settings)


def test_factory_postgres_requires_url(tmp_path: Path) -> None:
    settings = Settings(data_dir=tmp_path, storage_driver="postgres")
    with pytest.raises(RuntimeError, match="DATABASE_URL"):
        create_database(settings)


def test_factory_uses_workspace_url_override(tmp_path: Path) -> None:
    settings = Settings(
        data_dir=tmp_path,
        storage_driver="postgres",
        database_url="postgresql://shared@localhost/x",
        workspace_database_url="postgresql://specific@localhost/x",
    )
    # Don't actually connect — just resolve the URL.
    assert (
        settings.effective_database_url == "postgresql://specific@localhost/x"
    )


def test_translate_placeholders_simple() -> None:
    assert translate_placeholders_to_postgres("a=?") == "a=$1"
    assert translate_placeholders_to_postgres("a=? AND b=?") == "a=$1 AND b=$2"


def test_translate_placeholders_ignores_quoted_qmark() -> None:
    assert translate_placeholders_to_postgres("SELECT '?'") == "SELECT '?'"
    assert (
        translate_placeholders_to_postgres("SELECT '?' WHERE a=?")
        == "SELECT '?' WHERE a=$1"
    )


def test_translate_placeholders_handles_double_quoted() -> None:
    assert (
        translate_placeholders_to_postgres('SELECT "col?", ? FROM t')
        == 'SELECT "col?", $1 FROM t'
    )


def test_translate_placeholders_handles_escaped_quote() -> None:
    assert (
        translate_placeholders_to_postgres("SELECT 'it''s', ? FROM t")
        == "SELECT 'it''s', $1 FROM t"
    )
