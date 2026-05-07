# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 The Plinth Authors
"""SQLite driver tests for the gateway storage abstraction."""

from __future__ import annotations

from pathlib import Path

import pytest

from plinth_gateway.settings import Settings
from plinth_gateway.storage import create_database
from plinth_gateway.storage._translate import translate_placeholders_to_postgres
from plinth_gateway.storage.sqlite_driver import SQLiteDriver


@pytest.mark.asyncio()
async def test_sqlite_driver_init_schema(tmp_path: Path) -> None:
    db = SQLiteDriver(tmp_path / "g.sqlite")
    await db.connect()
    await db.init_schema()
    await db.init_schema()  # idempotent
    rows = await db.fetchall(
        "SELECT name FROM sqlite_master WHERE type='table' AND name=?",
        ("audit_events",),
    )
    assert rows == [("audit_events",)]
    await db.close()


@pytest.mark.asyncio()
async def test_sqlite_driver_crud(tmp_path: Path) -> None:
    db = SQLiteDriver(tmp_path / "g.sqlite")
    await db.init_schema()
    await db.execute(
        "INSERT INTO tools (tool_id, name, description, transport, endpoint, "
        "input_schema, output_schema, idempotent, side_effects, "
        "cache_ttl_seconds, auth_method, auth_config, tenant_id, "
        "created_at, updated_at) VALUES (?, ?, '', '', '', '{}', '{}', "
        "0, 'read', NULL, 'none', '{}', 'default', "
        "'2026-01-01T00:00:00+00:00', '2026-01-01T00:00:00+00:00')",
        ("t.fetch", "fetch"),
    )
    one = await db.fetchone("SELECT tool_id, name FROM tools WHERE tool_id=?", ("t.fetch",))
    assert one == ("t.fetch", "fetch")
    rows = await db.fetchall("SELECT tool_id FROM tools")
    assert rows == [("t.fetch",)]
    await db.close()


@pytest.mark.asyncio()
async def test_sqlite_driver_transaction_rolls_back(tmp_path: Path) -> None:
    db = SQLiteDriver(tmp_path / "g.sqlite")
    await db.init_schema()
    with pytest.raises(RuntimeError):
        async with db.transaction() as tx:
            await tx.execute(
                "INSERT INTO tools (tool_id, name, description, transport, "
                "endpoint, input_schema, output_schema, idempotent, "
                "side_effects, cache_ttl_seconds, auth_method, auth_config, "
                "tenant_id, created_at, updated_at) VALUES (?, 'n', '', "
                "'', '', '{}', '{}', 0, 'read', NULL, 'none', '{}', "
                "'default', '2026-01-01T00:00:00+00:00', "
                "'2026-01-01T00:00:00+00:00')",
                ("rollback",),
            )
            raise RuntimeError("trigger")
    rows = await db.fetchall("SELECT tool_id FROM tools")
    assert rows == []
    await db.close()


@pytest.mark.asyncio()
async def test_sqlite_driver_advisory_lock(tmp_path: Path) -> None:
    db = SQLiteDriver(tmp_path / "g.sqlite")
    await db.connect()
    assert await db.try_advisory_lock("k1") is True
    assert await db.try_advisory_lock("k1") is False
    await db.release_advisory_lock("k1")
    assert await db.try_advisory_lock("k1") is True
    await db.release_advisory_lock("k1")
    await db.close()


def test_factory_default_is_sqlite(tmp_path: Path) -> None:
    settings = Settings(data_dir=tmp_path)
    db = create_database(settings)
    assert db.driver == "sqlite"


def test_factory_postgres_requires_url(tmp_path: Path) -> None:
    settings = Settings(data_dir=tmp_path, storage_driver="postgres")
    with pytest.raises(RuntimeError, match="DATABASE_URL"):
        create_database(settings)


def test_factory_uses_gateway_url_override(tmp_path: Path) -> None:
    settings = Settings(
        data_dir=tmp_path,
        storage_driver="postgres",
        database_url="postgresql://shared@localhost/x",
        gateway_database_url="postgresql://gw@localhost/x",
    )
    assert settings.effective_database_url == "postgresql://gw@localhost/x"


def test_translate_placeholders() -> None:
    assert translate_placeholders_to_postgres("a=? AND b=?") == "a=$1 AND b=$2"
    assert translate_placeholders_to_postgres("SELECT '?'") == "SELECT '?'"
