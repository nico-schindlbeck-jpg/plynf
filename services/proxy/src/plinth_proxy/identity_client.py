# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 The Plynf Authors
"""Identity-service client.

The proxy verifies incoming bearer tokens against the existing
``services/identity`` ``/v1/tokens/verify`` endpoint. Claims are short-TTL
cached so we don't pay an HTTP roundtrip for every chat-completion.

Tier is encoded as a scope on the JWT:

    scopes: ["tier:pro", "workspace:read", ...]

Convention: ``tier:free``, ``tier:pro``, ``tier:enterprise``. Missing →
``free``. Multiple tier scopes → highest wins. This lets us upgrade/downgrade
a tenant by re-issuing their token without changing the schema.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass

import httpx

log = logging.getLogger("plinth.proxy.identity")


_TIER_RANK = {"free": 0, "pro": 1, "enterprise": 2}


@dataclass
class VerifiedClaims:
    tenant_id: str
    agent_id: str | None
    tier: str
    scopes: tuple[str, ...]
    jti: str
    exp: int


def _tier_from_scopes(scopes: list[str]) -> str:
    """Pick the highest ``tier:*`` scope. Default ``free``."""
    found = [s.split(":", 1)[1] for s in scopes if s.startswith("tier:")]
    if not found:
        return "free"
    found.sort(key=lambda t: _TIER_RANK.get(t, -1), reverse=True)
    return found[0] if found[0] in _TIER_RANK else "free"


class IdentityClient:
    """Verify JWTs against the Plynf identity service.

    Result cache key is the bearer token; TTL is bounded by the token's own
    ``exp`` claim and the configured ``cache_ttl_s`` ceiling. Revocations
    propagate within at most ``cache_ttl_s`` seconds — for hard-revocation
    cases, set ``cache_ttl_s=0``.
    """

    def __init__(
        self,
        base_url: str,
        *,
        timeout_s: float = 5.0,
        cache_ttl_s: int = 60,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self._timeout = timeout_s
        self._cache_ttl = cache_ttl_s
        self._cache: dict[str, tuple[float, VerifiedClaims]] = {}

    async def verify(self, token: str) -> VerifiedClaims:
        """Return the claims for ``token`` or raise :class:`IdentityError`."""
        now = time.time()
        cached = self._cache.get(token)
        if cached is not None:
            expires_at, claims = cached
            if expires_at > now:
                return claims

        url = f"{self.base_url}/v1/tokens/verify"
        try:
            async with httpx.AsyncClient(timeout=self._timeout) as client:
                resp = await client.post(url, json={"token": token})
        except httpx.HTTPError as e:
            raise IdentityError(503, f"identity service unreachable: {e!s}") from e

        if resp.status_code >= 400:
            raise IdentityError(resp.status_code, resp.text[:300])

        body = resp.json()
        scopes = list(body.get("scopes") or [])
        claims = VerifiedClaims(
            tenant_id=body.get("tenant_id") or "default",
            agent_id=body.get("agent_id"),
            tier=_tier_from_scopes(scopes),
            scopes=tuple(scopes),
            jti=body.get("jti", ""),
            exp=int(body.get("exp") or 0),
        )

        # Cache until min(token_exp, configured ceiling).
        ceiling = now + self._cache_ttl
        bound = float(claims.exp) if claims.exp > now else ceiling
        self._cache[token] = (min(ceiling, bound), claims)
        return claims

    def clear_cache(self) -> None:
        self._cache.clear()


class IdentityError(RuntimeError):
    def __init__(self, status: int, body: str) -> None:
        super().__init__(f"identity verify failed ({status}): {body[:200]}")
        self.status = status
        self.body = body


__all__ = ["IdentityClient", "IdentityError", "VerifiedClaims"]
