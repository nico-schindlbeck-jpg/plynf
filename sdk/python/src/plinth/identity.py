# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 The Plinth Authors
"""Client for the Plinth identity service.

Most apps don't need to touch the identity service directly: a long-lived
``api_key`` is enough. This client is for ops tooling and tests that mint
short-lived capability tokens, verify them out-of-band, or revoke them.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import datetime
from typing import Any, Dict, List, Optional  # noqa: UP035

import httpx
from pydantic import BaseModel, ConfigDict, Field

from ._http import HTTPClient
from .exceptions import (
    InvalidToken,
    PlinthError,
    TokenExpired,
    TokenRevoked,
)
from .models import (
    DeleteConfirmation,
    DeleteJob,
    ExportJob,
    ExportStatus,
    RevocationEntry,
    RevocationList,
    SigningKey,
    TenantQuotas,
    TenantQuotasUpdate,
    TenantUsage,
)

DEFAULT_IDENTITY_URL = "http://localhost:7425"


class TokenClaims(BaseModel):
    """Claims returned from ``POST /v1/tokens/verify`` (or embedded in issue)."""

    model_config = ConfigDict(extra="ignore")

    sub: str
    iss: str
    aud: str
    iat: int
    exp: int
    jti: str
    agent_id: str
    tenant_id: str
    workspace_id: Optional[str] = None  # noqa: UP045
    scopes: List[str] = Field(default_factory=list)  # noqa: UP006
    rate_limit: Optional[Dict[str, Any]] = None  # noqa: UP006, UP045


class TokenIssueResponse(BaseModel):
    """Response from ``POST /v1/tokens``."""

    model_config = ConfigDict(extra="ignore")

    token: str
    jti: str
    expires_at: datetime
    claims: TokenClaims


class TokenInfo(BaseModel):
    """Public introspection view from ``GET /v1/tokens/{jti}``."""

    model_config = ConfigDict(extra="ignore")

    jti: str
    agent_id: str
    tenant_id: str
    workspace_id: Optional[str] = None  # noqa: UP045
    scopes: List[str] = Field(default_factory=list)  # noqa: UP006
    issued_at: datetime
    expires_at: datetime
    revoked: bool = False
    revoked_at: Optional[datetime] = None  # noqa: UP045
    metadata: Dict[str, Any] = Field(default_factory=dict)  # noqa: UP006


class IdentityClient:
    """Thin wrapper around the identity service's REST endpoints.

    Args:
        base_url: Base URL of the identity service. Defaults to the local
            dev port (``7425``).
        api_key: Bearer token used to call the identity service. The identity
            service itself is unauthenticated in v0.3 (it issues credentials,
            not consumes them) but the SDK still ships a token to be ready
            for the future.
        timeout: Per-request timeout in seconds.
        transport: Optional ``httpx`` transport (used by tests with ``respx``).
    """

    def __init__(
        self,
        base_url: str = DEFAULT_IDENTITY_URL,
        *,
        api_key: str | None = None,
        timeout: float = 30.0,
        transport: httpx.BaseTransport | None = None,
        fallback_urls: dict[str, str] | None = None,
        primary_region: str | None = None,
    ) -> None:
        self._http = HTTPClient(
            base_url,
            api_key or "identity-client",
            timeout=timeout,
            transport=transport,
            fallback_urls=fallback_urls,
            primary_region=primary_region,
        )

    def close(self) -> None:
        self._http.close()

    def __enter__(self) -> IdentityClient:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # ------------------------------------------------------------------ tokens

    def issue_token(
        self,
        agent_id: str,
        scopes: list[str] | None = None,
        *,
        tenant_id: str = "default",
        workspace_id: str | None = None,
        ttl_seconds: int = 3600,
        metadata: dict[str, Any] | None = None,
        rate_limit: dict[str, Any] | None = None,
    ) -> TokenIssueResponse:
        """Mint a JWT capability token.

        Args:
            agent_id: Subject of the token (the agent it speaks for).
            scopes: List of scope strings (see CONTRACTS.md scope grammar).
            tenant_id: Tenancy partition. Defaults to ``"default"``.
            workspace_id: Optional workspace constraint.
            ttl_seconds: Token lifetime in seconds.
            metadata: Free-form metadata persisted alongside the token.
            rate_limit: Optional per-token rate-limit overrides.
        """

        body: dict[str, Any] = {
            "agent_id": agent_id,
            "tenant_id": tenant_id,
            "scopes": list(scopes or []),
            "ttl_seconds": ttl_seconds,
        }
        if workspace_id is not None:
            body["workspace_id"] = workspace_id
        if metadata:
            body["metadata"] = metadata
        if rate_limit is not None:
            body["rate_limit"] = rate_limit

        response = self._http.post("/v1/tokens", json=body)
        return TokenIssueResponse.model_validate(response.json())

    def verify_token(self, token: str) -> TokenClaims:
        """Verify ``token`` and return the decoded claims.

        Raises:
            TokenExpired: when the token's ``exp`` is in the past.
            TokenRevoked: when the JTI is in the identity service blocklist.
            InvalidToken: when the signature/structure is invalid.
        """

        response = self._http.post("/v1/tokens/verify", json={"token": token})
        return TokenClaims.model_validate(response.json())

    def revoke_token(self, jti: str) -> None:
        """Revoke a token by JTI. Safe to call repeatedly (idempotent)."""

        self._http.post(f"/v1/tokens/{jti}/revoke")

    def get_token_info(self, jti: str) -> TokenInfo:
        """Introspect a token by JTI. Never returns the JWT itself."""

        response = self._http.get(f"/v1/tokens/{jti}")
        return TokenInfo.model_validate(response.json())

    # ---------------------------------------------- v0.6 federated revocation

    def list_revocations(
        self,
        since: int = 0,
        limit: int = 1000,
    ) -> RevocationList:
        """List revoked tokens with ``revoked_at > since`` (unix seconds).

        Used by Workspace + Gateway pollers to refresh their in-memory
        revocation caches. The response carries a ``next_since`` cursor
        suitable for the next call's ``since`` argument and a
        ``has_more`` flag signalling an immediately-available next page.

        Args:
            since: Unix-second timestamp; only newer revocations are returned.
            limit: Max entries per page (server caps at 2000, defaults to 1000).
        """

        params: dict[str, Any] = {
            "since": int(since),
            "limit": int(limit),
        }
        response = self._http.get_json("/v1/revocations", params=params)
        return RevocationList.model_validate(response)

    def iter_revocations(
        self,
        since: int = 0,
        page_size: int = 1000,
    ) -> "Iterator[RevocationEntry]":
        """Yield every :class:`RevocationEntry` newer than ``since``.

        Transparently follows the ``has_more`` cursor so callers don't
        have to. Useful for one-shot bootstrap of an in-memory cache.
        """

        cursor = int(since)
        while True:
            page = self.list_revocations(since=cursor, limit=page_size)
            for entry in page.revocations:
                yield entry
            if not page.has_more:
                return
            # Don't loop on a non-advancing cursor: the server returns
            # ``next_since`` based on the last entry, but if the page is
            # empty we'd otherwise spin. ``list_revocations`` returns
            # the original ``since`` when no rows match, so a same-cursor
            # response means "stop".
            if page.next_since == cursor:
                return
            cursor = page.next_since

    def revocation_stats(self) -> dict[str, int]:
        """Return cheap counters about revoked tokens.

        Mirrors ``GET /v1/revocations/stats``. Keys are ``total``,
        ``since_24h``, ``since_1h`` (all integers).
        """

        return self._http.get_json("/v1/revocations/stats")

    # ------------------------------------------------------------------ tokens

    def list_tokens(
        self,
        *,
        revoked: bool | None = None,
        since: datetime | None = None,
        agent_id: str | None = None,
        tenant_id: str | None = None,
        limit: int = 1000,
    ) -> list[TokenInfo]:
        """List issued tokens with optional filters.

        Useful for revocation polling: ``revoked=True`` + ``since=<ts>``
        returns the JTIs revoked since the last poll, which downstream
        services cache in memory.
        """

        params: dict[str, Any] = {}
        if revoked is not None:
            params["revoked"] = "true" if revoked else "false"
        if since is not None:
            params["since"] = since.isoformat()
        if agent_id is not None:
            params["agent_id"] = agent_id
        if tenant_id is not None:
            params["tenant_id"] = tenant_id
        params["limit"] = int(limit)
        response = self._http.get_json("/v1/tokens", params=params)
        return [TokenInfo.model_validate(t) for t in response.get("tokens", [])]

    # ------------------------------------------------------------------ tenants

    def list_tenants(self) -> list[dict[str, Any]]:
        """List tenants known to the identity service.

        Returns dicts (not the SDK :class:`plinth.models.Tenant` model) so
        callers can use this without importing the model. The shape mirrors
        the identity service response: ``{"id", "name", "metadata", "created_at"}``.
        """

        response = self._http.get_json("/v1/tenants")
        return list(response.get("tenants", []))

    def create_tenant(
        self,
        tenant_id: str,
        name: str,
        *,
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Create a new tenant on the identity service."""

        body: dict[str, Any] = {"id": tenant_id, "name": name}
        if metadata:
            body["metadata"] = metadata
        response = self._http.post("/v1/tenants", json=body)
        return response.json()

    def get_tenant(self, tenant_id: str) -> dict[str, Any]:
        """Look up a single tenant by id."""

        return self._http.get_json(f"/v1/tenants/{tenant_id}")

    # --------------------------------------------------------- quotas (v1.0)

    def get_quotas(self, tenant_id: str) -> TenantQuotas:
        """Fetch the per-tenant quota envelope.

        A tenant without an explicit row returns the contract defaults —
        identity never returns 404 here, so callers don't need to wrap
        the call in a try/except.
        """

        body = self._http.get_json(f"/v1/tenants/{tenant_id}/quotas")
        return TenantQuotas.model_validate(body)

    def set_quotas(
        self,
        tenant_id: str,
        quotas: TenantQuotas | TenantQuotasUpdate | dict[str, Any],
    ) -> TenantQuotas:
        """Patch the tenant's quota envelope.

        Accepts a :class:`TenantQuotas` (full overwrite), a
        :class:`TenantQuotasUpdate` (partial), or a raw dict. Unset
        fields fall back to the existing row.
        """

        if isinstance(quotas, TenantQuotas):
            body = quotas.model_dump(
                exclude={"tenant_id", "updated_at"},
                exclude_none=True,
            )
        elif isinstance(quotas, TenantQuotasUpdate):
            body = quotas.model_dump(exclude_none=True)
        else:
            body = dict(quotas)
        response = self._http.post(
            f"/v1/tenants/{tenant_id}/quotas",
            json=body,
        )
        return TenantQuotas.model_validate(response.json())

    def reset_quotas(self, tenant_id: str) -> None:
        """Drop the tenant's quota row, reverting it to defaults."""

        self._http.delete(f"/v1/tenants/{tenant_id}/quotas")

    def get_usage(self, tenant_id: str) -> TenantUsage:
        """Return the per-tenant usage rollup.

        Some fields (``storage_gb``, ``cost_usd_day``, ``cost_usd_month``,
        ``last_invocation_at``) live in other services and surface as
        ``0`` / ``None`` with a ``notes`` map pointing at the canonical
        source.
        """

        body = self._http.get_json(f"/v1/tenants/{tenant_id}/usage")
        return TenantUsage.model_validate(body)

    # ---------------------------------------------------------------- keys (v0.4)

    def list_keys(self, *, include_expired: bool = False) -> list[SigningKey]:
        """List signing keys (public material only).

        For an HS256 deployment the identity service returns an empty
        list — the secret isn't published.

        Args:
            include_expired: When True, also return keys whose
                ``expires_at`` is in the past.
        """

        params: dict[str, Any] = {}
        if include_expired:
            params["include_expired"] = "true"
        response = self._http.get_json("/v1/keys", params=params)
        return [SigningKey.model_validate(k) for k in response.get("keys", [])]

    def get_key(self, kid: str) -> SigningKey:
        """Look up a single signing key by ``kid``.

        Raises:
            PlinthError: when the key doesn't exist (404 from identity).
        """

        # Identity exposes ``GET /v1/keys`` only — list and filter client-side.
        for key in self.list_keys(include_expired=True):
            if key.kid == kid:
                return key
        raise PlinthError(
            f"Signing key {kid!r} does not exist",
            code="SIGNING_KEY_NOT_FOUND",
        )

    def rotate_key(self) -> SigningKey:
        """Force a rotation. Returns the new active :class:`SigningKey`.

        Raises:
            PlinthError: when the identity service is in HS256 mode
                (rotation isn't applicable to a shared-secret deployment).
        """

        response = self._http.post("/v1/keys/rotate")
        return SigningKey.model_validate(response.json())

    def expire_key(self, kid: str) -> None:
        """Force-expire a signing key (incident response).

        After expiry, tokens signed with this key fail signature
        verification on the workspace + gateway as soon as their
        cached JWKS expires (or sooner, if they hit a kid miss).
        """

        self._http.delete(f"/v1/keys/{kid}")

    # ------------------------------------------------------ GDPR (v1.0)

    def export_tenant(self, tenant_id: str) -> ExportJob:
        """Kick off a GDPR Article 20 (data portability) export.

        Returns the ``ExportJob`` handle (``export_id``, ``status``).
        The caller polls :meth:`get_export` until ``status == 'ready'``,
        then fetches the ZIP via :meth:`download_export`.
        """

        response = self._http.post(f"/v1/tenants/{tenant_id}/export")
        return ExportJob.model_validate(response.json())

    def get_export(self, tenant_id: str, export_id: str) -> ExportStatus:
        """Return the current :class:`ExportStatus` for ``export_id``."""

        body = self._http.get_json(
            f"/v1/tenants/{tenant_id}/exports/{export_id}"
        )
        return ExportStatus.model_validate(body)

    def download_export(self, tenant_id: str, export_id: str) -> bytes:
        """Fetch the ZIP body for a ``ready`` export. Raises on 410/409."""

        response = self._http.get(
            f"/v1/tenants/{tenant_id}/exports/{export_id}/download"
        )
        return response.content

    def request_delete_confirmation(
        self,
        tenant_id: str,
    ) -> DeleteConfirmation:
        """Phase 1 of GDPR Article 17 — issue a one-shot confirm token.

        The token is short-lived (~10 min). Pass it back as the
        ``confirm_token=`` argument of :meth:`delete_tenant_data`.
        """

        response = self._http.post(
            f"/v1/tenants/{tenant_id}/delete-data-confirm"
        )
        return DeleteConfirmation.model_validate(response.json())

    def delete_tenant_data(
        self,
        tenant_id: str,
        *,
        confirm_token: str,
    ) -> DeleteJob:
        """Phase 2 of GDPR Article 17 — kick off the cascade.

        Returns the :class:`DeleteJob` handle. Poll with
        :meth:`get_delete_job` until ``status`` settles.
        """

        response = self._http.delete(
            f"/v1/tenants/{tenant_id}/data",
            params={"confirm": confirm_token},
        )
        return DeleteJob.model_validate(response.json())

    def get_delete_job(self, tenant_id: str, job_id: str) -> DeleteJob:
        """Return the current :class:`DeleteJob` snapshot."""

        body = self._http.get_json(
            f"/v1/tenants/{tenant_id}/delete-jobs/{job_id}"
        )
        return DeleteJob.model_validate(body)


# Re-export the new typed exceptions so callers can do
# ``from plinth.identity import TokenExpired``.
__all__ = [
    "DEFAULT_IDENTITY_URL",
    "DeleteConfirmation",
    "DeleteJob",
    "ExportJob",
    "ExportStatus",
    "IdentityClient",
    "InvalidToken",
    "PlinthError",
    "RevocationEntry",
    "RevocationList",
    "SigningKey",
    "TenantQuotas",
    "TenantQuotasUpdate",
    "TenantUsage",
    "TokenClaims",
    "TokenExpired",
    "TokenInfo",
    "TokenIssueResponse",
    "TokenRevoked",
]
