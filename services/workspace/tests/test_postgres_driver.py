# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 The Plinth Authors
"""Postgres driver tests — opt-in via ``PLINTH_TEST_POSTGRES_URL``.

Skipped by default. To run:

    docker run -d --name plinth-pg-test -p 5433:5432 \\
        -e POSTGRES_PASSWORD=test postgres:16
    PLINTH_TEST_POSTGRES_URL=postgresql://postgres:test@localhost:5433/plinth_test \\
        pytest -m postgres -q

Each test creates and drops a uniquely-named database so they don't interact.
"""

from __future__ import annotations

import asyncio
import os
import uuid

import pytest

POSTGRES_URL = os.environ.get("PLINTH_TEST_POSTGRES_URL")

# Mark every test in this module as requiring a Postgres instance — and skip
# the entire module when the env var is missing so a default ``pytest`` run
# is still green for SQLite-only contributors.
pytestmark = [
    pytest.mark.postgres,
    pytest.mark.skipif(
        not POSTGRES_URL,
        reason="PLINTH_TEST_POSTGRES_URL not set; skipping live Postgres tests.",
    ),
]


def _admin_url() -> str:
    """URL pointing at the default ``postgres`` database (for CREATE DATABASE)."""

    assert POSTGRES_URL is not None  # noqa: S101 -- guarded by skipif
    # Replace the trailing path component (the db name) with /postgres.
    if "/" in POSTGRES_URL.rsplit("@", 1)[-1]:
        head, _ = POSTGRES_URL.rsplit("/", 1)
        return f"{head}/postgres"
    return POSTGRES_URL


def _ephemeral_db_name() -> str:
    return f"plinth_test_{uuid.uuid4().hex[:12]}"


async def _create_db(name: str) -> None:
    import asyncpg  # local import — only needed when the marker runs

    conn = await asyncpg.connect(_admin_url())
    try:
        await conn.execute(f'CREATE DATABASE "{name}"')
    finally:
        await conn.close()


async def _drop_db(name: str) -> None:
    import asyncpg

    conn = await asyncpg.connect(_admin_url())
    try:
        # Make sure no leftover connections block the drop.
        await conn.execute(
            "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
            "WHERE datname=$1 AND pid <> pg_backend_pid()",
            name,
        )
        await conn.execute(f'DROP DATABASE IF EXISTS "{name}"')
    finally:
        await conn.close()


def _url_for(db_name: str) -> str:
    assert POSTGRES_URL is not None  # noqa: S101
    head, _ = POSTGRES_URL.rsplit("/", 1)
    return f"{head}/{db_name}"


@pytest.fixture()
def ephemeral_db_url() -> str:
    """Yield a freshly-created Postgres DB URL; drops it on teardown."""

    name = _ephemeral_db_name()
    asyncio.get_event_loop().run_until_complete(_create_db(name))
    try:
        yield _url_for(name)
    finally:
        asyncio.get_event_loop().run_until_complete(_drop_db(name))


@pytest.mark.asyncio()
async def test_postgres_init_schema(ephemeral_db_url: str) -> None:
    from plinth_workspace.storage_drivers.postgres_driver import PostgresDriver

    db = PostgresDriver(ephemeral_db_url, min_size=1, max_size=2)
    await db.connect()
    try:
        await db.init_schema()
        # Idempotent.
        await db.init_schema()
        rows = await db.fetchall(
            "SELECT table_name FROM information_schema.tables "
            "WHERE table_schema='public' AND table_name='retention_policies'"
        )
        assert rows == [("retention_policies",)]
    finally:
        await db.close()


@pytest.mark.asyncio()
async def test_postgres_crud_roundtrip(ephemeral_db_url: str) -> None:
    from plinth_workspace.storage_drivers.postgres_driver import PostgresDriver

    db = PostgresDriver(ephemeral_db_url, min_size=1, max_size=2)
    await db.connect()
    try:
        await db.init_schema()
        await db.execute(
            "INSERT INTO workspaces (id, name, metadata, tenant_id, "
            "created_at, updated_at) VALUES (?, ?, ?, ?, "
            "now(), now())",
            ("ws_a", "n", "{}", "default"),
        )
        row = await db.fetchone(
            "SELECT id, name FROM workspaces WHERE id=?", ("ws_a",)
        )
        assert row is not None
        assert row[0] == "ws_a"
        assert row[1] == "n"
        rows = await db.fetchall("SELECT id FROM workspaces ORDER BY id")
        assert [r[0] for r in rows] == ["ws_a"]
    finally:
        await db.close()


@pytest.mark.asyncio()
async def test_postgres_transaction_rolls_back(ephemeral_db_url: str) -> None:
    from plinth_workspace.storage_drivers.postgres_driver import PostgresDriver

    db = PostgresDriver(ephemeral_db_url, min_size=1, max_size=2)
    await db.connect()
    try:
        await db.init_schema()
        with pytest.raises(RuntimeError):
            async with db.transaction() as tx:
                await tx.execute(
                    "INSERT INTO workspaces (id, name, metadata, tenant_id, "
                    "created_at, updated_at) VALUES (?, 'n', '{}', 'default', "
                    "now(), now())",
                    ("ws_z",),
                )
                raise RuntimeError("rollback trigger")
        rows = await db.fetchall("SELECT id FROM workspaces")
        assert rows == []
    finally:
        await db.close()


@pytest.mark.asyncio()
async def test_postgres_advisory_lock(ephemeral_db_url: str) -> None:
    from plinth_workspace.storage_drivers.postgres_driver import PostgresDriver

    db = PostgresDriver(ephemeral_db_url, min_size=2, max_size=4)
    await db.connect()
    try:
        assert await db.try_advisory_lock("ws_lock_1") is True
        # Re-attempting from same driver is treated as contention.
        assert await db.try_advisory_lock("ws_lock_1") is False
        # Different key is fine.
        assert await db.try_advisory_lock("ws_lock_2") is True
        await db.release_advisory_lock("ws_lock_1")
        assert await db.try_advisory_lock("ws_lock_1") is True
        await db.release_advisory_lock("ws_lock_1")
        await db.release_advisory_lock("ws_lock_2")
    finally:
        await db.close()
