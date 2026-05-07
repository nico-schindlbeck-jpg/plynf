# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 The Plinth Authors
"""SQLite driver tests for the identity storage abstraction."""

from __future__ import annotations

from pathlib import Path

import pytest

from plinth_identity.settings import Settings
from plinth_identity.storage import create_database
from plinth_identity.storage._translate import (
    translate_placeholders_to_postgres,
)
from plinth_identity.storage.sqlite_driver import SQLiteDriver


@pytest.mark.asyncio()
async def test_sqlite_driver_init_schema(tmp_path: Path) -> None:
    db = SQLiteDriver(tmp_path / "id.sqlite")
    await db.connect()
    await db.init_schema()
    await db.init_schema()  # idempotent
    rows = await db.fetchall(
        "SELECT name FROM sqlite_master WHERE type='table' AND name=?",
        ("issued_tokens",),
    )
    assert rows == [("issued_tokens",)]
    await db.close()


@pytest.mark.asyncio()
async def test_sqlite_driver_crud(tmp_path: Path) -> None:
    db = SQLiteDriver(tmp_path / "id.sqlite")
    await db.init_schema()
    await db.execute(
        "INSERT INTO issued_tokens (jti, agent_id, tenant_id, workspace_id, "
        "scopes, issued_at, expires_at, revoked, revoked_at, metadata) "
        "VALUES (?, ?, 'default', NULL, '[]', '2026-01-01T00:00:00+00:00', "
        "'2026-01-02T00:00:00+00:00', 0, NULL, '{}')",
        ("jti_1", "agt"),
    )
    one = await db.fetchone(
        "SELECT jti, agent_id FROM issued_tokens WHERE jti=?", ("jti_1",)
    )
    assert one == ("jti_1", "agt")
    await db.close()


@pytest.mark.asyncio()
async def test_sqlite_driver_transaction_rolls_back(tmp_path: Path) -> None:
    db = SQLiteDriver(tmp_path / "id.sqlite")
    await db.init_schema()
    with pytest.raises(RuntimeError):
        async with db.transaction() as tx:
            await tx.execute(
                "INSERT INTO issued_tokens (jti, agent_id, tenant_id, "
                "workspace_id, scopes, issued_at, expires_at, revoked, "
                "revoked_at, metadata) VALUES (?, 'agt', 'default', NULL, "
                "'[]', '2026-01-01T00:00:00+00:00', "
                "'2026-01-02T00:00:00+00:00', 0, NULL, '{}')",
                ("rb",),
            )
            raise RuntimeError("trigger")
    rows = await db.fetchall("SELECT jti FROM issued_tokens")
    assert rows == []
    await db.close()


@pytest.mark.asyncio()
async def test_sqlite_driver_advisory_lock(tmp_path: Path) -> None:
    db = SQLiteDriver(tmp_path / "id.sqlite")
    await db.connect()
    assert await db.try_advisory_lock("k") is True
    assert await db.try_advisory_lock("k") is False
    await db.release_advisory_lock("k")
    assert await db.try_advisory_lock("k") is True
    await db.release_advisory_lock("k")
    await db.close()


def test_factory_default_is_sqlite(tmp_path: Path) -> None:
    settings = Settings(data_dir=tmp_path)
    db = create_database(settings)
    assert db.driver == "sqlite"


def test_factory_postgres_requires_url(tmp_path: Path) -> None:
    settings = Settings(data_dir=tmp_path, storage_driver="postgres")
    with pytest.raises(RuntimeError, match="DATABASE_URL"):
        create_database(settings)


def test_factory_uses_identity_url_override(tmp_path: Path) -> None:
    settings = Settings(
        data_dir=tmp_path,
        storage_driver="postgres",
        database_url="postgresql://shared@localhost/x",
        identity_database_url="postgresql://id@localhost/x",
    )
    assert settings.effective_database_url == "postgresql://id@localhost/x"


def test_translate_placeholders() -> None:
    assert translate_placeholders_to_postgres("a=? AND b=?") == "a=$1 AND b=$2"
    assert translate_placeholders_to_postgres("SELECT '?'") == "SELECT '?'"
