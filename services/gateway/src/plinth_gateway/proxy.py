# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 The Plinth Authors
"""HTTP proxy to MCP-style backends.

The gateway POSTs the raw ``arguments`` dict as JSON to the registered
``tool.endpoint`` and expects a JSON object back. Non-2xx or malformed
JSON raise :class:`ToolInvocationError`.

OAuth-backed tools (``auth_method=oauth2``) get their bearer token resolved
just-in-time: the proxy looks up the connection via the optional
``connection_store``, decrypts the access token, and attaches
``Authorization: Bearer <token>``. If the token is expired and a refresh
token is on file, the proxy attempts to refresh before the call.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from typing import Any

import httpx

from .auth import outbound_headers
from .exceptions import OAuthError, ToolInvocationError, TransportNotSupported
from .logging_config import get_logger
from .models import Tool

log = get_logger(__name__)

# How long before nominal expiry to proactively refresh, so we don't fire a
# request just to have it 401 a second later because the token expired in flight.
_REFRESH_LEEWAY = timedelta(seconds=30)


class HttpProxy:
    """Wrap an :class:`httpx.AsyncClient` to call registered HTTP tools."""

    def __init__(
        self,
        *,
        timeout_seconds: float = 30.0,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._timeout = timeout_seconds
        self._owned_client = client is None
        self._client = client or httpx.AsyncClient(timeout=timeout_seconds)

    @property
    def client(self) -> httpx.AsyncClient:
        return self._client

    async def aclose(self) -> None:
        """Close the underlying httpx client (only if we own it)."""
        if self._owned_client:
            await self._client.aclose()

    async def invoke(
        self,
        tool: Tool,
        arguments: dict[str, Any],
        *,
        connection_store: Any | None = None,
        settings: Any | None = None,
        connection_id_override: str | None = None,
    ) -> Any:
        """Invoke ``tool`` with ``arguments``. Raise on failure.

        For ``transport=http`` we POST ``arguments`` as JSON to ``tool.endpoint``
        and decode the JSON body. For ``transport=stdio`` (or anything else)
        we raise :class:`TransportNotSupported`.

        OAuth resolution: when ``tool.auth_method == "oauth2"`` and a
        ``connection_store`` is provided, the access token is fetched and
        attached as ``Authorization: Bearer ...``. If the token is past its
        expiry (with leeway) and a refresh token exists, the proxy attempts a
        refresh before the call.

        Args:
            tool: The registered tool.
            arguments: JSON-serialisable args.
            connection_store: An :class:`OAuthConnectionStore` (or None to use
                the legacy ``mock_token`` path).
            settings: Gateway :class:`Settings` (needed to refresh tokens).
            connection_id_override: Connection id resolved from request context
                (e.g. via ``connection_id_from`` template). Falls back to
                ``tool.auth_config["connection_id"]``.
        """
        if tool.transport != "http":
            raise TransportNotSupported(
                f"Transport {tool.transport!r} is not supported in v0.1",
                details={"tool_id": tool.tool_id, "transport": tool.transport},
            )

        headers = {"Content-Type": "application/json"}

        if tool.auth_method == "oauth2" and connection_store is not None:
            auth_header = await self._resolve_oauth_header(
                tool=tool,
                connection_store=connection_store,
                settings=settings,
                connection_id_override=connection_id_override,
            )
            if auth_header is not None:
                headers["Authorization"] = auth_header
            else:
                # Fall back to legacy mock_token if configured (preserves v0.1
                # behaviour for tests that haven't migrated yet).
                headers.update(outbound_headers(tool.auth_method, tool.auth_config))
        else:
            headers.update(outbound_headers(tool.auth_method, tool.auth_config))

        try:
            response = await self._client.post(
                tool.endpoint,
                content=json.dumps(arguments),
                headers=headers,
                timeout=self._timeout,
            )
        except httpx.RequestError as exc:
            raise ToolInvocationError(
                f"Backend request failed: {exc}",
                details={"tool_id": tool.tool_id, "endpoint": tool.endpoint},
            ) from exc

        if response.status_code >= 400:
            body_preview = response.text[:500]
            raise ToolInvocationError(
                f"Backend returned HTTP {response.status_code}",
                details={
                    "tool_id": tool.tool_id,
                    "status_code": response.status_code,
                    "body_preview": body_preview,
                },
            )

        try:
            return response.json()
        except ValueError as exc:
            raise ToolInvocationError(
                f"Backend returned non-JSON response: {exc}",
                details={"tool_id": tool.tool_id},
            ) from exc

    async def _resolve_oauth_header(
        self,
        *,
        tool: Tool,
        connection_store: Any,
        settings: Any | None,
        connection_id_override: str | None,
    ) -> str | None:
        """Resolve and return the ``Authorization`` header for an oauth2 tool.

        Returns None when the tool has no usable connection wired (so the
        caller can fall back to the legacy mock-token path or raise).
        """
        cfg = tool.auth_config or {}
        connection_id = connection_id_override or cfg.get("connection_id")
        if not connection_id:
            return None

        try:
            decrypted = await connection_store.get_decrypted(connection_id)
        except Exception as exc:  # noqa: BLE001
            raise ToolInvocationError(
                f"oauth connection lookup failed: {exc}",
                details={"tool_id": tool.tool_id, "connection_id": connection_id},
            ) from exc

        if decrypted.expires_at is not None and decrypted.refresh_token:
            now = datetime.now(timezone.utc)
            expires_at = decrypted.expires_at
            if expires_at.tzinfo is None:
                expires_at = expires_at.replace(tzinfo=timezone.utc)
            if expires_at - now <= _REFRESH_LEEWAY:
                refreshed = await self._maybe_refresh_token(
                    decrypted=decrypted,
                    connection_store=connection_store,
                    settings=settings,
                    tool=tool,
                )
                if refreshed is not None:
                    return f"Bearer {refreshed}"

        return f"Bearer {decrypted.access_token}"

    async def _maybe_refresh_token(
        self,
        *,
        decrypted: Any,
        connection_store: Any,
        settings: Any | None,
        tool: Tool,
    ) -> str | None:
        """Best-effort refresh. Returns the new access token, or None on skip.

        We deliberately swallow refresh failures and return None so the call
        falls back to the (possibly-stale) cached token. The downstream API
        will surface a 401 if the token really has expired, and the agent
        can react.
        """
        if settings is None:
            return None
        try:
            from .oauth import (
                get_provider,
                provider_credentials,
                refresh_token_grant,
            )

            provider = get_provider(decrypted.provider)
            client_id, client_secret = provider_credentials(provider, settings)
        except Exception as exc:  # noqa: BLE001
            log.warning(
                "oauth.refresh.skipped",
                tool_id=tool.tool_id,
                connection_id=decrypted.id,
                reason=str(exc),
            )
            return None

        try:
            grant = await refresh_token_grant(
                provider=provider,
                client_id=client_id,
                client_secret=client_secret,
                refresh_token=decrypted.refresh_token,
                http_client=self._client,
            )
        except OAuthError as exc:
            log.warning(
                "oauth.refresh.failed",
                tool_id=tool.tool_id,
                connection_id=decrypted.id,
                error=exc.message,
            )
            return None

        await connection_store.update_tokens(
            decrypted.id,
            access_token=grant.access_token,
            refresh_token=grant.refresh_token,
            expires_at=grant.expires_at,
        )
        log.info(
            "oauth.refresh.success",
            tool_id=tool.tool_id,
            connection_id=decrypted.id,
        )
        return grant.access_token
