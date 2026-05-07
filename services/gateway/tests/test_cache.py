# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 The Plinth Authors
"""Direct tests for ``cache.Cache``."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from plinth_gateway.cache import (
    Cache,
    canonical_json,
    compute_cache_key,
    hash_args,
    hash_result,
)


def test_canonical_json_sorts_keys_and_strips_spaces() -> None:
    a = canonical_json({"b": 1, "a": 2, "c": [3, 4]})
    b = canonical_json({"a": 2, "c": [3, 4], "b": 1})
    assert a == b == '{"a":2,"b":1,"c":[3,4]}'


def test_compute_cache_key_uses_tool_id_and_args() -> None:
    k1 = compute_cache_key("web.fetch", {"url": "x"})
    k2 = compute_cache_key("web.fetch", {"url": "x"})
    k3 = compute_cache_key("web.fetch", {"url": "y"})
    k4 = compute_cache_key("web.search", {"url": "x"})
    assert k1 == k2
    assert k1 != k3
    assert k1 != k4
    assert len(k1) == 64  # sha256 hex


def test_hash_args_and_result_are_sha256() -> None:
    assert len(hash_args({"a": 1})) == 64
    assert len(hash_result([1, 2, 3])) == 64


def test_parse_ts_handles_naive_and_aware_datetimes() -> None:
    """Internal _parse_ts: aware datetime preserved, naive gets UTC, str parsed."""
    from plinth_gateway import cache as cache_mod
    from datetime import datetime, timezone

    aware = datetime(2026, 1, 1, tzinfo=timezone.utc)
    assert cache_mod._parse_ts(aware) is aware

    naive = datetime(2026, 1, 1)
    assert cache_mod._parse_ts(naive).tzinfo == timezone.utc

    s = "2026-01-01T00:00:00+00:00"
    assert cache_mod._parse_ts(s) == datetime(2026, 1, 1, tzinfo=timezone.utc)


@pytest.mark.asyncio
async def test_store_and_lookup_increments_hit_count(db) -> None:
    cache = Cache(db)
    await cache.store("web.fetch", {"url": "u"}, {"x": 1}, ttl_seconds=300)

    hit1 = await cache.lookup("web.fetch", {"url": "u"})
    assert hit1 is not None
    assert hit1.result == {"x": 1}
    assert hit1.hit_count == 1

    hit2 = await cache.lookup("web.fetch", {"url": "u"})
    assert hit2 is not None
    assert hit2.hit_count == 2

    assert cache.counters.hits == 2
    assert cache.counters.misses == 0


@pytest.mark.asyncio
async def test_lookup_miss(db) -> None:
    cache = Cache(db)
    miss = await cache.lookup("web.fetch", {"url": "nope"})
    assert miss is None
    assert cache.counters.misses == 1


@pytest.mark.asyncio
async def test_expired_entry_removed_on_lookup(db) -> None:
    cache = Cache(db)
    # Insert an expired entry by hand
    key = compute_cache_key("web.fetch", {"url": "u"})
    past = (datetime.now(timezone.utc) - timedelta(seconds=10)).isoformat()
    now = datetime.now(timezone.utc).isoformat()
    await db.execute(
        """
        INSERT INTO cache_entries
          (cache_key, tool_id, arguments_hash, result, created_at, expires_at, hit_count)
        VALUES (?, ?, ?, ?, ?, ?, 0)
        """,
        (key, "web.fetch", hash_args({"url": "u"}), '{"x":1}', now, past),
    )
    miss = await cache.lookup("web.fetch", {"url": "u"})
    assert miss is None
    rows = await db.fetchall("SELECT * FROM cache_entries WHERE cache_key = ?", (key,))
    assert rows == []


@pytest.mark.asyncio
async def test_cleanup_expired(db) -> None:
    cache = Cache(db)
    past = (datetime.now(timezone.utc) - timedelta(seconds=10)).isoformat()
    fut = (datetime.now(timezone.utc) + timedelta(seconds=300)).isoformat()
    now = datetime.now(timezone.utc).isoformat()
    await db.execute(
        "INSERT INTO cache_entries VALUES ('k1','t','h','{}',?,?,0)", (now, past)
    )
    await db.execute(
        "INSERT INTO cache_entries VALUES ('k2','t','h','{}',?,?,0)", (now, fut)
    )
    purged = await cache.cleanup_expired()
    assert purged == 1
    remaining = await db.fetchall("SELECT cache_key FROM cache_entries")
    assert {r["cache_key"] for r in remaining} == {"k2"}


@pytest.mark.asyncio
async def test_clear_by_tool_and_global(db) -> None:
    cache = Cache(db)
    await cache.store("a", {"x": 1}, {"r": 1}, ttl_seconds=300)
    await cache.store("b", {"x": 2}, {"r": 2}, ttl_seconds=300)
    n = await cache.clear(tool_id="a")
    assert n == 1
    remaining = await db.fetchall("SELECT tool_id FROM cache_entries")
    assert [r["tool_id"] for r in remaining] == ["b"]

    n = await cache.clear()
    assert n == 1
    remaining = await db.fetchall("SELECT tool_id FROM cache_entries")
    assert remaining == []


@pytest.mark.asyncio
async def test_store_replaces_existing(db) -> None:
    cache = Cache(db)
    await cache.store("a", {"x": 1}, {"r": 1}, ttl_seconds=300)
    await cache.store("a", {"x": 1}, {"r": 2}, ttl_seconds=300)
    rows = await db.fetchall("SELECT cache_key, result FROM cache_entries WHERE tool_id='a'")
    assert len(rows) == 1
    assert rows[0]["result"] == '{"r":2}'


@pytest.mark.asyncio
async def test_stats_reports_size(db) -> None:
    cache = Cache(db)
    await cache.store("a", {"x": 1}, {"r": "x" * 100}, ttl_seconds=300)
    s = await cache.stats()
    assert s["entries"] == 1
    assert s["size_bytes"] >= 100
    assert s["hits"] == 0
    assert s["misses"] == 0
