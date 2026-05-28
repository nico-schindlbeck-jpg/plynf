# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 The Plynf Authors
"""Tiny in-memory cache for tool responses.

Production deployments swap this for Redis (already part of the Plynf stack
via ``redis>=5.0``); the MVP demo runs single-process so a TTL dict is
sufficient.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any


@dataclass
class _Entry:
    value: Any
    expires_at: float


class TTLCache:
    def __init__(self) -> None:
        self._store: dict[str, _Entry] = {}

    def get(self, key: str) -> tuple[bool, Any]:
        """Return ``(hit, value)``. Expired entries are evicted lazily."""
        entry = self._store.get(key)
        if entry is None:
            return False, None
        if entry.expires_at < time.time():
            self._store.pop(key, None)
            return False, None
        return True, entry.value

    def set(self, key: str, value: Any, ttl_seconds: int) -> None:
        if ttl_seconds <= 0:
            return
        self._store[key] = _Entry(value=value, expires_at=time.time() + ttl_seconds)

    def clear(self) -> None:
        self._store.clear()


__all__ = ["TTLCache"]
