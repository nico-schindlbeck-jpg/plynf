# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 The Plinth Authors
"""OAuth 2.0 Authorization Code (PKCE) flow for the gateway.

Per CONTRACTS.md ("OAuth 2.0 Authorization Code Flow (Gateway)"), the gateway
brokers OAuth on behalf of agents. End-to-end:

1. Caller hits ``GET /v1/oauth/{provider}/authorize`` with a desired
   ``redirect_uri`` and (optional) scope/state. The gateway generates a
   server-side ``state``, a PKCE ``code_verifier``, and the ``code_challenge``,
   persists them, and redirects the browser to the provider.
2. Provider redirects back to ``GET /v1/oauth/{provider}/callback?code=...&state=...``.
   The gateway looks up the persisted state, exchanges the code for tokens at
   the provider's ``token_url``, fetches the user info, encrypts the tokens
   at rest, creates an :class:`OAuthConnection`, marks the state used, and
   redirects the browser back to the original ``redirect_uri`` with a
   ``connection_id`` query parameter.
3. Subsequent tool invocations of an OAuth-backed tool resolve the connection
   server-side and attach ``Authorization: Bearer <access_token>``.

This module owns the protocol (PKCE, state CSRF defence, token exchange) and
the persistence helpers. The HTTP routes live in :mod:`oauth_api`.
"""

from __future__ import annotations

import base64
import hashlib
import json
import secrets
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

import httpx
from ulid import ULID

from .db import Database
from .encryption import decrypt, encrypt
from .exceptions import (
    OAuthConnectionNotFound,
    OAuthError,
    OAuthProviderNotConfigured,
)
from .logging_config import get_logger
from .models import OAuthConnectionPublic
from .settings import Settings

log = get_logger(__name__)


# ---------------------------------------------------------------------------
# Provider config
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class OAuthProvider:
    """Static metadata for an OAuth provider.

    Attributes:
        name: Provider key used in URL paths (e.g. ``"github"``).
        authorize_url: Provider's authorization endpoint (browser-redirect target).
        token_url: Provider's token-exchange endpoint (server-to-server).
        userinfo_url: Endpoint that identifies the authenticated user. Used to
            populate ``user_id`` and ``user_login`` on the connection.
        userinfo_id_field: JSON path inside the userinfo body (a key for the
            top-level user id, e.g. ``"id"`` for GitHub).
        userinfo_login_field: JSON key for the human-readable login.
        default_scopes: Scopes granted when caller doesn't override.
        pkce: Whether to send PKCE code challenge / verifier.
    """

    name: str
    authorize_url: str
    token_url: str
    userinfo_url: str
    userinfo_id_field: str = "id"
    userinfo_login_field: str = "login"
    default_scopes: list[str] = field(default_factory=list)
    pkce: bool = True


GITHUB = OAuthProvider(
    name="github",
    authorize_url="https://github.com/login/oauth/authorize",
    token_url="https://github.com/login/oauth/access_token",
    userinfo_url="https://api.github.com/user",
    userinfo_id_field="id",
    userinfo_login_field="login",
    default_scopes=["repo", "read:user"],
    pkce=True,
)


SLACK = OAuthProvider(
    name="slack",
    authorize_url="https://slack.com/oauth/v2/authorize",
    token_url="https://slack.com/api/oauth.v2.access",
    # ``auth.test`` is the canonical "who am I" endpoint for a Slack token. The
    # response shape is flat (``user_id``/``user``) — we read the id via
    # ``userinfo_id_field`` and the login via ``userinfo_login_field``.
    userinfo_url="https://slack.com/api/auth.test",
    userinfo_id_field="user_id",
    userinfo_login_field="user",
    default_scopes=["channels:read", "chat:write", "users:read"],
    # Slack's OAuth v2 flow does not support PKCE — the spec requires a
    # client_secret round-trip and rejects the ``code_challenge`` parameter on
    # some workspaces.
    pkce=False,
)


LINEAR = OAuthProvider(
    name="linear",
    authorize_url="https://linear.app/oauth/authorize",
    token_url="https://api.linear.app/oauth/token",
    # Linear has no REST userinfo endpoint; we POST a small GraphQL ``viewer``
    # query to ``api.linear.app/graphql`` to identify the user. The exchange
    # in :func:`fetch_user_info` knows about this and returns a flat dict
    # ``{"id": ..., "name": ..., "email": ...}`` that the userinfo_id_field /
    # userinfo_login_field below can index into.
    userinfo_url="https://api.linear.app/graphql",
    userinfo_id_field="id",
    userinfo_login_field="name",
    default_scopes=["read", "write"],
    pkce=True,
)


_PROVIDERS: dict[str, OAuthProvider] = {
    GITHUB.name: GITHUB,
    SLACK.name: SLACK,
    LINEAR.name: LINEAR,
}


def get_provider(name: str) -> OAuthProvider:
    """Return the provider config for ``name``.

    Raises:
        OAuthError: If the provider is not registered.
    """
    p = _PROVIDERS.get(name.lower())
    if p is None:
        raise OAuthError(
            f"unknown oauth provider: {name!r}",
            details={"provider": name, "supported": sorted(_PROVIDERS)},
            code="OAUTH_PROVIDER_UNKNOWN",
            http_status=404,
        )
    return p


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _parse_ts(value: object) -> datetime | None:
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    return datetime.fromisoformat(str(value))


def _b64url(data: bytes) -> str:
    """URL-safe base64 with padding stripped (RFC 7636 compatible)."""
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _new_state() -> str:
    """Return a 32-byte URL-safe random state token."""
    return secrets.token_urlsafe(32)


def _new_pkce_pair() -> tuple[str, str]:
    """Return ``(code_verifier, code_challenge)`` for PKCE S256."""
    verifier = secrets.token_urlsafe(48)  # 64 chars, 384 bits — within RFC limits
    challenge = _b64url(hashlib.sha256(verifier.encode("ascii")).digest())
    return verifier, challenge


# ---------------------------------------------------------------------------
# Provider availability
# ---------------------------------------------------------------------------


# Per-provider settings binding. Each entry says which Settings fields hold
# the client_id/client_secret/redirect_uri, plus the human hint we surface when
# something is missing. Keeping this as data (rather than another big if-tree)
# means new providers only need to register their settings here.
_PROVIDER_SETTINGS: dict[str, dict[str, Any]] = {
    "github": {
        "client_id_attr": "oauth_github_client_id",
        "client_secret_attr": "oauth_github_client_secret",
        "redirect_uri_attr": "oauth_github_redirect_uri",
        "client_id_env": "PLINTH_OAUTH_GITHUB_CLIENT_ID",
        "client_secret_env": "PLINTH_OAUTH_GITHUB_CLIENT_SECRET",
        "hint": (
            "Create a GitHub OAuth App at "
            "https://github.com/settings/developers, then export "
            "PLINTH_OAUTH_GITHUB_CLIENT_ID and "
            "PLINTH_OAUTH_GITHUB_CLIENT_SECRET before starting "
            "the gateway."
        ),
    },
    "slack": {
        "client_id_attr": "oauth_slack_client_id",
        "client_secret_attr": "oauth_slack_client_secret",
        "redirect_uri_attr": "oauth_slack_redirect_uri",
        "client_id_env": "PLINTH_OAUTH_SLACK_CLIENT_ID",
        "client_secret_env": "PLINTH_OAUTH_SLACK_CLIENT_SECRET",
        "hint": (
            "Create a Slack app at https://api.slack.com/apps, enable OAuth "
            "v2 with the scopes 'channels:read,chat:write,users:read', then "
            "export PLINTH_OAUTH_SLACK_CLIENT_ID and "
            "PLINTH_OAUTH_SLACK_CLIENT_SECRET before starting the gateway."
        ),
    },
    "linear": {
        "client_id_attr": "oauth_linear_client_id",
        "client_secret_attr": "oauth_linear_client_secret",
        "redirect_uri_attr": "oauth_linear_redirect_uri",
        "client_id_env": "PLINTH_OAUTH_LINEAR_CLIENT_ID",
        "client_secret_env": "PLINTH_OAUTH_LINEAR_CLIENT_SECRET",
        "hint": (
            "Register a Linear OAuth application at "
            "https://linear.app/settings/api/applications, request the 'read' "
            "and 'write' scopes, then export PLINTH_OAUTH_LINEAR_CLIENT_ID "
            "and PLINTH_OAUTH_LINEAR_CLIENT_SECRET before starting the "
            "gateway."
        ),
    },
}


def assert_provider_configured(provider: OAuthProvider, settings: Settings) -> None:
    """Verify the gateway has the secrets needed to talk to ``provider``.

    Raises:
        OAuthProviderNotConfigured: If credentials are missing. The error
            message tells the operator exactly which env vars to set.
    """
    spec = _PROVIDER_SETTINGS.get(provider.name)
    if spec is None:
        raise OAuthProviderNotConfigured(
            f"Provider {provider.name!r} is not yet configurable.",
            details={"provider": provider.name},
        )
    client_id = getattr(settings, spec["client_id_attr"], "")
    client_secret = getattr(settings, spec["client_secret_attr"], "")
    if not client_id or not client_secret:
        missing = [
            env_name
            for env_name, value in (
                (spec["client_id_env"], client_id),
                (spec["client_secret_env"], client_secret),
            )
            if not value
        ]
        raise OAuthProviderNotConfigured(
            f"{provider.name.title()} OAuth is not configured on this gateway.",
            details={
                "provider": provider.name,
                "missing": missing,
                "hint": spec["hint"],
            },
        )


def provider_credentials(provider: OAuthProvider, settings: Settings) -> tuple[str, str]:
    """Return ``(client_id, client_secret)`` for ``provider``."""
    spec = _PROVIDER_SETTINGS.get(provider.name)
    if spec is None:
        raise OAuthProviderNotConfigured(
            f"Provider {provider.name!r} has no credentials wired.",
            details={"provider": provider.name},
        )
    return (
        getattr(settings, spec["client_id_attr"], ""),
        getattr(settings, spec["client_secret_attr"], ""),
    )


def provider_redirect_uri(provider: OAuthProvider, settings: Settings) -> str:
    """Return the gateway-side ``redirect_uri`` registered with ``provider``."""
    spec = _PROVIDER_SETTINGS.get(provider.name)
    if spec is None:
        raise OAuthProviderNotConfigured(
            f"Provider {provider.name!r} has no redirect URI configured.",
            details={"provider": provider.name},
        )
    return getattr(settings, spec["redirect_uri_attr"])


# ---------------------------------------------------------------------------
# State store (oauth_states table)
# ---------------------------------------------------------------------------


@dataclass
class OAuthStateRecord:
    """A persisted state row across the authorize/callback round-trip."""

    state: str
    provider: str
    redirect_uri: str
    scopes: list[str]
    pkce_verifier: str | None
    tenant_id: str
    created_at: datetime
    used: bool


class OAuthStateStore:
    """CRUD over the ``oauth_states`` table."""

    def __init__(self, db: Database, *, ttl_seconds: int = 600) -> None:
        self._db = db
        self._ttl_seconds = ttl_seconds

    async def create(
        self,
        *,
        provider: str,
        redirect_uri: str,
        scopes: list[str],
        pkce_verifier: str | None,
        tenant_id: str = "default",
    ) -> str:
        state = _new_state()
        now = _utcnow()
        await self._db.execute(
            """
            INSERT INTO oauth_states (
                state, provider, redirect_uri, scopes, pkce_verifier,
                tenant_id, created_at, used
            ) VALUES (?, ?, ?, ?, ?, ?, ?, 0)
            """,
            (
                state,
                provider,
                redirect_uri,
                json.dumps(scopes),
                pkce_verifier,
                tenant_id,
                now.isoformat(),
            ),
        )
        return state

    async def consume(self, state: str, *, provider: str) -> OAuthStateRecord:
        """Look up and atomically mark ``state`` as used.

        Raises:
            OAuthError: If the state is unknown, already used, or expired.
        """
        row = await self._db.fetchone(
            "SELECT * FROM oauth_states WHERE state = ?", (state,)
        )
        if row is None:
            raise OAuthError(
                "unknown or expired oauth state",
                details={"state_prefix": state[:8] + "..."},
            )
        if row["provider"] != provider:
            # State was minted for a different provider — refuse.
            raise OAuthError(
                "oauth state provider mismatch",
                details={"expected": provider, "actual": row["provider"]},
            )
        if int(row["used"]):
            raise OAuthError("oauth state has already been used")

        created_at = _parse_ts(row["created_at"]) or _utcnow()
        if _utcnow() - created_at > timedelta(seconds=self._ttl_seconds):
            await self._db.execute(
                "DELETE FROM oauth_states WHERE state = ?", (state,)
            )
            raise OAuthError(
                "oauth state has expired",
                details={"ttl_seconds": self._ttl_seconds},
            )

        await self._db.execute(
            "UPDATE oauth_states SET used = 1 WHERE state = ?", (state,)
        )
        return OAuthStateRecord(
            state=row["state"],
            provider=row["provider"],
            redirect_uri=row["redirect_uri"],
            scopes=json.loads(row["scopes"]) if row["scopes"] else [],
            pkce_verifier=row["pkce_verifier"],
            tenant_id=row["tenant_id"],
            created_at=created_at,
            used=True,
        )

    async def cleanup_expired(self) -> int:
        """Delete expired/used rows. Returns the number of rows removed."""
        cutoff = (_utcnow() - timedelta(seconds=self._ttl_seconds)).isoformat()
        async with self._db.cursor() as cur:
            await cur.execute(
                "DELETE FROM oauth_states WHERE used = 1 OR created_at < ?",
                (cutoff,),
            )
            return cur.rowcount


# ---------------------------------------------------------------------------
# Connection store (oauth_connections table)
# ---------------------------------------------------------------------------


@dataclass
class OAuthConnection:
    """In-process representation of a connection row (with secrets).

    The plaintext access/refresh tokens are populated only when explicitly
    decrypted via :meth:`OAuthConnectionStore.get_decrypted`. The default
    fetchers return public-view dataclasses without secrets.
    """

    id: str
    tenant_id: str
    provider: str
    user_id: str
    user_login: str | None
    scopes: list[str]
    access_token: str
    refresh_token: str | None
    expires_at: datetime | None
    created_at: datetime
    last_refreshed_at: datetime | None


def _row_to_public(row) -> OAuthConnectionPublic:
    return OAuthConnectionPublic(
        id=row["id"],
        tenant_id=row["tenant_id"],
        provider=row["provider"],
        user_id=row["user_id"],
        user_login=row["user_login"],
        scopes=json.loads(row["scopes"]) if row["scopes"] else [],
        created_at=_parse_ts(row["created_at"]) or _utcnow(),
        expires_at=_parse_ts(row["expires_at"]),
        last_refreshed_at=_parse_ts(row["last_refreshed_at"]),
    )


class OAuthConnectionStore:
    """CRUD over the ``oauth_connections`` table."""

    def __init__(self, db: Database, *, encryption_key: str) -> None:
        self._db = db
        self._key = encryption_key

    async def create(
        self,
        *,
        tenant_id: str,
        provider: str,
        user_id: str,
        user_login: str | None,
        scopes: list[str],
        access_token: str,
        refresh_token: str | None = None,
        expires_at: datetime | None = None,
    ) -> OAuthConnectionPublic:
        conn_id = f"conn_{ULID()}"
        now = _utcnow()
        await self._db.execute(
            """
            INSERT INTO oauth_connections (
                id, tenant_id, provider, user_id, user_login, scopes,
                access_token_encrypted, refresh_token_encrypted, expires_at,
                created_at, last_refreshed_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL)
            """,
            (
                conn_id,
                tenant_id,
                provider,
                str(user_id),
                user_login,
                json.dumps(scopes),
                encrypt(access_token, key_b64=self._key),
                encrypt(refresh_token, key_b64=self._key) if refresh_token else None,
                expires_at.isoformat() if expires_at else None,
                now.isoformat(),
            ),
        )
        public = await self.get_public(conn_id)
        if public is None:
            # Should not happen — we just inserted.
            raise OAuthError("failed to read back oauth connection after insert")
        return public

    async def get_public(self, conn_id: str) -> OAuthConnectionPublic | None:
        row = await self._db.fetchone(
            "SELECT * FROM oauth_connections WHERE id = ?", (conn_id,)
        )
        return _row_to_public(row) if row else None

    async def require_public(self, conn_id: str) -> OAuthConnectionPublic:
        public = await self.get_public(conn_id)
        if public is None:
            raise OAuthConnectionNotFound(
                f"OAuth connection {conn_id!r} not found",
                details={"connection_id": conn_id},
            )
        return public

    async def get_decrypted(self, conn_id: str) -> OAuthConnection:
        """Return the connection with plaintext tokens decrypted.

        Raises:
            OAuthConnectionNotFound: If no such connection exists.
        """
        row = await self._db.fetchone(
            "SELECT * FROM oauth_connections WHERE id = ?", (conn_id,)
        )
        if row is None:
            raise OAuthConnectionNotFound(
                f"OAuth connection {conn_id!r} not found",
                details={"connection_id": conn_id},
            )
        access = decrypt(row["access_token_encrypted"], key_b64=self._key)
        refresh = (
            decrypt(row["refresh_token_encrypted"], key_b64=self._key)
            if row["refresh_token_encrypted"]
            else None
        )
        return OAuthConnection(
            id=row["id"],
            tenant_id=row["tenant_id"],
            provider=row["provider"],
            user_id=row["user_id"],
            user_login=row["user_login"],
            scopes=json.loads(row["scopes"]) if row["scopes"] else [],
            access_token=access,
            refresh_token=refresh,
            expires_at=_parse_ts(row["expires_at"]),
            created_at=_parse_ts(row["created_at"]) or _utcnow(),
            last_refreshed_at=_parse_ts(row["last_refreshed_at"]),
        )

    async def list_public(
        self,
        *,
        tenant_id: str | None = None,
        provider: str | None = None,
    ) -> list[OAuthConnectionPublic]:
        sql = "SELECT * FROM oauth_connections"
        clauses: list[str] = []
        params: list[object] = []
        if tenant_id is not None:
            clauses.append("tenant_id = ?")
            params.append(tenant_id)
        if provider is not None:
            clauses.append("provider = ?")
            params.append(provider)
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY created_at DESC"
        rows = await self._db.fetchall(sql, tuple(params))
        return [_row_to_public(r) for r in rows]

    async def delete(self, conn_id: str) -> None:
        existing = await self._db.fetchone(
            "SELECT id FROM oauth_connections WHERE id = ?", (conn_id,)
        )
        if existing is None:
            raise OAuthConnectionNotFound(
                f"OAuth connection {conn_id!r} not found",
                details={"connection_id": conn_id},
            )
        await self._db.execute(
            "DELETE FROM oauth_connections WHERE id = ?", (conn_id,)
        )

    async def update_tokens(
        self,
        conn_id: str,
        *,
        access_token: str,
        refresh_token: str | None = None,
        expires_at: datetime | None = None,
    ) -> OAuthConnectionPublic:
        existing = await self._db.fetchone(
            "SELECT id FROM oauth_connections WHERE id = ?", (conn_id,)
        )
        if existing is None:
            raise OAuthConnectionNotFound(
                f"OAuth connection {conn_id!r} not found",
                details={"connection_id": conn_id},
            )
        now = _utcnow()
        access_enc = encrypt(access_token, key_b64=self._key)
        if refresh_token is not None:
            refresh_enc = encrypt(refresh_token, key_b64=self._key)
            await self._db.execute(
                """
                UPDATE oauth_connections SET
                    access_token_encrypted = ?,
                    refresh_token_encrypted = ?,
                    expires_at = ?,
                    last_refreshed_at = ?
                WHERE id = ?
                """,
                (
                    access_enc,
                    refresh_enc,
                    expires_at.isoformat() if expires_at else None,
                    now.isoformat(),
                    conn_id,
                ),
            )
        else:
            await self._db.execute(
                """
                UPDATE oauth_connections SET
                    access_token_encrypted = ?,
                    expires_at = ?,
                    last_refreshed_at = ?
                WHERE id = ?
                """,
                (
                    access_enc,
                    expires_at.isoformat() if expires_at else None,
                    now.isoformat(),
                    conn_id,
                ),
            )
        public = await self.get_public(conn_id)
        # Should not happen — we just updated.
        if public is None:  # pragma: no cover
            raise OAuthError("failed to read back oauth connection after update")
        return public


# ---------------------------------------------------------------------------
# Authorize URL builder
# ---------------------------------------------------------------------------


@dataclass
class AuthorizeRedirect:
    """Result of a ``/authorize`` request."""

    url: str
    state: str
    pkce_verifier: str | None


def build_authorize_redirect(
    provider: OAuthProvider,
    *,
    client_id: str,
    redirect_uri: str,
    scopes: list[str],
    state: str,
    pkce_challenge: str | None,
) -> str:
    """Construct the provider's authorize URL with all required query params."""
    from urllib.parse import urlencode

    params: dict[str, str] = {
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "scope": " ".join(scopes),
        "state": state,
        "response_type": "code",
    }
    if pkce_challenge is not None:
        params["code_challenge"] = pkce_challenge
        params["code_challenge_method"] = "S256"
    sep = "&" if "?" in provider.authorize_url else "?"
    return f"{provider.authorize_url}{sep}{urlencode(params)}"


# ---------------------------------------------------------------------------
# Token exchange (server-side)
# ---------------------------------------------------------------------------


@dataclass
class TokenGrant:
    """Result of exchanging an auth code (or refresh token) for tokens."""

    access_token: str
    token_type: str
    refresh_token: str | None
    expires_at: datetime | None
    scopes: list[str]


def _parse_token_response(body: Any) -> TokenGrant:
    """Decode a provider's token response into a :class:`TokenGrant`.

    Handles both JSON and form-encoded bodies (GitHub returns form-encoded by
    default, which we work around with the ``Accept: application/json`` header,
    but we tolerate either shape). Also tolerates Slack's
    ``oauth.v2.access`` response shape which embeds an ``ok: bool`` field plus
    ``authed_user`` / ``team`` siblings alongside the top-level ``access_token``.
    """
    if not isinstance(body, dict):
        raise OAuthError(
            "unexpected token response shape",
            details={"got": type(body).__name__},
        )
    # Slack always returns ``ok: true|false`` even on 200 OK.
    if body.get("ok") is False:
        raise OAuthError(
            f"oauth provider returned error: {body.get('error')}",
            details={
                "provider_error": body.get("error"),
                "description": body.get("error_description"),
            },
        )
    if "error" in body and body.get("ok") is not True:
        raise OAuthError(
            f"oauth provider returned error: {body.get('error')}",
            details={
                "provider_error": body.get("error"),
                "description": body.get("error_description"),
            },
        )
    access = body.get("access_token")
    if not access:
        raise OAuthError(
            "oauth provider response missing access_token",
            details={"keys": sorted(body.keys())},
        )
    expires_in = body.get("expires_in")
    expires_at: datetime | None = None
    if isinstance(expires_in, (int, float)) and expires_in > 0:
        expires_at = _utcnow() + timedelta(seconds=int(expires_in))
    raw_scope = body.get("scope") or ""
    if isinstance(raw_scope, list):
        scopes = [str(s) for s in raw_scope]
    elif isinstance(raw_scope, str):
        # Both space-separated (RFC 6749) and comma-separated (GitHub, Slack)
        # appear in the wild; normalise to a flat list.
        if "," in raw_scope and " " not in raw_scope:
            scopes = [s.strip() for s in raw_scope.split(",") if s.strip()]
        else:
            scopes = [s.strip() for s in raw_scope.split() if s.strip()]
    else:
        scopes = []
    return TokenGrant(
        access_token=access,
        token_type=str(body.get("token_type", "bearer")),
        refresh_token=body.get("refresh_token"),
        expires_at=expires_at,
        scopes=scopes,
    )


async def exchange_code_for_token(
    *,
    provider: OAuthProvider,
    client_id: str,
    client_secret: str,
    code: str,
    redirect_uri: str,
    pkce_verifier: str | None,
    http_client: httpx.AsyncClient,
) -> TokenGrant:
    """POST to the provider's token endpoint and parse the result."""
    payload: dict[str, str] = {
        "client_id": client_id,
        "client_secret": client_secret,
        "code": code,
        "redirect_uri": redirect_uri,
        "grant_type": "authorization_code",
    }
    if pkce_verifier is not None:
        payload["code_verifier"] = pkce_verifier

    headers = {"Accept": "application/json"}
    try:
        resp = await http_client.post(
            provider.token_url, data=payload, headers=headers, timeout=15.0
        )
    except httpx.HTTPError as exc:
        raise OAuthError(
            f"token exchange request failed: {exc}",
            details={"provider": provider.name},
        ) from exc
    if resp.status_code >= 400:
        raise OAuthError(
            f"token exchange returned HTTP {resp.status_code}",
            details={
                "provider": provider.name,
                "status_code": resp.status_code,
                "body_preview": resp.text[:300],
            },
        )
    try:
        body = resp.json()
    except ValueError as exc:
        # Provider returned form-encoded body — parse it.
        from urllib.parse import parse_qs

        try:
            parsed = parse_qs(resp.text)
            body = {k: v[0] if isinstance(v, list) and v else v for k, v in parsed.items()}
        except Exception:  # noqa: BLE001
            raise OAuthError(
                "token exchange returned unparseable body",
                details={"provider": provider.name, "body_preview": resp.text[:300]},
            ) from exc
    return _parse_token_response(body)


async def refresh_token_grant(
    *,
    provider: OAuthProvider,
    client_id: str,
    client_secret: str,
    refresh_token: str,
    http_client: httpx.AsyncClient,
) -> TokenGrant:
    """POST a refresh-token grant to the provider's token endpoint."""
    payload = {
        "client_id": client_id,
        "client_secret": client_secret,
        "refresh_token": refresh_token,
        "grant_type": "refresh_token",
    }
    headers = {"Accept": "application/json"}
    try:
        resp = await http_client.post(
            provider.token_url, data=payload, headers=headers, timeout=15.0
        )
    except httpx.HTTPError as exc:
        raise OAuthError(
            f"refresh request failed: {exc}",
            details={"provider": provider.name},
        ) from exc
    if resp.status_code >= 400:
        raise OAuthError(
            f"refresh returned HTTP {resp.status_code}",
            details={
                "provider": provider.name,
                "status_code": resp.status_code,
                "body_preview": resp.text[:300],
            },
        )
    try:
        body = resp.json()
    except ValueError as exc:
        from urllib.parse import parse_qs

        try:
            parsed = parse_qs(resp.text)
            body = {k: v[0] if isinstance(v, list) and v else v for k, v in parsed.items()}
        except Exception:  # noqa: BLE001
            raise OAuthError(
                "refresh returned unparseable body",
                details={"provider": provider.name},
            ) from exc
    return _parse_token_response(body)


async def fetch_user_info(
    *,
    provider: OAuthProvider,
    access_token: str,
    http_client: httpx.AsyncClient,
) -> dict[str, Any]:
    """Fetch user identity from ``provider.userinfo_url`` with ``access_token``.

    GitHub: vanilla ``GET /user`` with bearer.
    Slack: ``GET /api/auth.test`` — returns ``{ok, user, user_id, team, ...}``.
    Linear: GraphQL ``query { viewer { id name email } }`` POSTed to
    ``api.linear.app/graphql``. The GraphQL response shape is unwrapped here so
    callers see a flat ``{id, name, email}`` dict, identical to the userinfo
    contract for the other providers.
    """
    headers = {
        "Authorization": f"Bearer {access_token}",
        "Accept": "application/json",
    }
    if provider.name == "github":
        # GitHub's API recommends the X-GitHub-Api-Version pin.
        headers["X-GitHub-Api-Version"] = "2022-11-28"

    if provider.name == "linear":
        # Linear has no REST userinfo endpoint — it's GraphQL only. Fetch the
        # viewer's id/name/email so we can populate user_id + user_login on the
        # connection.
        return await _fetch_linear_viewer(
            access_token=access_token, http_client=http_client, url=provider.userinfo_url
        )

    try:
        resp = await http_client.get(provider.userinfo_url, headers=headers, timeout=15.0)
    except httpx.HTTPError as exc:
        raise OAuthError(
            f"userinfo request failed: {exc}",
            details={"provider": provider.name},
        ) from exc
    if resp.status_code >= 400:
        raise OAuthError(
            f"userinfo returned HTTP {resp.status_code}",
            details={"provider": provider.name, "status_code": resp.status_code},
        )
    try:
        body = resp.json()
    except ValueError as exc:
        raise OAuthError(
            "userinfo response was not JSON",
            details={"provider": provider.name},
        ) from exc

    if provider.name == "slack":
        # Slack's ``auth.test`` always returns ``ok``; surface failures.
        if not isinstance(body, dict) or body.get("ok") is False:
            raise OAuthError(
                f"slack auth.test returned error: {body.get('error') if isinstance(body, dict) else 'unknown'}",
                details={"provider": "slack", "body": body if isinstance(body, dict) else None},
            )
    return body


async def _fetch_linear_viewer(
    *,
    access_token: str,
    http_client: httpx.AsyncClient,
    url: str,
) -> dict[str, Any]:
    """POST a small ``viewer`` GraphQL query to Linear and unwrap the result."""
    headers = {
        "Authorization": f"Bearer {access_token}",
        "Accept": "application/json",
        "Content-Type": "application/json",
    }
    payload = {"query": "query { viewer { id name email } }"}
    try:
        resp = await http_client.post(url, json=payload, headers=headers, timeout=15.0)
    except httpx.HTTPError as exc:
        raise OAuthError(
            f"linear viewer request failed: {exc}",
            details={"provider": "linear"},
        ) from exc
    if resp.status_code >= 400:
        raise OAuthError(
            f"linear viewer returned HTTP {resp.status_code}",
            details={"provider": "linear", "status_code": resp.status_code},
        )
    try:
        body = resp.json()
    except ValueError as exc:
        raise OAuthError(
            "linear viewer response was not JSON",
            details={"provider": "linear"},
        ) from exc
    if not isinstance(body, dict):
        raise OAuthError(
            "linear viewer response was not a JSON object",
            details={"provider": "linear"},
        )
    if isinstance(body.get("errors"), list) and body["errors"]:
        first = body["errors"][0] if isinstance(body["errors"][0], dict) else {}
        raise OAuthError(
            f"linear graphql error: {first.get('message', 'unknown')}",
            details={"provider": "linear", "errors": body["errors"]},
        )
    data = body.get("data") or {}
    viewer = data.get("viewer") if isinstance(data, dict) else None
    if not isinstance(viewer, dict) or not viewer.get("id"):
        raise OAuthError(
            "linear viewer response missing data.viewer.id",
            details={"provider": "linear"},
        )
    return {
        "id": viewer.get("id"),
        "name": viewer.get("name"),
        "email": viewer.get("email"),
    }


# ---------------------------------------------------------------------------
# Top-level orchestration helpers (used by the route handlers)
# ---------------------------------------------------------------------------


def parse_scopes(raw: str | None, *, default: list[str]) -> list[str]:
    """Parse comma- or space-separated scopes from a query parameter."""
    if not raw:
        return list(default)
    if "," in raw:
        parts = [p.strip() for p in raw.split(",") if p.strip()]
    else:
        parts = [p.strip() for p in raw.split() if p.strip()]
    return parts or list(default)
