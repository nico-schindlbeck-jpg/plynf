# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 The Plinth Authors
"""JWT issue + verify helpers.

The :class:`TokenManager` wraps issuance + verification with the configured
algorithm (HS256 or RS256), issuer, and audience so callers don't have to
thread the args.

Algorithm dispatch:

* **HS256** — synchronous; existing v0.3 callers and tests pass a
  ``secret`` and call :meth:`TokenManager.issue` / :meth:`decode` directly.
* **RS256** — async; pass a :class:`KeyStore` and call
  :meth:`TokenManager.issue_async` / :meth:`decode_async`. The store
  resolves the active key (issue) or the key matching ``kid`` (verify)
  on every call.

The sync methods preserve the v0.3 API. The async methods add the v0.4
RS256 path. Both branches share the claim-building + payload-defaulting
logic so behaviour stays consistent.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

import jwt as pyjwt
from ulid import ULID

from .exceptions import InvalidArguments, InvalidToken, TokenExpired
from .models import TokenClaims

UTC = timezone.utc  # noqa: UP017
JWT_ALG = "HS256"  # historical alias for callers/tests pinned to HS256
HS256 = "HS256"
RS256 = "RS256"


def new_jti() -> str:
    """Return a fresh ``jti_<ulid>`` identifier."""

    return f"jti_{ULID()}"


@dataclass
class IssuedToken:
    """A freshly minted token plus its decoded claims."""

    token: str
    claims: TokenClaims


def _build_payload(
    *,
    agent_id: str,
    tenant_id: str,
    scopes: list[str],
    workspace_id: str | None,
    ttl_seconds: int,
    rate_limit: dict[str, Any] | None,
    jti: str | None,
    now: datetime | None,
    issuer: str,
    audience: str,
) -> tuple[dict[str, Any], TokenClaims]:
    """Shared claim-builder used by both HS256 and RS256 issue paths."""

    if ttl_seconds < 1:
        raise InvalidArguments(
            "ttl_seconds must be >= 1",
            details={"ttl_seconds": ttl_seconds},
        )

    issued_at = now or datetime.now(UTC)
    issued_at = issued_at.replace(microsecond=0)
    expires_at = issued_at + timedelta(seconds=ttl_seconds)
    token_jti = jti or new_jti()

    payload: dict[str, Any] = {
        "sub": agent_id,
        "iss": issuer,
        "aud": audience,
        "iat": int(issued_at.timestamp()),
        "exp": int(expires_at.timestamp()),
        "jti": token_jti,
        "agent_id": agent_id,
        "tenant_id": tenant_id,
        "workspace_id": workspace_id,
        "scopes": list(scopes),
        "rate_limit": rate_limit,
    }
    return payload, TokenClaims(**payload)


def _normalize_token(token: str | bytes) -> str:
    if isinstance(token, bytes):  # pragma: no cover - defensive
        return token.decode("ascii")
    return token


def _payload_to_claims(payload: dict[str, Any]) -> TokenClaims:
    """Apply the defensive defaults + Pydantic validation shared by all paths."""

    payload.setdefault("agent_id", payload.get("sub", ""))
    payload.setdefault("tenant_id", "default")
    payload.setdefault("scopes", [])
    payload.setdefault("workspace_id", None)
    payload.setdefault("rate_limit", None)
    try:
        return TokenClaims(**payload)
    except Exception as exc:  # pragma: no cover - defensive
        raise InvalidToken(f"claims failed validation: {exc}") from exc


class TokenManager:
    """Encapsulates the signing key, issuer, and audience for issued JWTs."""

    def __init__(
        self,
        *,
        issuer: str,
        audience: str,
        secret: str | None = None,
        alg: str = HS256,
        key_store: Any | None = None,  # forward-ref to KeyStore
    ) -> None:
        if alg not in (HS256, RS256):
            raise InvalidArguments(
                f"unsupported JWT algorithm {alg!r}",
                details={"alg": alg},
            )
        if alg == HS256 and not secret:
            raise InvalidArguments(
                "TokenManager(HS256) requires a non-empty secret",
                details={"alg": alg},
            )
        if alg == RS256 and key_store is None:
            raise InvalidArguments(
                "TokenManager(RS256) requires a KeyStore",
                details={"alg": alg},
            )

        self._alg = alg
        self._secret = secret or ""
        self._issuer = issuer
        self._audience = audience
        self._key_store = key_store

    @property
    def alg(self) -> str:
        return self._alg

    @property
    def issuer(self) -> str:
        return self._issuer

    @property
    def audience(self) -> str:
        return self._audience

    @property
    def key_store(self) -> Any | None:
        return self._key_store

    # ------------------------------------------------------------------ issue

    def issue(
        self,
        *,
        agent_id: str,
        tenant_id: str,
        scopes: list[str],
        workspace_id: str | None = None,
        ttl_seconds: int = 3600,
        rate_limit: dict[str, Any] | None = None,
        jti: str | None = None,
        now: datetime | None = None,
    ) -> IssuedToken:
        """Mint and sign an HS256 JWT (synchronous).

        Raises:
            InvalidArguments: when called on an RS256 manager (use
                :meth:`issue_async` instead).
        """

        if self._alg != HS256:
            raise InvalidArguments(
                "TokenManager configured for RS256; call issue_async()",
                details={"alg": self._alg},
            )
        payload, claims = _build_payload(
            agent_id=agent_id,
            tenant_id=tenant_id,
            scopes=scopes,
            workspace_id=workspace_id,
            ttl_seconds=ttl_seconds,
            rate_limit=rate_limit,
            jti=jti,
            now=now,
            issuer=self._issuer,
            audience=self._audience,
        )
        token = pyjwt.encode(payload, self._secret, algorithm=HS256)
        return IssuedToken(token=_normalize_token(token), claims=claims)

    async def issue_async(
        self,
        *,
        agent_id: str,
        tenant_id: str,
        scopes: list[str],
        workspace_id: str | None = None,
        ttl_seconds: int = 3600,
        rate_limit: dict[str, Any] | None = None,
        jti: str | None = None,
        now: datetime | None = None,
    ) -> IssuedToken:
        """Async variant — works for both HS256 and RS256.

        For HS256 it just delegates to :meth:`issue`. For RS256 it
        resolves the active signing key from the :class:`KeyStore`,
        decrypts the private PEM, signs the JWT with the matching kid in
        the header.
        """

        if self._alg == HS256:
            return self.issue(
                agent_id=agent_id,
                tenant_id=tenant_id,
                scopes=scopes,
                workspace_id=workspace_id,
                ttl_seconds=ttl_seconds,
                rate_limit=rate_limit,
                jti=jti,
                now=now,
            )

        assert self._key_store is not None  # noqa: S101
        payload, claims = _build_payload(
            agent_id=agent_id,
            tenant_id=tenant_id,
            scopes=scopes,
            workspace_id=workspace_id,
            ttl_seconds=ttl_seconds,
            rate_limit=rate_limit,
            jti=jti,
            now=now,
            issuer=self._issuer,
            audience=self._audience,
        )
        active = await self._key_store.active_key()
        private_pem = await self._key_store.get_private_pem(active.kid)
        token = pyjwt.encode(
            payload,
            private_pem,
            algorithm=RS256,
            headers={"kid": active.kid},
        )
        return IssuedToken(token=_normalize_token(token), claims=claims)

    # ----------------------------------------------------------------- decode

    def decode(self, token: str) -> TokenClaims:
        """Validate signature + standard claims (HS256 only, sync)."""

        if self._alg != HS256:
            raise InvalidArguments(
                "TokenManager configured for RS256; call decode_async()",
                details={"alg": self._alg},
            )
        try:
            payload = pyjwt.decode(
                token,
                self._secret,
                algorithms=[HS256],
                audience=self._audience,
                issuer=self._issuer,
                options={"require": ["exp", "iat", "jti", "sub", "aud", "iss"]},
            )
        except pyjwt.ExpiredSignatureError as exc:
            raise TokenExpired() from exc
        except pyjwt.InvalidTokenError as exc:
            raise InvalidToken(str(exc)) from exc
        return _payload_to_claims(payload)

    async def decode_async(self, token: str) -> TokenClaims:
        """Validate signature + standard claims (HS256 or RS256)."""

        if self._alg == HS256:
            return self.decode(token)

        try:
            unverified_header = pyjwt.get_unverified_header(token)
        except pyjwt.InvalidTokenError as exc:
            raise InvalidToken(str(exc)) from exc

        header_alg = unverified_header.get("alg")
        # Reject confused-deputy attempts: a server in RS256 mode must
        # not accept tokens signed under a different algorithm.
        if header_alg != RS256:
            raise InvalidToken(
                f"token alg {header_alg!r} does not match expected RS256",
            )

        kid = unverified_header.get("kid")
        if not kid:
            raise InvalidToken("RS256 token is missing 'kid' header")
        assert self._key_store is not None  # noqa: S101
        key = await self._key_store.get_by_kid(kid)
        if key is None or key.expires_at <= datetime.now(UTC):
            raise InvalidToken(f"unknown or expired kid {kid!r}")

        try:
            payload = pyjwt.decode(
                token,
                key.public_key_pem.encode("ascii"),
                algorithms=[RS256],
                audience=self._audience,
                issuer=self._issuer,
                options={"require": ["exp", "iat", "jti", "sub", "aud", "iss"]},
            )
        except pyjwt.ExpiredSignatureError as exc:
            raise TokenExpired() from exc
        except pyjwt.InvalidTokenError as exc:
            raise InvalidToken(str(exc)) from exc

        return _payload_to_claims(payload)

    def decode_unverified(self, token: str) -> dict[str, Any]:
        """Return the raw payload without verifying signature/expiry.

        Reserved for diagnostics — never use the result for trust decisions.
        """

        try:
            return pyjwt.decode(token, options={"verify_signature": False})
        except pyjwt.InvalidTokenError as exc:
            raise InvalidToken(str(exc)) from exc


__all__ = [
    "HS256",
    "JWT_ALG",
    "RS256",
    "IssuedToken",
    "TokenManager",
    "new_jti",
]
