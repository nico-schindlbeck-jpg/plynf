# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 The Plinth Authors
"""Tests for the v1.1 coordination backend (workspace service).

Mirrors the gateway/identity coordination tests so each service can be
verified in isolation. The workspace surface area uses the backend for
lease coordination + the cluster-shared revocation cache; this file
exercises the protocol-level operations that those callers depend on.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from plinth_workspace.coordination import (
    CoordinationBackend,
    MemoryBackend,
    RedisBackend,
    make_coordination_backend,
)


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
        key_prefix="plinth-workspace-test",
        client=_fake_async_redis(),
    )


@pytest.fixture(params=["memory", "redis"])
def any_backend(request) -> CoordinationBackend:
    if request.param == "memory":
        return MemoryBackend()
    return RedisBackend(
        "redis://localhost:6379/0",
        key_prefix="plinth-workspace-test",
        client=_fake_async_redis(),
    )


# ---------------------------------------------------------------------------
# Round-trip


@pytest.mark.asyncio
async def test_get_set(any_backend: CoordinationBackend) -> None:
    await any_backend.set("k", "v")
    assert await any_backend.get("k") == "v"


@pytest.mark.asyncio
async def test_delete(any_backend: CoordinationBackend) -> None:
    await any_backend.set("k", "v")
    await any_backend.delete("k")
    assert await any_backend.get("k") is None


@pytest.mark.asyncio
async def test_incr(any_backend: CoordinationBackend) -> None:
    assert await any_backend.incr("c") == 1
    assert await any_backend.incr("c", amount=4) == 5


@pytest.mark.asyncio
async def test_set_and_member(any_backend: CoordinationBackend) -> None:
    await any_backend.add_to_set("s", "a")
    assert await any_backend.is_member("s", "a") is True
    assert await any_backend.is_member("s", "b") is False


@pytest.mark.asyncio
async def test_members_returns_list(any_backend: CoordinationBackend) -> None:
    for v in ("a", "b", "c"):
        await any_backend.add_to_set("s", v)
    out = sorted(await any_backend.members("s"))
    assert out == ["a", "b", "c"]


@pytest.mark.asyncio
async def test_health(any_backend: CoordinationBackend) -> None:
    assert await any_backend.health() is True


# ---------------------------------------------------------------------------
# Lock — workspace's primary use case (replaces fcntl per CONTRACTS.md)


@pytest.mark.asyncio
async def test_lock_acquire_release(any_backend: CoordinationBackend) -> None:
    assert await any_backend.acquire_lock("lease:w1", "h1", ttl_seconds=5) is True
    assert await any_backend.acquire_lock("lease:w1", "h2", ttl_seconds=5) is False
    assert await any_backend.release_lock("lease:w1", "h2") is False
    assert await any_backend.release_lock("lease:w1", "h1") is True
    # Now h2 can claim it.
    assert await any_backend.acquire_lock("lease:w1", "h2", ttl_seconds=5) is True


@pytest.mark.asyncio
async def test_lock_zero_ttl_raises(any_backend: CoordinationBackend) -> None:
    with pytest.raises(ValueError):
        await any_backend.acquire_lock("k", "h", ttl_seconds=0)


# ---------------------------------------------------------------------------
# Factory


def test_factory_default() -> None:
    class _S:
        coordination_backend = "memory"
        coordination_redis_url = "redis://x/0"
        coordination_key_prefix = "plinth"

    assert isinstance(make_coordination_backend(_S()), MemoryBackend)


def test_factory_redis() -> None:
    class _S:
        coordination_backend = "redis"
        coordination_redis_url = "redis://x/0"
        coordination_key_prefix = "plinth"

    assert isinstance(make_coordination_backend(_S()), RedisBackend)


# ---------------------------------------------------------------------------
# Cluster sharing


@pytest.mark.asyncio
async def test_lock_shared_across_replicas() -> None:
    """Two ``RedisBackend`` instances against the same fake server: a lock
    held by replica A blocks replica B until A releases it.
    """

    import fakeredis

    server = fakeredis.FakeServer()
    a = RedisBackend("redis://x/0", key_prefix="ws", client=_fakeredis_with_server(server))
    b = RedisBackend("redis://x/0", key_prefix="ws", client=_fakeredis_with_server(server))

    assert await a.acquire_lock("hot", "rep-a", ttl_seconds=10) is True
    assert await b.acquire_lock("hot", "rep-b", ttl_seconds=10) is False
    assert await a.release_lock("hot", "rep-a") is True
    assert await b.acquire_lock("hot", "rep-b", ttl_seconds=10) is True


@pytest.mark.asyncio
async def test_set_shared_across_replicas() -> None:
    """A revocation written by replica A is visible to replica B."""

    import fakeredis

    server = fakeredis.FakeServer()
    a = RedisBackend("redis://x/0", key_prefix="ws", client=_fakeredis_with_server(server))
    b = RedisBackend("redis://x/0", key_prefix="ws", client=_fakeredis_with_server(server))

    await a.add_to_set("revoked_jtis", "jti_q", ttl_seconds=300)
    assert await b.is_member("revoked_jtis", "jti_q") is True


# ---------------------------------------------------------------------------
# aclose


@pytest.mark.asyncio
async def test_aclose_safe(any_backend: CoordinationBackend) -> None:
    await any_backend.aclose()


# ---------------------------------------------------------------------------
# Concurrency


@pytest.mark.asyncio
async def test_lock_concurrent_acquires(memory_backend: MemoryBackend) -> None:
    results: list[bool] = []

    async def attempt(holder: str) -> None:
        ok = await memory_backend.acquire_lock("k", holder, ttl_seconds=10)
        results.append(ok)

    await asyncio.gather(*(attempt(f"h{i}") for i in range(6)))
    assert results.count(True) == 1
    assert results.count(False) == 5
