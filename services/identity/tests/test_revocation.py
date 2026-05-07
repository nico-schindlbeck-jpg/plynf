# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 The Plinth Authors
"""Unit tests for the in-memory revocation list + store revocation flow."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from plinth_identity.exceptions import TokenNotFound
from plinth_identity.revocation import RevocationList
from plinth_identity.store import TokenStore

UTC = timezone.utc


def test_revocation_list_starts_empty():
    rl = RevocationList()
    assert len(rl) == 0
    assert "jti_x" not in rl


def test_revocation_list_add_and_contains():
    rl = RevocationList()
    rl.add("jti_a")
    assert rl.contains("jti_a")
    assert "jti_a" in rl
    assert not rl.contains("jti_b")


def test_revocation_list_replace():
    rl = RevocationList({"jti_a", "jti_b"})
    assert len(rl) == 2
    n = rl.replace({"jti_c"})
    assert n == 1
    assert "jti_c" in rl
    assert "jti_a" not in rl


@pytest.mark.asyncio
async def test_store_revoke_round_trips(store: TokenStore):
    issued_at = datetime.now(UTC).replace(microsecond=0)
    expires_at = issued_at + timedelta(hours=1)
    info = await store.insert(
        jti="jti_rev1",
        agent_id="agt_1",
        tenant_id="t",
        workspace_id=None,
        scopes=["x"],
        issued_at=issued_at,
        expires_at=expires_at,
    )
    assert info.revoked is False

    revoked_info = await store.revoke("jti_rev1")
    assert revoked_info.revoked is True
    assert revoked_info.revoked_at is not None
    assert await store.is_revoked("jti_rev1")


@pytest.mark.asyncio
async def test_store_revoke_unknown_raises(store: TokenStore):
    with pytest.raises(TokenNotFound):
        await store.revoke("jti_missing")


@pytest.mark.asyncio
async def test_store_is_revoked_uses_cache(store: TokenStore):
    issued_at = datetime.now(UTC).replace(microsecond=0)
    expires_at = issued_at + timedelta(hours=1)
    await store.insert(
        jti="jti_cache",
        agent_id="a",
        tenant_id="t",
        workspace_id=None,
        scopes=[],
        issued_at=issued_at,
        expires_at=expires_at,
    )
    # First call populates the cache; second call is a pure in-memory lookup.
    assert await store.is_revoked("jti_cache") is False
    await store.revoke("jti_cache")
    assert await store.is_revoked("jti_cache") is True


@pytest.mark.asyncio
async def test_store_reload_cache_after_external_revoke(store: TokenStore):
    issued_at = datetime.now(UTC).replace(microsecond=0)
    expires_at = issued_at + timedelta(hours=1)
    await store.insert(
        jti="jti_reload",
        agent_id="a",
        tenant_id="t",
        workspace_id=None,
        scopes=[],
        issued_at=issued_at,
        expires_at=expires_at,
    )
    # Warm the cache
    assert await store.is_revoked("jti_reload") is False

    # Simulate another instance writing the revoke directly to SQLite.
    import json

    import aiosqlite

    async with aiosqlite.connect(store.db_path) as conn:
        await conn.execute(
            "UPDATE issued_tokens SET revoked=1, revoked_at=? WHERE jti=?",
            (datetime.now(UTC).isoformat(), "jti_reload"),
        )
        await conn.commit()

    # Cache is stale → still says False.
    assert await store.is_revoked("jti_reload") is False

    # Reload picks up the change.
    n = await store.reload_cache()
    assert n >= 1
    assert await store.is_revoked("jti_reload") is True

    # Touch json for lint coverage (it's the persistence backbone).
    json.dumps({})


@pytest.mark.asyncio
async def test_revoked_jtis_returns_set(store: TokenStore):
    issued_at = datetime.now(UTC).replace(microsecond=0)
    expires_at = issued_at + timedelta(hours=1)
    await store.insert(
        jti="jti_a",
        agent_id="a",
        tenant_id="t",
        workspace_id=None,
        scopes=[],
        issued_at=issued_at,
        expires_at=expires_at,
    )
    await store.insert(
        jti="jti_b",
        agent_id="a",
        tenant_id="t",
        workspace_id=None,
        scopes=[],
        issued_at=issued_at,
        expires_at=expires_at,
    )
    await store.revoke("jti_a")
    revoked = await store.revoked_jtis()
    assert "jti_a" in revoked
    assert "jti_b" not in revoked


@pytest.mark.asyncio
async def test_idempotent_revoke(store: TokenStore):
    issued_at = datetime.now(UTC).replace(microsecond=0)
    expires_at = issued_at + timedelta(hours=1)
    await store.insert(
        jti="jti_idem",
        agent_id="a",
        tenant_id="t",
        workspace_id=None,
        scopes=[],
        issued_at=issued_at,
        expires_at=expires_at,
    )
    info1 = await store.revoke("jti_idem")
    info2 = await store.revoke("jti_idem")
    assert info1.revoked_at == info2.revoked_at  # second revoke preserves first ts
