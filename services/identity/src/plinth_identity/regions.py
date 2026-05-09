# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 The Plinth Authors
"""Cross-region peer discovery + status probing.

Implements the v1.0 ``GET /v1/regions`` endpoint shape spec'd in
``CONTRACTS.md → Multi-Region Scaffolding``. This module is **scaffolding
only**: it doesn't ship cross-region replication logic. The endpoint
exists so an operator-side orchestrator (cron, k8s sidecar, agent) can
discover peers and inspect their reachability without each service
re-implementing the wire format.

Probing uses ``HEAD /healthz`` against each peer URL with a short timeout
and a 30-second cache so the endpoint stays cheap under load.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from typing import Literal

import httpx
from pydantic import BaseModel, ConfigDict, Field

PeerStatus = Literal["up", "degraded", "down"]


class RegionPeer(BaseModel):
    """One peer region's reachability snapshot."""

    model_config = ConfigDict(extra="forbid")

    id: str
    url: str
    status: PeerStatus
    lag_ms: float | None = None
    last_seen_at: str | None = None  # ISO-8601 timestamp


class RegionsResponse(BaseModel):
    """Response shape for ``GET /v1/regions``."""

    model_config = ConfigDict(extra="forbid")

    current: str
    mode: Literal["primary", "replica", "standalone"] = "standalone"
    peers: list[RegionPeer] = Field(default_factory=list)


@dataclass
class _CachedPeer:
    """Internal cache entry — wall-clock expiry guards repeated probes."""

    peer: RegionPeer
    expires_at: float


class RegionStatusProbe:
    """Background-aware peer-status cache.

    Holds a 30-second cache (configurable) of probe results so a request
    spike doesn't translate to a probe spike. Probe failures degrade to
    ``down`` rather than raising — peers being unreachable is normal.
    """

    def __init__(
        self,
        *,
        cache_ttl_seconds: int = 30,
        probe_timeout_seconds: float = 2.0,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._cache_ttl = cache_ttl_seconds
        self._probe_timeout = probe_timeout_seconds
        self._cache: dict[str, _CachedPeer] = {}
        self._lock = asyncio.Lock()
        # Tests inject a mock client (e.g. via ``httpx.MockTransport``).
        self._client = client

    async def aclose(self) -> None:
        if self._client is not None:
            try:
                await self._client.aclose()
            except Exception:  # pragma: no cover - best-effort
                pass

    def _now(self) -> float:
        # Indirected so tests can monkeypatch.
        return time.monotonic()

    async def status(
        self,
        peer_id: str,
        peer_url: str,
    ) -> RegionPeer:
        """Return a cached or freshly-probed status for one peer."""

        now = self._now()
        cached = self._cache.get(peer_id)
        if cached is not None and cached.expires_at > now:
            return cached.peer

        peer = await self._probe(peer_id, peer_url)
        self._cache[peer_id] = _CachedPeer(
            peer=peer,
            expires_at=now + self._cache_ttl,
        )
        return peer

    async def _probe(self, peer_id: str, peer_url: str) -> RegionPeer:
        """Probe one peer's ``/healthz`` endpoint."""

        from datetime import datetime, timezone

        url = peer_url.rstrip("/") + "/healthz"
        started = time.perf_counter()
        status: PeerStatus = "down"
        lag_ms: float | None = None
        last_seen: str | None = None

        client_owned = False
        client = self._client
        if client is None:
            client = httpx.AsyncClient(timeout=self._probe_timeout)
            client_owned = True

        try:
            try:
                resp = await client.get(url)
                lag_ms = (time.perf_counter() - started) * 1000.0
                if 200 <= resp.status_code < 400:
                    # ``degraded`` heuristic: GET succeeds but the server
                    # hints it's behind. The contract leaves this rough
                    # ("a real implementation would correlate replication
                    # checkpoints"); we approximate via response time.
                    if lag_ms > self._probe_timeout * 1000.0 * 0.75:
                        status = "degraded"
                    else:
                        status = "up"
                    last_seen = datetime.now(timezone.utc).isoformat()
                else:
                    status = "down"
            except Exception:
                status = "down"
                lag_ms = None
        finally:
            if client_owned:
                try:
                    await client.aclose()
                except Exception:  # pragma: no cover
                    pass

        return RegionPeer(
            id=peer_id,
            url=peer_url,
            status=status,
            lag_ms=lag_ms,
            last_seen_at=last_seen,
        )

    async def all_peers(
        self,
        peer_urls: dict[str, str],
    ) -> list[RegionPeer]:
        """Fan-out probe + cache for the configured peer set."""

        # Probes are independent — run them concurrently. ``return_exceptions``
        # is unnecessary because ``status`` itself never raises.
        async with self._lock:
            tasks = [
                self.status(peer_id, peer_url)
                for peer_id, peer_url in peer_urls.items()
            ]
            return await asyncio.gather(*tasks)


__all__ = [
    "PeerStatus",
    "RegionPeer",
    "RegionStatusProbe",
    "RegionsResponse",
]
