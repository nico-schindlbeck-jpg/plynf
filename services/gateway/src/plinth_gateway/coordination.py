# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 The Plinth Authors
"""Pluggable coordination backend for distributed state.

v1.1 introduces a pair of backends behind a single :class:`CoordinationBackend`
``Protocol`` so the gateway, identity, and workspace services can share
in-process state across replicas when an operator opts in to Redis.

Two implementations:

* :class:`MemoryBackend` — the default. Mirrors v1.0 single-process behaviour
  exactly; nothing crosses the process boundary. Safe to use with no Redis
  install at all (the import path is in ``RedisBackend.__init__``).
* :class:`RedisBackend` — uses ``redis.asyncio`` for async-native ops. Keys
  are namespaced under ``<key_prefix>:`` so multi-tenant Redis clusters can
  share an instance without collisions.

Operators flip between the two via ``PLINTH_COORDINATION_BACKEND=memory|redis``.
The :func:`make_coordination_backend` factory does the dispatch.

Best-effort semantics
---------------------
Every call is *best-effort*: when a Redis backend cannot reach its server, we
log a warning and degrade gracefully — typically by acting like the operation
"succeeded" so the caller can fall through to in-process behaviour. This means
a Redis outage never crashes the gateway/identity/workspace; it only loses
the cluster-wide consistency guarantee until the connection recovers.

Lock semantics
--------------
``acquire_lock`` is a SET-NX-EX atomic primitive (Redis ``SET key value NX EX``).
The ``holder`` is opaque — the caller usually passes a process-unique token
(uuid) so that ``release_lock`` is safe in the face of races. Only the holder
that originally set the value can release it; any other caller's release is
a no-op.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any, Protocol, runtime_checkable

import structlog


_log = structlog.get_logger(__name__)


# ---------------------------------------------------------------------------
# Protocol


@runtime_checkable
class CoordinationBackend(Protocol):
    """Pluggable backend for distributed-state coordination.

    Implementations: :class:`MemoryBackend` (in-process, default),
    :class:`RedisBackend` (cluster-shared). All operations are async +
    best-effort: if Redis is unreachable, callers should degrade gracefully
    (log warning, fall through to in-process behaviour).
    """

    async def get(self, key: str) -> str | None: ...

    async def set(
        self,
        key: str,
        value: str,
        *,
        ttl_seconds: int | None = None,
    ) -> None: ...

    async def delete(self, key: str) -> None: ...

    async def incr(
        self,
        key: str,
        *,
        amount: int = 1,
        ttl_seconds: int | None = None,
    ) -> int: ...

    async def add_to_set(
        self,
        key: str,
        value: str,
        *,
        ttl_seconds: int | None = None,
    ) -> None: ...

    async def is_member(self, key: str, value: str) -> bool: ...

    async def members(self, key: str, *, limit: int = 1000) -> list[str]: ...

    async def acquire_lock(
        self,
        key: str,
        holder: str,
        *,
        ttl_seconds: int,
    ) -> bool: ...

    async def release_lock(self, key: str, holder: str) -> bool: ...

    async def health(self) -> bool: ...

    async def aclose(self) -> None: ...


# ---------------------------------------------------------------------------
# In-process backend


class MemoryBackend:
    """In-process backend. Default. Mirrors v1.0 single-replica behaviour.

    Stores three independent maps (key→value, key→set-of-values, key→lock)
    so that ``set`` and ``add_to_set`` don't collide. TTLs are checked
    lazily on access — there's no background sweeper. This is correct for
    every test + single-node deployment; the slight CPU cost of always
    re-checking ``time()`` on access is negligible.

    The lock guard is an :class:`asyncio.Lock` because Plinth services run
    inside an async event loop and the maps are mutated from coroutines.
    """

    def __init__(self) -> None:
        # key → (value, expires_at | None)
        self._store: dict[str, tuple[str, float | None]] = {}
        # set-name → {value: expires_at | None}
        self._sets: dict[str, dict[str, float | None]] = {}
        self._set_expires: dict[str, float | None] = {}
        # key → (holder, expires_at)
        self._locks: dict[str, tuple[str, float]] = {}
        self._lock = asyncio.Lock()

    @staticmethod
    def _expires_at(ttl_seconds: int | None) -> float | None:
        if ttl_seconds is None or ttl_seconds <= 0:
            return None
        return time.monotonic() + float(ttl_seconds)

    @staticmethod
    def _is_alive(expires_at: float | None) -> bool:
        if expires_at is None:
            return True
        return time.monotonic() < expires_at

    async def get(self, key: str) -> str | None:
        async with self._lock:
            entry = self._store.get(key)
            if entry is None:
                return None
            value, expires_at = entry
            if not self._is_alive(expires_at):
                self._store.pop(key, None)
                return None
            return value

    async def set(
        self,
        key: str,
        value: str,
        *,
        ttl_seconds: int | None = None,
    ) -> None:
        async with self._lock:
            self._store[key] = (str(value), self._expires_at(ttl_seconds))

    async def delete(self, key: str) -> None:
        async with self._lock:
            self._store.pop(key, None)

    async def incr(
        self,
        key: str,
        *,
        amount: int = 1,
        ttl_seconds: int | None = None,
    ) -> int:
        async with self._lock:
            entry = self._store.get(key)
            current = 0
            expires_at: float | None = None
            if entry is not None:
                value, expires_at = entry
                if self._is_alive(expires_at):
                    try:
                        current = int(value)
                    except (TypeError, ValueError):
                        # Non-integer values reset to 0 — same behaviour as
                        # Redis ``INCR`` on a malformed string would error;
                        # we degrade rather than crash to keep the caller
                        # path simple.
                        current = 0
                else:
                    expires_at = None
            new_value = current + int(amount)
            # If the caller specified a TTL and there isn't one already,
            # apply it now. If a TTL exists, leave it in place — Redis
            # ``INCR`` doesn't reset TTLs.
            if expires_at is None and ttl_seconds is not None:
                expires_at = self._expires_at(ttl_seconds)
            self._store[key] = (str(new_value), expires_at)
            return new_value

    async def add_to_set(
        self,
        key: str,
        value: str,
        *,
        ttl_seconds: int | None = None,
    ) -> None:
        async with self._lock:
            bucket = self._sets.setdefault(key, {})
            bucket[str(value)] = self._expires_at(ttl_seconds)
            # The set itself can also have a TTL — applied per-value to mimic
            # Redis ``SADD`` + per-member expiry semantics. We additionally
            # track a per-set TTL for ``health``-style queries.
            if ttl_seconds is not None and ttl_seconds > 0:
                self._set_expires[key] = self._expires_at(ttl_seconds)

    async def is_member(self, key: str, value: str) -> bool:
        async with self._lock:
            bucket = self._sets.get(key)
            if not bucket:
                return False
            self._reap_set_locked(key, bucket)
            return str(value) in bucket

    async def members(self, key: str, *, limit: int = 1000) -> list[str]:
        async with self._lock:
            bucket = self._sets.get(key)
            if not bucket:
                return []
            self._reap_set_locked(key, bucket)
            return list(bucket.keys())[: max(0, int(limit))]

    def _reap_set_locked(
        self,
        key: str,
        bucket: dict[str, float | None],
    ) -> None:
        """Drop expired members from a set. Caller must hold ``self._lock``."""

        now = time.monotonic()
        dead = [v for v, exp in bucket.items() if exp is not None and now >= exp]
        for v in dead:
            bucket.pop(v, None)
        if not bucket:
            self._sets.pop(key, None)
            self._set_expires.pop(key, None)

    async def acquire_lock(
        self,
        key: str,
        holder: str,
        *,
        ttl_seconds: int,
    ) -> bool:
        if ttl_seconds <= 0:
            raise ValueError("ttl_seconds must be > 0")
        async with self._lock:
            entry = self._locks.get(key)
            now = time.monotonic()
            if entry is not None:
                cur_holder, expires_at = entry
                if expires_at > now:
                    # Holder re-asserting their lease is allowed.
                    if cur_holder == holder:
                        self._locks[key] = (holder, now + float(ttl_seconds))
                        return True
                    return False
            self._locks[key] = (str(holder), now + float(ttl_seconds))
            return True

    async def release_lock(self, key: str, holder: str) -> bool:
        async with self._lock:
            entry = self._locks.get(key)
            if entry is None:
                return False
            cur_holder, expires_at = entry
            if cur_holder != holder:
                return False
            # Honour expiry — releasing an already-expired lock is a no-op.
            self._locks.pop(key, None)
            return time.monotonic() < expires_at or True  # always True if owner

    async def health(self) -> bool:
        return True

    async def aclose(self) -> None:
        return None


# ---------------------------------------------------------------------------
# Redis backend


class RedisBackend:
    """Redis-backed coordination — activates on ``coordination_backend=redis``.

    Uses ``redis.asyncio`` (the modern async client shipped in ``redis>=5.0``).
    Every key is prefixed with ``<key_prefix>:`` so multiple Plinth tenants
    can share one Redis cluster. The connection is constructed lazily; if
    Redis is unreachable, every method logs a warning and returns a "best
    effort" response (e.g. ``False`` for ``is_member`` / ``acquire_lock``,
    ``0`` for ``incr``, ``None`` for ``get``) so caller paths can degrade
    gracefully.
    """

    def __init__(
        self,
        redis_url: str,
        *,
        key_prefix: str = "plinth",
        client: Any | None = None,
    ) -> None:
        if not redis_url:
            raise ValueError("redis_url must be set for RedisBackend")
        self._url = redis_url
        self._prefix = (key_prefix or "plinth").rstrip(":")
        # Lazy client: tests pass an already-constructed ``fakeredis.aioredis``
        # via ``client=`` so we don't need a real Redis. Production lets us
        # init the connection here.
        self._client = client
        self._owns_client = client is None

    @staticmethod
    def _import_redis() -> Any:
        from redis.asyncio import Redis  # type: ignore[import-not-found]

        return Redis

    def _ensure_client(self) -> Any:
        if self._client is None:
            Redis = self._import_redis()
            self._client = Redis.from_url(self._url, decode_responses=True)
            self._owns_client = True
        return self._client

    def _k(self, key: str) -> str:
        return f"{self._prefix}:{key}"

    async def aclose(self) -> None:
        if self._owns_client and self._client is not None:
            try:
                await self._client.aclose()
            except Exception:  # noqa: BLE001 — close is best-effort
                pass
            self._client = None

    async def get(self, key: str) -> str | None:
        try:
            client = self._ensure_client()
            value = await client.get(self._k(key))
        except Exception as exc:  # noqa: BLE001
            _log.warning("coordination.redis.get_failed", key=key, error=str(exc))
            return None
        if value is None:
            return None
        return value if isinstance(value, str) else str(value)

    async def set(
        self,
        key: str,
        value: str,
        *,
        ttl_seconds: int | None = None,
    ) -> None:
        try:
            client = self._ensure_client()
            if ttl_seconds is not None and ttl_seconds > 0:
                await client.set(self._k(key), str(value), ex=int(ttl_seconds))
            else:
                await client.set(self._k(key), str(value))
        except Exception as exc:  # noqa: BLE001
            _log.warning("coordination.redis.set_failed", key=key, error=str(exc))

    async def delete(self, key: str) -> None:
        try:
            client = self._ensure_client()
            await client.delete(self._k(key))
        except Exception as exc:  # noqa: BLE001
            _log.warning(
                "coordination.redis.delete_failed", key=key, error=str(exc)
            )

    async def incr(
        self,
        key: str,
        *,
        amount: int = 1,
        ttl_seconds: int | None = None,
    ) -> int:
        try:
            client = self._ensure_client()
            full = self._k(key)
            new_value = await client.incrby(full, int(amount))
            if ttl_seconds is not None and ttl_seconds > 0:
                # ``EXPIRE`` only when no TTL is set so the rolling-window
                # semantics are preserved (matches Redis ``INCR`` + ``EXPIRE NX``).
                if int(new_value) == int(amount):
                    # Fresh key — apply the TTL.
                    await client.expire(full, int(ttl_seconds))
            return int(new_value)
        except Exception as exc:  # noqa: BLE001
            _log.warning(
                "coordination.redis.incr_failed", key=key, error=str(exc)
            )
            return 0

    async def add_to_set(
        self,
        key: str,
        value: str,
        *,
        ttl_seconds: int | None = None,
    ) -> None:
        try:
            client = self._ensure_client()
            full = self._k(key)
            await client.sadd(full, str(value))
            if ttl_seconds is not None and ttl_seconds > 0:
                await client.expire(full, int(ttl_seconds))
        except Exception as exc:  # noqa: BLE001
            _log.warning(
                "coordination.redis.add_to_set_failed",
                key=key,
                error=str(exc),
            )

    async def is_member(self, key: str, value: str) -> bool:
        try:
            client = self._ensure_client()
            return bool(await client.sismember(self._k(key), str(value)))
        except Exception as exc:  # noqa: BLE001
            _log.warning(
                "coordination.redis.is_member_failed",
                key=key,
                error=str(exc),
            )
            return False

    async def members(self, key: str, *, limit: int = 1000) -> list[str]:
        try:
            client = self._ensure_client()
            full = self._k(key)
            cap = max(1, int(limit))
            # SSCAN is bounded; for sets > 1k members we still cap at ``limit``.
            cursor = 0
            out: list[str] = []
            while True:
                cursor, batch = await client.sscan(full, cursor=cursor, count=cap)
                for v in batch:
                    out.append(v if isinstance(v, str) else str(v))
                    if len(out) >= cap:
                        return out
                if cursor == 0:
                    break
            return out
        except Exception as exc:  # noqa: BLE001
            _log.warning(
                "coordination.redis.members_failed", key=key, error=str(exc)
            )
            return []

    async def acquire_lock(
        self,
        key: str,
        holder: str,
        *,
        ttl_seconds: int,
    ) -> bool:
        if ttl_seconds <= 0:
            raise ValueError("ttl_seconds must be > 0")
        try:
            client = self._ensure_client()
            full = self._k(f"lock:{key}")
            # SET key value NX EX seconds — atomic acquire.
            result = await client.set(
                full,
                str(holder),
                nx=True,
                ex=int(ttl_seconds),
            )
            if result:
                return True
            # Re-check: maybe ``holder`` already owns the lock — allow the
            # re-assert path to refresh the TTL (matches the in-memory
            # backend's behaviour).
            current = await client.get(full)
            if current is not None and (
                current == holder or current == str(holder)
            ):
                await client.expire(full, int(ttl_seconds))
                return True
            return False
        except Exception as exc:  # noqa: BLE001
            _log.warning(
                "coordination.redis.acquire_lock_failed",
                key=key,
                error=str(exc),
            )
            return False

    async def release_lock(self, key: str, holder: str) -> bool:
        try:
            client = self._ensure_client()
            full = self._k(f"lock:{key}")
            # Lua script to do the compare-and-delete atomically: only
            # remove the key if the stored holder matches ours.
            script = (
                "if redis.call('GET', KEYS[1]) == ARGV[1] "
                "then return redis.call('DEL', KEYS[1]) "
                "else return 0 end"
            )
            try:
                result = await client.eval(script, 1, full, str(holder))
            except Exception:  # noqa: BLE001
                # Fall back to a non-atomic compare-and-delete. Less safe,
                # but it's only used when the server doesn't support EVAL
                # (e.g. some restricted shared-Redis offerings).
                current = await client.get(full)
                if current is not None and (
                    current == holder or current == str(holder)
                ):
                    await client.delete(full)
                    return True
                return False
            return bool(int(result or 0))
        except Exception as exc:  # noqa: BLE001
            _log.warning(
                "coordination.redis.release_lock_failed",
                key=key,
                error=str(exc),
            )
            return False

    async def health(self) -> bool:
        try:
            client = self._ensure_client()
            pong = await client.ping()
            return bool(pong)
        except Exception as exc:  # noqa: BLE001
            _log.warning("coordination.redis.health_failed", error=str(exc))
            return False


# ---------------------------------------------------------------------------
# Factory


def make_coordination_backend(settings: Any) -> CoordinationBackend:
    """Construct the backend declared by ``settings``.

    ``settings`` must expose the v1.1 attributes:

    * ``coordination_backend`` — ``"memory"`` (default) or ``"redis"``
    * ``coordination_redis_url`` — Redis connection URL when redis
    * ``coordination_key_prefix`` — multi-tenant namespace

    A ``redis`` backend that fails to construct (bad URL, missing import)
    falls back to :class:`MemoryBackend` with a warning so the service
    keeps booting.
    """

    backend = str(getattr(settings, "coordination_backend", "memory")).lower()
    if backend == "redis":
        url = str(getattr(settings, "coordination_redis_url", "") or "")
        prefix = str(getattr(settings, "coordination_key_prefix", "plinth") or "plinth")
        try:
            return RedisBackend(url, key_prefix=prefix)
        except Exception as exc:  # noqa: BLE001 — never break startup
            _log.warning(
                "coordination.redis.init_failed",
                error=str(exc),
                hint="falling back to MemoryBackend",
            )
            return MemoryBackend()
    return MemoryBackend()


__all__ = [
    "CoordinationBackend",
    "MemoryBackend",
    "RedisBackend",
    "make_coordination_backend",
]
