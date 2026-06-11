# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 The Plynf Authors
"""Resolve customer API keys against the dashboard control plane.

Self-serve customers get an opaque ``plynf_sk_live_…`` key at signup. The
proxy resolves such keys via ``GET <accounts_url>/internal/keys/{key}``
(shared-secret protected). Results are cached briefly on both directions:

* positive hits for ``cache_ttl_s`` (default 60 s) — so a plan change via
  the Stripe webhook is live on the proxy within a minute, and a key
  rotation locks the old key out within a minute;
* misses for ``negative_ttl_s`` (default 10 s) — so a typo'd key can't be
  used to hammer the control plane.

Configured via ``PLINTH_PROXY_ACCOUNTS_URL`` + ``PLINTH_PROXY_INTERNAL_SECRET``.
"""

from __future__ import annotations

import time

import httpx


class AccountsKeyClient:
    """Async client for the dashboard's internal key resolver."""

    def __init__(
        self,
        base_url: str,
        internal_secret: str,
        *,
        cache_ttl_s: float = 60.0,
        negative_ttl_s: float = 10.0,
        timeout_s: float = 5.0,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.internal_secret = internal_secret
        self.cache_ttl_s = cache_ttl_s
        self.negative_ttl_s = negative_ttl_s
        self._client = client or httpx.AsyncClient(timeout=timeout_s)
        self._owns_client = client is None
        # key → (expires_at, (tenant_id, tier) | None)
        self._cache: dict[str, tuple[float, tuple[str, str] | None]] = {}

    # Hard cap so a flood of garbage/typo'd keys can't grow the cache
    # without bound (memory-exhaustion DoS).
    _MAX_ENTRIES = 50_000

    def _evict(self, now: float) -> None:
        """Drop expired entries; if still over the cap, drop oldest-expiring."""
        for k in [k for k, (exp, _) in self._cache.items() if exp <= now]:
            self._cache.pop(k, None)
        if len(self._cache) > self._MAX_ENTRIES:
            overflow = len(self._cache) - self._MAX_ENTRIES
            for k, _ in sorted(self._cache.items(), key=lambda kv: kv[1][0])[:overflow]:
                self._cache.pop(k, None)

    async def resolve(self, api_key: str) -> tuple[str, str] | None:
        """Return ``(tenant_id, tier)`` for a customer key, or None."""
        now = time.monotonic()
        cached = self._cache.get(api_key)
        if cached is not None and cached[0] > now:
            return cached[1]

        result: tuple[str, str] | None = None
        try:
            resp = await self._client.get(
                f"{self.base_url}/internal/keys/{api_key}",
                headers={"X-Internal-Secret": self.internal_secret},
            )
            if resp.status_code == 200:
                data = resp.json()
                tenant = str(data.get("tenant_id") or "")
                tier = str(data.get("tier") or "free")
                if tenant:
                    result = (tenant, tier)
            elif resp.status_code >= 500:
                # Transient control-plane error (e.g. a mid-deploy 503):
                # treat like unreachable — serve the stale entry instead of
                # locking a VALID customer out for negative_ttl_s. Don't
                # cache the failure. A 4xx (404 unknown key / 403 bad secret)
                # is authoritative and falls through to the negative cache.
                return cached[1] if cached is not None else None
        except (httpx.HTTPError, ValueError):
            # Control plane unreachable: don't poison the cache — return the
            # stale entry if we had one (graceful degradation), else None.
            if cached is not None:
                return cached[1]
            return None

        ttl = self.cache_ttl_s if result is not None else self.negative_ttl_s
        self._cache[api_key] = (now + ttl, result)
        if len(self._cache) > self._MAX_ENTRIES:
            self._evict(now)
        return result

    def clear_cache(self) -> None:
        self._cache.clear()

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()


__all__ = ["AccountsKeyClient"]
