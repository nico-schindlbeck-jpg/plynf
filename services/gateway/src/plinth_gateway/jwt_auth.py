# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 The Plinth Authors
"""JWT verification for the gateway's inbound auth.

The gateway plays the same role as the workspace: it verifies tokens issued
by the identity service. Three modes (``permissive``, ``verify_local``,
``verify_remote``) match the workspace's semantics — see
``plinth_workspace.auth`` for the rationale.

The existing ``auth.py`` is reserved for *outbound* auth (the gateway
attaching a Bearer token to backend tool calls). We keep this module
separate to avoid mixing concerns.

For v0.4 the verifier accepts both HS256 and RS256 tokens. RS256
verification fetches a JWKS document from the identity service and
caches public keys for ``identity_jwks_cache_ttl_seconds``.
"""

from __future__ import annotations

import asyncio
import base64
from dataclasses import dataclass
from datetime import datetime, timezone

import httpx
import jwt as pyjwt
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from .exceptions import Unauthorized
from .settings import Settings

UTC = timezone.utc  # noqa: UP017
HS256 = "HS256"
RS256 = "RS256"
JWT_ALG = HS256  # historical default for HS256-only callers


@dataclass
class AuthContext:
    """Resolved auth metadata for a single inbound gateway request."""

    tenant_id: str = "default"
    agent_id: str | None = None
    scopes: list[str] | None = None
    jti: str | None = None
    authenticated: bool = False

    def has_scope(self, required: str) -> bool:
        """Return True iff the token grants ``required`` (see workspace.auth)."""

        held = list(self.scopes or [])
        if "*" in held:
            return True
        if required in held:
            return True
        parts = required.split(":")
        if len(parts) >= 2:
            prefix = ":".join(parts[:-1])
            if prefix in held:
                return True
            wildcard = f"{parts[0]}:*"
            if wildcard in held:
                return True
        return False


# ---------------------------------------------------------------------------
# JWKS cache (RS256 verifier) — mirror of the workspace implementation


class JWKSCache:
    """Caches public keys fetched from the identity service.

    Lazy: fetches the JWKS document on first call. Refreshes when:

    * the cache is older than ``ttl_seconds``, or
    * a verify request asks for a ``kid`` we don't know about.
    """

    def __init__(
        self,
        identity_url: str,
        *,
        ttl_seconds: int = 300,
        http_client: httpx.AsyncClient | None = None,
    ) -> None:
        self._identity_url = identity_url.rstrip("/")
        self._ttl = ttl_seconds
        self._http = http_client
        self._owned_http = http_client is None
        self._keys: dict[str, str] = {}
        self._fetched_at: datetime | None = None
        self._lock = asyncio.Lock()

    async def aclose(self) -> None:
        if self._owned_http and self._http is not None:
            await self._http.aclose()
            self._http = None

    def _is_stale(self) -> bool:
        if self._fetched_at is None:
            return True
        age = (datetime.now(UTC) - self._fetched_at).total_seconds()
        return age >= self._ttl

    async def _client(self) -> httpx.AsyncClient:
        if self._http is None:
            self._http = httpx.AsyncClient(timeout=5.0)
        return self._http

    async def get(self, kid: str) -> str:
        if not self._is_stale() and kid in self._keys:
            return self._keys[kid]
        await self._refresh(force=kid not in self._keys)
        pem = self._keys.get(kid)
        if pem is None:
            raise Unauthorized(
                f"unknown signing key kid {kid!r}",
                code="INVALID_TOKEN",
                details={"kid": kid},
            )
        return pem

    async def _refresh(self, *, force: bool = False) -> None:
        async with self._lock:
            if not force and not self._is_stale() and self._keys:
                return
            url = f"{self._identity_url}/v1/.well-known/jwks.json"
            try:
                client = await self._client()
                response = await client.get(url)
                response.raise_for_status()
            except httpx.HTTPError as exc:
                raise Unauthorized(
                    "unable to fetch JWKS from identity service",
                    code="INVALID_TOKEN",
                    details={"reason": str(exc), "url": url},
                ) from exc
            doc = response.json()
            self._keys = {
                jwk["kid"]: _jwk_to_pem(jwk).decode("ascii")
                for jwk in doc.get("keys", [])
                if "kid" in jwk
            }
            self._fetched_at = datetime.now(UTC)


def _b64url_decode(value: str) -> bytes:
    padded = value + "=" * (-len(value) % 4)
    return base64.urlsafe_b64decode(padded)


def _jwk_to_pem(jwk: dict) -> bytes:
    n_b64 = jwk.get("n")
    e_b64 = jwk.get("e")
    if not n_b64 or not e_b64:
        raise Unauthorized(
            "JWKS entry missing 'n' or 'e'",
            code="INVALID_TOKEN",
            details={"kid": jwk.get("kid")},
        )
    n = int.from_bytes(_b64url_decode(n_b64), "big")
    e = int.from_bytes(_b64url_decode(e_b64), "big")
    public_key = rsa.RSAPublicNumbers(e=e, n=n).public_key()
    return public_key.public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    )


_jwks_caches: dict[int, JWKSCache] = {}


def _get_jwks_cache(settings: Settings) -> JWKSCache:
    cache = _jwks_caches.get(id(settings))
    if cache is None:
        cache = JWKSCache(
            settings.identity_url,
            ttl_seconds=settings.identity_jwks_cache_ttl_seconds,
        )
        _jwks_caches[id(settings)] = cache
    return cache


async def reset_jwks_cache(settings: Settings) -> None:
    cache = _jwks_caches.pop(id(settings), None)
    if cache is not None:
        await cache.aclose()


def _bearer_token(authorization: str | None) -> str:
    if not authorization:
        return ""
    parts = authorization.strip().split()
    if len(parts) != 2 or parts[0].lower() != "bearer" or not parts[1]:
        return ""
    return parts[1]


# ---------------------------------------------------------------------------
# HS256 verifier (sync — no I/O)


def _verify_hs256(token: str, settings: Settings) -> AuthContext:
    secret = settings.jwt_secret_value
    if not secret:
        raise Unauthorized(
            "verify_local mode is enabled but no JWT secret is configured",
            details={"hint": "set PLINTH_IDENTITY_JWT_SECRET"},
        )

    try:
        payload = pyjwt.decode(
            token,
            secret,
            algorithms=[HS256],
            audience=settings.jwt_audience,
            options={"require": ["exp", "iat", "jti", "sub", "aud"]},
        )
    except pyjwt.ExpiredSignatureError as exc:
        raise Unauthorized(
            "token has expired",
            code="TOKEN_EXPIRED",
            details={},
        ) from exc
    except pyjwt.InvalidTokenError as exc:
        raise Unauthorized(
            f"token is invalid: {exc}",
            code="INVALID_TOKEN",
            details={},
        ) from exc

    return AuthContext(
        tenant_id=payload.get("tenant_id") or "default",
        agent_id=payload.get("agent_id") or payload.get("sub"),
        scopes=list(payload.get("scopes") or []),
        jti=payload.get("jti"),
        authenticated=True,
    )


# ---------------------------------------------------------------------------
# RS256 verifier (async)


async def _verify_rs256(token: str, settings: Settings) -> AuthContext:
    try:
        header = pyjwt.get_unverified_header(token)
    except pyjwt.InvalidTokenError as exc:
        raise Unauthorized(
            f"token header is invalid: {exc}",
            code="INVALID_TOKEN",
            details={},
        ) from exc

    kid = header.get("kid")
    if not kid:
        raise Unauthorized(
            "RS256 token is missing 'kid' header",
            code="INVALID_TOKEN",
            details={},
        )

    cache = _get_jwks_cache(settings)
    pem = await cache.get(kid)
    try:
        payload = pyjwt.decode(
            token,
            pem,
            algorithms=[RS256],
            audience=settings.jwt_audience,
            options={"require": ["exp", "iat", "jti", "sub", "aud"]},
        )
    except pyjwt.ExpiredSignatureError as exc:
        raise Unauthorized(
            "token has expired",
            code="TOKEN_EXPIRED",
            details={},
        ) from exc
    except pyjwt.InvalidTokenError as exc:
        raise Unauthorized(
            f"token is invalid: {exc}",
            code="INVALID_TOKEN",
            details={},
        ) from exc

    return AuthContext(
        tenant_id=payload.get("tenant_id") or "default",
        agent_id=payload.get("agent_id") or payload.get("sub"),
        scopes=list(payload.get("scopes") or []),
        jti=payload.get("jti"),
        authenticated=True,
    )


async def _verify_locally_async(token: str, settings: Settings) -> AuthContext:
    """Pick the algorithm based on the token's ``alg`` header."""

    try:
        header = pyjwt.get_unverified_header(token)
    except pyjwt.InvalidTokenError as exc:
        raise Unauthorized(
            f"token header is invalid: {exc}",
            code="INVALID_TOKEN",
            details={},
        ) from exc
    alg = header.get("alg")
    if alg == HS256:
        return _verify_hs256(token, settings)
    if alg == RS256:
        return await _verify_rs256(token, settings)
    raise Unauthorized(
        f"unsupported JWT algorithm {alg!r}",
        code="INVALID_TOKEN",
        details={"alg": alg},
    )


# ---------------------------------------------------------------------------
# verify_remote (synchronous httpx — unchanged)


def _verify_remotely(token: str, settings: Settings) -> AuthContext:
    url = f"{settings.identity_url.rstrip('/')}/v1/tokens/verify"
    try:
        response = httpx.post(
            url,
            json={"token": token},
            timeout=settings.auth_remote_timeout_seconds,
        )
    except httpx.HTTPError as exc:
        raise Unauthorized(
            "identity service unreachable",
            code="INVALID_TOKEN",
            details={"reason": str(exc)},
        ) from exc

    if response.status_code == 401:
        body = _safe_json(response)
        err = (body or {}).get("error", {})
        raise Unauthorized(
            err.get("message") or "token rejected by identity service",
            code=err.get("code") or "INVALID_TOKEN",
            details=err.get("details") or {},
        )
    if response.status_code != 200:
        raise Unauthorized(
            "identity service returned an unexpected status",
            code="INVALID_TOKEN",
            details={"status": response.status_code},
        )

    claims = response.json()
    return AuthContext(
        tenant_id=claims.get("tenant_id") or "default",
        agent_id=claims.get("agent_id") or claims.get("sub"),
        scopes=list(claims.get("scopes") or []),
        jti=claims.get("jti"),
        authenticated=True,
    )


def _safe_json(response: httpx.Response):
    try:
        return response.json()
    except ValueError:
        return None


# ---------------------------------------------------------------------------
# Sync facade + async dispatcher


def _verify_locally(token: str, settings: Settings) -> AuthContext:
    """Sync HS256-only verifier kept for back-compat with v0.3 callers."""

    try:
        header = pyjwt.get_unverified_header(token)
    except pyjwt.InvalidTokenError as exc:
        raise Unauthorized(
            f"token header is invalid: {exc}",
            code="INVALID_TOKEN",
            details={},
        ) from exc
    alg = header.get("alg")
    if alg == HS256 or alg is None:
        return _verify_hs256(token, settings)
    raise Unauthorized(
        "RS256 verification requires the async path; "
        "callers must use extract_auth_context_async",
        code="INVALID_TOKEN",
        details={"alg": alg},
    )


def extract_auth_context(
    authorization: str | None,
    settings: Settings,
) -> AuthContext:
    """Sync top-level dispatcher (HS256 only).

    Preserved for v0.3 callers and tests. New code should prefer
    :func:`extract_auth_context_async`, which accepts both algorithms.
    """

    mode = settings.auth_mode

    if mode == "permissive":
        token = _bearer_token(authorization)
        if token and settings.jwt_secret_value:
            try:
                return _verify_locally(token, settings)
            except Unauthorized:
                return AuthContext()
        return AuthContext()

    token = _bearer_token(authorization)
    if not token:
        raise Unauthorized(
            "missing or invalid bearer token",
            details={},
        )

    if mode == "verify_local":
        return _verify_locally(token, settings)
    if mode == "verify_remote":
        return _verify_remotely(token, settings)

    raise Unauthorized(
        f"unknown auth mode: {mode!r}",
        details={"auth_mode": mode},
    )


async def extract_auth_context_async(
    authorization: str | None,
    settings: Settings,
) -> AuthContext:
    """Async dispatcher; accepts both HS256 and RS256 tokens."""

    mode = settings.auth_mode

    if mode == "permissive":
        token = _bearer_token(authorization)
        if not token:
            return AuthContext()
        try:
            return await _verify_locally_async(token, settings)
        except Unauthorized:
            return AuthContext()

    token = _bearer_token(authorization)
    if not token:
        raise Unauthorized(
            "missing or invalid bearer token",
            details={},
        )

    if mode == "verify_local":
        return await _verify_locally_async(token, settings)
    if mode == "verify_remote":
        return _verify_remotely(token, settings)

    raise Unauthorized(
        f"unknown auth mode: {mode!r}",
        details={"auth_mode": mode},
    )


__all__ = [
    "AuthContext",
    "JWKSCache",
    "extract_auth_context",
    "extract_auth_context_async",
    "reset_jwks_cache",
]
