# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 The Plinth Authors
"""Tests for the v1.1 coordination backend.

Covers both implementations behind the :class:`CoordinationBackend` Protocol:

* :class:`MemoryBackend` — in-process default.
* :class:`RedisBackend` — driven against ``fakeredis.aioredis`` so tests
  don't need a live Redis.

The factory + integration tests verify the wiring matches the v1.0 contracts
(memory backend = no behaviour change) and that switching to ``redis`` shares
state across two backend instances simulating a multi-replica deployment.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from plinth_gateway.coordination import (
    CoordinationBackend,
    MemoryBackend,
    RedisBackend,
    make_coordination_backend,
)


# ---------------------------------------------------------------------------
# Fixtures


def _fake_async_redis() -> Any:
    """Construct a fresh ``fakeredis.aioredis.FakeRedis`` with a shared server."""

    import fakeredis.aioredis

    return fakeredis.aioredis.FakeRedis(decode_responses=True)


@pytest.fixture
def memory_backend() -> MemoryBackend:
    return MemoryBackend()


@pytest.fixture
def redis_backend() -> RedisBackend:
    return RedisBackend(
        "redis://localhost:6379/0",
        key_prefix="plinth-test",
        client=_fake_async_redis(),
    )


@pytest.fixture(params=["memory", "redis"])
def any_backend(request) -> CoordinationBackend:
    if request.param == "memory":
        return MemoryBackend()
    return RedisBackend(
        "redis://localhost:6379/0",
        key_prefix="plinth-test",
        client=_fake_async_redis(),
    )


# ---------------------------------------------------------------------------
# Round-trip — get / set / delete


@pytest.mark.asyncio
async def test_set_then_get_returns_value(any_backend: CoordinationBackend) -> None:
    await any_backend.set("alpha", "one")
    assert await any_backend.get("alpha") == "one"


@pytest.mark.asyncio
async def test_get_missing_returns_none(any_backend: CoordinationBackend) -> None:
    assert await any_backend.get("missing") is None


@pytest.mark.asyncio
async def test_delete_removes_key(any_backend: CoordinationBackend) -> None:
    await any_backend.set("doomed", "here")
    await any_backend.delete("doomed")
    assert await any_backend.get("doomed") is None


@pytest.mark.asyncio
async def test_set_with_ttl_expires(memory_backend: MemoryBackend) -> None:
    """In-memory TTL is monotonic-clock-based; we monkeypatch ``time.monotonic``."""

    import time

    original = time.monotonic
    fake_now = [original()]
    try:
        time.monotonic = lambda: fake_now[0]
        await memory_backend.set("expires", "soon", ttl_seconds=1)
        assert await memory_backend.get("expires") == "soon"
        fake_now[0] += 2
        assert await memory_backend.get("expires") is None
    finally:
        time.monotonic = original


# ---------------------------------------------------------------------------
# incr — counter semantics


@pytest.mark.asyncio
async def test_incr_starts_at_amount(any_backend: CoordinationBackend) -> None:
    assert await any_backend.incr("counter", amount=3) == 3


@pytest.mark.asyncio
async def test_incr_accumulates(any_backend: CoordinationBackend) -> None:
    await any_backend.incr("c2")
    assert await any_backend.incr("c2", amount=4) == 5


@pytest.mark.asyncio
async def test_incr_with_ttl_persists_value(
    any_backend: CoordinationBackend,
) -> None:
    new_value = await any_backend.incr("c3", amount=2, ttl_seconds=60)
    assert new_value == 2
    assert int((await any_backend.get("c3")) or 0) == 2


# ---------------------------------------------------------------------------
# Sets — add_to_set / is_member / members


@pytest.mark.asyncio
async def test_add_to_set_then_member(any_backend: CoordinationBackend) -> None:
    await any_backend.add_to_set("revoked_jtis", "jti_a")
    assert await any_backend.is_member("revoked_jtis", "jti_a") is True
    assert await any_backend.is_member("revoked_jtis", "jti_b") is False


@pytest.mark.asyncio
async def test_members_returns_added_values(
    any_backend: CoordinationBackend,
) -> None:
    await any_backend.add_to_set("revoked_jtis", "jti_a")
    await any_backend.add_to_set("revoked_jtis", "jti_b")
    members = sorted(await any_backend.members("revoked_jtis"))
    assert members == ["jti_a", "jti_b"]


@pytest.mark.asyncio
async def test_members_respects_limit(any_backend: CoordinationBackend) -> None:
    for i in range(5):
        await any_backend.add_to_set("things", f"v{i}")
    out = await any_backend.members("things", limit=2)
    assert len(out) == 2


@pytest.mark.asyncio
async def test_members_empty_set_returns_empty_list(
    any_backend: CoordinationBackend,
) -> None:
    assert await any_backend.members("nope") == []


# ---------------------------------------------------------------------------
# Locks — acquire / release


@pytest.mark.asyncio
async def test_acquire_lock_first_caller_wins(
    any_backend: CoordinationBackend,
) -> None:
    assert await any_backend.acquire_lock("res1", "h1", ttl_seconds=10) is True
    # A different holder cannot acquire while h1 holds the lock.
    assert await any_backend.acquire_lock("res1", "h2", ttl_seconds=10) is False


@pytest.mark.asyncio
async def test_acquire_lock_same_holder_can_re_assert(
    any_backend: CoordinationBackend,
) -> None:
    await any_backend.acquire_lock("res2", "h1", ttl_seconds=10)
    # Same holder should be able to refresh — protocol promise
    assert await any_backend.acquire_lock("res2", "h1", ttl_seconds=10) is True


@pytest.mark.asyncio
async def test_release_lock_only_owner_succeeds(
    any_backend: CoordinationBackend,
) -> None:
    await any_backend.acquire_lock("res3", "h1", ttl_seconds=10)
    assert await any_backend.release_lock("res3", "wrong") is False
    assert await any_backend.release_lock("res3", "h1") is True


@pytest.mark.asyncio
async def test_release_lock_unknown_returns_false(
    any_backend: CoordinationBackend,
) -> None:
    assert await any_backend.release_lock("never_held", "h") is False


@pytest.mark.asyncio
async def test_acquire_lock_zero_ttl_raises(
    any_backend: CoordinationBackend,
) -> None:
    with pytest.raises(ValueError):
        await any_backend.acquire_lock("res4", "h", ttl_seconds=0)


# ---------------------------------------------------------------------------
# Health check


@pytest.mark.asyncio
async def test_memory_backend_health_always_true(
    memory_backend: MemoryBackend,
) -> None:
    assert await memory_backend.health() is True


@pytest.mark.asyncio
async def test_redis_backend_health_with_fakeredis(
    redis_backend: RedisBackend,
) -> None:
    assert await redis_backend.health() is True


@pytest.mark.asyncio
async def test_redis_backend_health_returns_false_on_disconnect() -> None:
    """Constructor with bad URL → connection failure → health False.

    We pass ``client=None`` so the backend tries to lazy-connect and
    blows up on the underlying socket; the Exception path inside
    ``health`` catches and returns False.
    """

    backend = RedisBackend("redis://localhost:1/0")
    assert await backend.health() is False


# ---------------------------------------------------------------------------
# Factory — settings.coordination_backend wiring


def test_factory_memory_default() -> None:
    class _S:
        coordination_backend = "memory"
        coordination_redis_url = "redis://localhost:6379/0"
        coordination_key_prefix = "plinth"

    backend = make_coordination_backend(_S())
    assert isinstance(backend, MemoryBackend)


def test_factory_redis_when_configured() -> None:
    class _S:
        coordination_backend = "redis"
        coordination_redis_url = "redis://localhost:6379/0"
        coordination_key_prefix = "plinth"

    backend = make_coordination_backend(_S())
    assert isinstance(backend, RedisBackend)


def test_factory_invalid_redis_url_falls_back_to_memory() -> None:
    """Empty URL is non-fatal — fall back to ``MemoryBackend``."""

    class _S:
        coordination_backend = "redis"
        coordination_redis_url = ""  # invalid
        coordination_key_prefix = "plinth"

    backend = make_coordination_backend(_S())
    assert isinstance(backend, MemoryBackend)


# ---------------------------------------------------------------------------
# Cluster sharing — two RedisBackend instances on the same fake server


@pytest.mark.asyncio
async def test_redis_backend_shared_state_across_instances() -> None:
    """A revoke from one ``RedisBackend`` is visible from another sharing
    the same Redis server. This is the v1.1 contract for multi-replica.
    """

    import fakeredis

    server = fakeredis.FakeServer()
    a = RedisBackend(
        "redis://x/0",
        key_prefix="plinth",
        client=_fakeredis_with_server(server),
    )
    b = RedisBackend(
        "redis://x/0",
        key_prefix="plinth",
        client=_fakeredis_with_server(server),
    )

    await a.add_to_set("revoked_jtis", "jti_xyz", ttl_seconds=300)
    assert await b.is_member("revoked_jtis", "jti_xyz") is True
    assert "jti_xyz" in await b.members("revoked_jtis")


def _fakeredis_with_server(server: Any) -> Any:
    import fakeredis.aioredis

    return fakeredis.aioredis.FakeRedis(server=server, decode_responses=True)


# ---------------------------------------------------------------------------
# Integration with LimitsRegistry — memory backend = v1.0 behaviour


@pytest.mark.asyncio
async def test_limits_registry_uses_memory_backend_by_default(tmp_path) -> None:
    from plinth_gateway.db import Database
    from plinth_gateway.limits import LimitsRegistry
    from plinth_gateway.settings import Settings

    db = Database(tmp_path / "g.db")
    await db.connect()
    settings = Settings(
        data_dir=str(tmp_path), rate_limit_default_rpm=60, rate_limit_default_burst=10
    )
    registry = LimitsRegistry(db, settings)
    assert isinstance(registry.coordination, MemoryBackend)
    # First call should not raise.
    await registry.assert_within_rate("agent_a")
    await db.close()


@pytest.mark.asyncio
async def test_limits_registry_with_redis_enforces_cluster_limit(tmp_path) -> None:
    """A second ``LimitsRegistry`` sharing the same Redis backend sees the
    cluster-wide counter and rejects when over the cluster cap.
    """

    from plinth_gateway.db import Database
    from plinth_gateway.exceptions import RateLimited
    from plinth_gateway.limits import LimitsRegistry
    from plinth_gateway.settings import Settings

    import fakeredis

    server = fakeredis.FakeServer()
    backend_a = RedisBackend(
        "redis://x/0",
        key_prefix="plinth-test-cluster",
        client=_fakeredis_with_server(server),
    )
    backend_b = RedisBackend(
        "redis://x/0",
        key_prefix="plinth-test-cluster",
        client=_fakeredis_with_server(server),
    )

    db_a = Database(tmp_path / "a.db")
    db_b = Database(tmp_path / "b.db")
    await db_a.connect()
    await db_b.connect()

    settings = Settings(
        data_dir=str(tmp_path),
        rate_limit_default_rpm=2,
        rate_limit_default_burst=10,  # local bucket allows the burst
        coordination_backend="redis",
        coordination_key_prefix="plinth-test-cluster",
    )
    reg_a = LimitsRegistry(db_a, settings, coordination=backend_a)
    reg_b = LimitsRegistry(db_b, settings, coordination=backend_b)

    # Two calls: each replica records 1 → cluster=2 (at limit).
    await reg_a.assert_within_rate("agent_x")
    await reg_b.assert_within_rate("agent_x")
    # The third call sees cluster_count=3 > rpm=2 → RateLimited.
    with pytest.raises(RateLimited):
        await reg_a.assert_within_rate("agent_x")

    await db_a.close()
    await db_b.close()


# ---------------------------------------------------------------------------
# Lock concurrency — two coroutines fighting for the same lock


@pytest.mark.asyncio
async def test_lock_concurrency_one_winner(memory_backend: MemoryBackend) -> None:
    results: list[bool] = []

    async def attempt(holder: str) -> None:
        ok = await memory_backend.acquire_lock("hot-key", holder, ttl_seconds=10)
        results.append(ok)

    await asyncio.gather(*(attempt(f"h{i}") for i in range(5)))
    # Exactly one acquirer wins.
    assert results.count(True) == 1
    assert results.count(False) == 4


# ---------------------------------------------------------------------------
# aclose is idempotent + safe


@pytest.mark.asyncio
async def test_memory_backend_aclose_is_noop(
    memory_backend: MemoryBackend,
) -> None:
    await memory_backend.aclose()
    await memory_backend.aclose()


@pytest.mark.asyncio
async def test_redis_backend_aclose_is_idempotent(
    redis_backend: RedisBackend,
) -> None:
    await redis_backend.aclose()
    await redis_backend.aclose()
