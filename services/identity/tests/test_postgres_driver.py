# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 The Plinth Authors
"""Postgres driver tests — opt-in via ``PLINTH_TEST_POSTGRES_URL``."""

from __future__ import annotations

import asyncio
import os
import uuid

import pytest

POSTGRES_URL = os.environ.get("PLINTH_TEST_POSTGRES_URL")

pytestmark = [
    pytest.mark.postgres,
    pytest.mark.skipif(
        not POSTGRES_URL,
        reason="PLINTH_TEST_POSTGRES_URL not set; skipping live Postgres tests.",
    ),
]


def _admin_url() -> str:
    assert POSTGRES_URL is not None  # noqa: S101
    if "/" in POSTGRES_URL.rsplit("@", 1)[-1]:
        head, _ = POSTGRES_URL.rsplit("/", 1)
        return f"{head}/postgres"
    return POSTGRES_URL


def _ephemeral_db_name() -> str:
    return f"plinth_id_test_{uuid.uuid4().hex[:12]}"


async def _create_db(name: str) -> None:
    import asyncpg

    conn = await asyncpg.connect(_admin_url())
    try:
        await conn.execute(f'CREATE DATABASE "{name}"')
    finally:
        await conn.close()


async def _drop_db(name: str) -> None:
    import asyncpg

    conn = await asyncpg.connect(_admin_url())
    try:
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
    name = _ephemeral_db_name()
    asyncio.run(_create_db(name))
    try:
        yield _url_for(name)
    finally:
        asyncio.run(_drop_db(name))


@pytest.mark.asyncio()
async def test_postgres_init_schema(ephemeral_db_url: str) -> None:
    from plinth_identity.storage.postgres_driver import PostgresDriver

    db = PostgresDriver(ephemeral_db_url, min_size=1, max_size=2)
    await db.connect()
    try:
        await db.init_schema()
        await db.init_schema()  # idempotent
        rows = await db.fetchall(
            "SELECT table_name FROM information_schema.tables "
            "WHERE table_schema='public' AND table_name='issued_tokens'"
        )
        assert rows == [("issued_tokens",)]
    finally:
        await db.close()


@pytest.mark.asyncio()
async def test_postgres_crud_roundtrip(ephemeral_db_url: str) -> None:
    from plinth_identity.storage.postgres_driver import PostgresDriver

    db = PostgresDriver(ephemeral_db_url, min_size=1, max_size=2)
    await db.connect()
    try:
        await db.init_schema()
        await db.execute(
            "INSERT INTO issued_tokens (jti, agent_id, tenant_id, "
            "workspace_id, scopes, issued_at, expires_at, revoked, "
            "revoked_at, metadata) VALUES (?, ?, 'default', NULL, '[]', "
            "now(), now() + interval '1 hour', 0, NULL, '{}')",
            ("jti_1", "agt"),
        )
        rows = await db.fetchall("SELECT jti FROM issued_tokens")
        assert rows == [("jti_1",)]
    finally:
        await db.close()


@pytest.mark.asyncio()
async def test_postgres_transaction_rolls_back(ephemeral_db_url: str) -> None:
    from plinth_identity.storage.postgres_driver import PostgresDriver

    db = PostgresDriver(ephemeral_db_url, min_size=1, max_size=2)
    await db.connect()
    try:
        await db.init_schema()
        with pytest.raises(RuntimeError):
            async with db.transaction() as tx:
                await tx.execute(
                    "INSERT INTO issued_tokens (jti, agent_id, tenant_id, "
                    "workspace_id, scopes, issued_at, expires_at, revoked, "
                    "revoked_at, metadata) VALUES (?, 'agt', 'default', "
                    "NULL, '[]', now(), now() + interval '1 hour', 0, "
                    "NULL, '{}')",
                    ("rb",),
                )
                raise RuntimeError("rollback trigger")
        rows = await db.fetchall("SELECT jti FROM issued_tokens")
        assert rows == []
    finally:
        await db.close()


@pytest.mark.asyncio()
async def test_postgres_advisory_lock(ephemeral_db_url: str) -> None:
    from plinth_identity.storage.postgres_driver import PostgresDriver

    db = PostgresDriver(ephemeral_db_url, min_size=2, max_size=4)
    await db.connect()
    try:
        assert await db.try_advisory_lock("id_lock") is True
        assert await db.try_advisory_lock("id_lock") is False
        await db.release_advisory_lock("id_lock")
        assert await db.try_advisory_lock("id_lock") is True
        await db.release_advisory_lock("id_lock")
    finally:
        await db.close()
