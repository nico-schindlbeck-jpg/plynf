# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 The Plinth Authors
"""Caching layer.

Cache key = ``sha256(tool_id || "::" || canonical_json(args))``.
Canonical JSON: ``json.dumps(args, sort_keys=True, separators=(",", ":"))``.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from .db import Database


def canonical_json(args: dict[str, Any] | Any) -> str:
    """Render ``args`` as canonical JSON (sorted keys, no spaces)."""
    return json.dumps(args, sort_keys=True, separators=(",", ":"), default=str)


def hash_args(args: dict[str, Any] | Any) -> str:
    """Return ``sha256(canonical_json(args))``."""
    return hashlib.sha256(canonical_json(args).encode("utf-8")).hexdigest()


def hash_result(result: Any) -> str:
    """Return ``sha256(canonical_json(result))``."""
    return hashlib.sha256(canonical_json(result).encode("utf-8")).hexdigest()


def compute_cache_key(tool_id: str, arguments: dict[str, Any]) -> str:
    """Return the canonical cache key for a tool invocation.

    ``sha256(tool_id + "::" + canonical_json(args))``.
    """
    body = f"{tool_id}::{canonical_json(arguments)}"
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _parse_ts(value: object) -> datetime:
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    return datetime.fromisoformat(str(value))


@dataclass
class CacheHit:
    """Returned on cache hit."""

    cache_key: str
    result: Any
    created_at: datetime
    expires_at: datetime
    hit_count: int  # value AFTER the increment


@dataclass
class CacheCounters:
    """In-process counters for ``GET /v1/cache/stats``.

    Persistent stats live in ``cache_entries.hit_count``; this is for
    cumulative hits/misses across the lifetime of the running process.
    """

    hits: int = 0
    misses: int = 0


class Cache:
    """Cache wrapper around the ``cache_entries`` table."""

    def __init__(self, db: Database) -> None:
        self._db = db
        self.counters = CacheCounters()

    @staticmethod
    def cache_key(tool_id: str, arguments: dict[str, Any]) -> str:
        return compute_cache_key(tool_id, arguments)

    async def lookup(
        self, tool_id: str, arguments: dict[str, Any]
    ) -> CacheHit | None:
        """Return a hit (and bump hit_count) or ``None``.

        Expired entries are removed and not returned.
        """
        key = self.cache_key(tool_id, arguments)
        row = await self._db.fetchone(
            "SELECT cache_key, result, created_at, expires_at, hit_count "
            "FROM cache_entries WHERE cache_key = ?",
            (key,),
        )
        if row is None:
            self.counters.misses += 1
            return None

        expires_at = _parse_ts(row["expires_at"])
        if expires_at <= _utcnow():
            await self._db.execute(
                "DELETE FROM cache_entries WHERE cache_key = ?", (key,)
            )
            self.counters.misses += 1
            return None

        new_hit_count = int(row["hit_count"]) + 1
        await self._db.execute(
            "UPDATE cache_entries SET hit_count = ? WHERE cache_key = ?",
            (new_hit_count, key),
        )
        self.counters.hits += 1
        return CacheHit(
            cache_key=key,
            result=json.loads(row["result"]),
            created_at=_parse_ts(row["created_at"]),
            expires_at=expires_at,
            hit_count=new_hit_count,
        )

    async def store(
        self,
        tool_id: str,
        arguments: dict[str, Any],
        result: Any,
        ttl_seconds: int,
    ) -> str:
        """Insert or replace a cache entry. Returns the cache key."""
        key = self.cache_key(tool_id, arguments)
        now = _utcnow()
        expires_at = now + timedelta(seconds=ttl_seconds)
        await self._db.execute(
            """
            INSERT INTO cache_entries
              (cache_key, tool_id, arguments_hash, result, created_at, expires_at, hit_count)
            VALUES (?, ?, ?, ?, ?, ?, 0)
            ON CONFLICT(cache_key) DO UPDATE SET
              result = excluded.result,
              created_at = excluded.created_at,
              expires_at = excluded.expires_at,
              hit_count = 0
            """,
            (
                key,
                tool_id,
                hash_args(arguments),
                canonical_json(result),
                now.isoformat(),
                expires_at.isoformat(),
            ),
        )
        return key

    async def cleanup_expired(self) -> int:
        """Delete expired entries; return the number purged."""
        now = _utcnow()
        async with self._db.cursor() as cur:
            await cur.execute(
                "DELETE FROM cache_entries WHERE expires_at < ?", (now.isoformat(),)
            )
            count = cur.rowcount
        conn = await self._db.connect()
        await conn.commit()
        return count if count and count >= 0 else 0

    async def clear(self, tool_id: str | None = None) -> int:
        """Drop entries (all or by tool). Returns count removed."""
        async with self._db.cursor() as cur:
            if tool_id is None:
                await cur.execute("DELETE FROM cache_entries")
            else:
                await cur.execute(
                    "DELETE FROM cache_entries WHERE tool_id = ?", (tool_id,)
                )
            count = cur.rowcount
        conn = await self._db.connect()
        await conn.commit()
        return count if count and count >= 0 else 0

    async def stats(self) -> dict[str, int]:
        """Return live cache stats including byte size and hits/misses."""
        row = await self._db.fetchone(
            "SELECT COUNT(*) AS n, COALESCE(SUM(LENGTH(result)),0) AS sz "
            "FROM cache_entries"
        )
        n = int(row["n"]) if row else 0
        size_bytes = int(row["sz"]) if row else 0
        return {
            "hits": self.counters.hits,
            "misses": self.counters.misses,
            "entries": n,
            "size_bytes": size_bytes,
        }
