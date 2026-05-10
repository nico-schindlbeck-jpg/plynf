# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 The Plinth Authors
"""Tests for the v1.1 coordination backend (identity service).

Same surface as the gateway tests, plus an integration check that ``revoke``
propagates the JTI into the cluster-shared set so a peer replica's
``is_revoked`` returns True without a polling cycle.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest

from plinth_identity.coordination import (
    CoordinationBackend,
    MemoryBackend,
    RedisBackend,
    make_coordination_backend,
)
from plinth_identity.store import TokenStore, init_db

UTC = timezone.utc


# ---------------------------------------------------------------------------
# Fixtures


def _fake_async_redis() -> Any:
    import fakeredis.aioredis

    return fakeredis.aioredis.FakeRedis(decode_responses=True)


def _fakeredis_with_server(server: Any) -> Any:
    import fakeredis.aioredis

    return fakeredis.aioredis.FakeRedis(server=server, decode_responses=True)


@pytest.fixture
def memory_backend() -> MemoryBackend:
    return MemoryBackend()


@pytest.fixture
def redis_backend() -> RedisBackend:
    return RedisBackend(
        "redis://localhost:6379/0",
        key_prefix="plinth-identity-test",
        client=_fake_async_redis(),
    )


@pytest.fixture(params=["memory", "redis"])
def any_backend(request) -> CoordinationBackend:
    if request.param == "memory":
        return MemoryBackend()
    return RedisBackend(
        "redis://localhost:6379/0",
        key_prefix="plinth-identity-test",
        client=_fake_async_redis(),
    )


# ---------------------------------------------------------------------------
# Round-trip


@pytest.mark.asyncio
async def test_set_then_get(any_backend: CoordinationBackend) -> None:
    await any_backend.set("k", "v")
    assert await any_backend.get("k") == "v"


@pytest.mark.asyncio
async def test_delete(any_backend: CoordinationBackend) -> None:
    await any_backend.set("k", "v")
    await any_backend.delete("k")
    assert await any_backend.get("k") is None


@pytest.mark.asyncio
async def test_incr_round_trip(any_backend: CoordinationBackend) -> None:
    assert await any_backend.incr("c", amount=2) == 2
    assert await any_backend.incr("c", amount=3) == 5


@pytest.mark.asyncio
async def test_set_and_member(any_backend: CoordinationBackend) -> None:
    await any_backend.add_to_set("s", "a")
    await any_backend.add_to_set("s", "b")
    assert await any_backend.is_member("s", "a")
    assert sorted(await any_backend.members("s")) == ["a", "b"]


@pytest.mark.asyncio
async def test_lock_basic(any_backend: CoordinationBackend) -> None:
    assert await any_backend.acquire_lock("lk", "h1", ttl_seconds=5) is True
    assert await any_backend.acquire_lock("lk", "h2", ttl_seconds=5) is False
    assert await any_backend.release_lock("lk", "h1") is True


@pytest.mark.asyncio
async def test_health(any_backend: CoordinationBackend) -> None:
    assert await any_backend.health() is True


# ---------------------------------------------------------------------------
# Factory


def test_factory_default_is_memory() -> None:
    class _S:
        coordination_backend = "memory"
        coordination_redis_url = "redis://x/0"
        coordination_key_prefix = "plinth"

    assert isinstance(make_coordination_backend(_S()), MemoryBackend)


def test_factory_redis_when_configured() -> None:
    class _S:
        coordination_backend = "redis"
        coordination_redis_url = "redis://x/0"
        coordination_key_prefix = "plinth"

    assert isinstance(make_coordination_backend(_S()), RedisBackend)


# ---------------------------------------------------------------------------
# Integration — TokenStore.revoke pushes to coordination set


async def _seed_token(store: TokenStore, jti: str) -> None:
    await store.insert(
        jti=jti,
        agent_id="agent_test",
        tenant_id="default",
        workspace_id=None,
        scopes=["read"],
        issued_at=datetime.now(UTC),
        expires_at=datetime.now(UTC) + timedelta(hours=1),
    )


@pytest.mark.asyncio
async def test_revoke_propagates_to_coordination_set(tmp_path: Path) -> None:
    """A revoke writes into the in-memory coordination set so a peer
    replica querying ``is_member`` sees it.
    """

    db_path = tmp_path / "ident.db"
    await init_db(db_path)
    backend = MemoryBackend()
    store = TokenStore(db_path, coordination=backend)
    await _seed_token(store, "jti_abc")

    await store.revoke("jti_abc")
    assert await backend.is_member("revoked_jtis", "jti_abc") is True


@pytest.mark.asyncio
async def test_is_revoked_consults_coordination_on_cache_miss(
    tmp_path: Path,
) -> None:
    """A token revoked on a peer (= present in the shared set, *not* in
    the local cache) is reported as revoked here on the next ``is_revoked``.
    """

    db_path = tmp_path / "ident.db"
    await init_db(db_path)
    backend = MemoryBackend()
    store = TokenStore(db_path, coordination=backend)
    # Warm the local cache.
    await store.reload_cache()
    # Simulate peer replica revoking jti_xyz.
    await backend.add_to_set("revoked_jtis", "jti_xyz")

    assert await store.is_revoked("jti_xyz") is True


@pytest.mark.asyncio
async def test_is_revoked_returns_false_for_unknown_jti(tmp_path: Path) -> None:
    db_path = tmp_path / "ident.db"
    await init_db(db_path)
    backend = MemoryBackend()
    store = TokenStore(db_path, coordination=backend)
    await store.reload_cache()
    assert await store.is_revoked("never_seen_jti") is False


@pytest.mark.asyncio
async def test_revoke_propagates_across_redis_replicas(tmp_path: Path) -> None:
    """Two ``TokenStore`` instances each holding a ``RedisBackend`` against
    the same fake Redis server: a revoke on store A is seen by store B
    immediately via the shared set (no polling required).
    """

    import fakeredis

    server = fakeredis.FakeServer()
    backend_a = RedisBackend(
        "redis://x/0",
        key_prefix="plinth-id-rev",
        client=_fakeredis_with_server(server),
    )
    backend_b = RedisBackend(
        "redis://x/0",
        key_prefix="plinth-id-rev",
        client=_fakeredis_with_server(server),
    )

    db_a = tmp_path / "a.db"
    db_b = tmp_path / "b.db"
    await init_db(db_a)
    await init_db(db_b)
    store_a = TokenStore(db_a, coordination=backend_a)
    store_b = TokenStore(db_b, coordination=backend_b)

    await _seed_token(store_a, "jti_shared")
    await store_b.reload_cache()

    await store_a.revoke("jti_shared")
    # Peer replica picks it up via the shared set even though the SQLite
    # row only exists in db_a.
    assert await store_b.is_revoked("jti_shared") is True


# ---------------------------------------------------------------------------
# Concurrency


@pytest.mark.asyncio
async def test_lock_concurrent_acquires(memory_backend: MemoryBackend) -> None:
    results: list[bool] = []

    async def attempt(holder: str) -> None:
        ok = await memory_backend.acquire_lock("k", holder, ttl_seconds=10)
        results.append(ok)

    await asyncio.gather(*(attempt(f"h{i}") for i in range(8)))
    assert results.count(True) == 1
    assert results.count(False) == 7


# ---------------------------------------------------------------------------
# aclose is safe


@pytest.mark.asyncio
async def test_aclose_safe(any_backend: CoordinationBackend) -> None:
    await any_backend.aclose()
