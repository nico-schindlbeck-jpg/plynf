# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 The Plinth Authors
"""FastAPI routes for the OAuth flow + connection management."""

from __future__ import annotations

from typing import Any
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, Header, Query, Request, Response
from fastapi.responses import RedirectResponse

from .auth import check_inbound_auth
from .exceptions import OAuthError
from .logging_config import get_logger
from .models import (
    OAuthConnectionCreate,
    OAuthConnectionListResponse,
    OAuthConnectionPublic,
    OAuthRefreshRequest,
    OAuthRefreshResponse,
)
from .oauth import (
    OAuthConnectionStore,
    OAuthStateStore,
    assert_provider_configured,
    build_authorize_redirect,
    exchange_code_for_token,
    fetch_atlassian_cloudid,
    fetch_user_info,
    get_provider,
    parse_scopes,
    provider_credentials,
    provider_redirect_uri,
    refresh_token_grant,
    _new_pkce_pair,
)

log = get_logger(__name__)


def create_oauth_router() -> APIRouter:
    """Build the ``/v1/oauth/...`` router.

    Dependencies are pulled off ``request.app.state`` so the router stays
    factoryable without an explicit DI container — same pattern the rest of
    the gateway uses.
    """
    router = APIRouter(prefix="/v1/oauth", tags=["oauth"])

    async def _inbound_auth(
        request: Request,
        authorization: str | None = Header(default=None),
    ) -> None:
        if request.app.state.settings.inbound_auth_required:
            check_inbound_auth(authorization)

    def _connection_store(request: Request) -> OAuthConnectionStore:
        return request.app.state.oauth_connections

    def _state_store(request: Request) -> OAuthStateStore:
        return request.app.state.oauth_states

    # ------------------------------------------------------------------
    # Authorize
    # ------------------------------------------------------------------

    @router.get("/{provider_name}/authorize")
    async def authorize(
        provider_name: str,
        request: Request,
        redirect_uri: str = Query(...),
        state: str | None = Query(default=None),  # caller-supplied; we mint our own
        scopes: str | None = Query(default=None),
        tenant_id: str = Query(default="default"),
    ) -> RedirectResponse:
        """Redirect the browser to the provider's consent screen."""
        # NOTE: caller-supplied ``state`` is ignored intentionally; the gateway
        # always mints its own server-side state to defend against CSRF and to
        # remember the redirect_uri + PKCE verifier across the round-trip.
        del state
        provider = get_provider(provider_name)
        settings = request.app.state.settings
        assert_provider_configured(provider, settings)

        client_id, _ = provider_credentials(provider, settings)
        gateway_redirect = provider_redirect_uri(provider, settings)

        scope_list = parse_scopes(scopes, default=provider.default_scopes)

        verifier: str | None = None
        challenge: str | None = None
        if provider.pkce:
            verifier, challenge = _new_pkce_pair()

        states: OAuthStateStore = _state_store(request)
        new_state = await states.create(
            provider=provider.name,
            redirect_uri=redirect_uri,
            scopes=scope_list,
            pkce_verifier=verifier,
            tenant_id=tenant_id,
        )

        url = build_authorize_redirect(
            provider,
            client_id=client_id,
            redirect_uri=gateway_redirect,
            scopes=scope_list,
            state=new_state,
            pkce_challenge=challenge,
        )
        log.info(
            "oauth.authorize",
            provider=provider.name,
            tenant_id=tenant_id,
            scopes=scope_list,
        )
        return RedirectResponse(url=url, status_code=302)

    # ------------------------------------------------------------------
    # Callback (no auth — provider is calling us, browser-mediated)
    # ------------------------------------------------------------------

    @router.get("/{provider_name}/callback")
    async def callback(
        provider_name: str,
        request: Request,
        code: str | None = Query(default=None),
        state: str | None = Query(default=None),
        error: str | None = Query(default=None),
        error_description: str | None = Query(default=None),
    ) -> RedirectResponse:
        """Provider redirects here with ``code`` + ``state``. We close the loop."""
        provider = get_provider(provider_name)
        settings = request.app.state.settings

        if error is not None:
            raise OAuthError(
                f"oauth provider returned error: {error}",
                details={"provider_error": error, "description": error_description},
            )
        if not code or not state:
            raise OAuthError(
                "oauth callback missing code or state",
                details={"has_code": bool(code), "has_state": bool(state)},
            )

        assert_provider_configured(provider, settings)
        client_id, client_secret = provider_credentials(provider, settings)
        gateway_redirect = provider_redirect_uri(provider, settings)

        states: OAuthStateStore = _state_store(request)
        record = await states.consume(state, provider=provider.name)

        proxy = request.app.state.proxy
        http_client = proxy.client

        grant = await exchange_code_for_token(
            provider=provider,
            client_id=client_id,
            client_secret=client_secret,
            code=code,
            redirect_uri=gateway_redirect,
            pkce_verifier=record.pkce_verifier,
            http_client=http_client,
        )

        userinfo = await fetch_user_info(
            provider=provider,
            access_token=grant.access_token,
            http_client=http_client,
        )
        user_id = userinfo.get(provider.userinfo_id_field)
        if user_id is None:
            raise OAuthError(
                f"userinfo missing {provider.userinfo_id_field!r}",
                details={"keys": sorted(userinfo.keys())},
            )
        user_login = userinfo.get(provider.userinfo_login_field)

        # Effective scopes: prefer what the provider granted, fall back to the
        # state's recorded request, fall back to the provider defaults.
        effective_scopes = grant.scopes or record.scopes or list(provider.default_scopes)

        # v1.5 — Per-provider connection metadata. Salesforce returns
        # ``instance_url`` in the token body itself; Atlassian needs a
        # follow-up call to ``/oauth/token/accessible-resources`` to learn
        # the workspace's cloudid. Other providers leave metadata empty.
        connection_metadata: dict[str, Any] = {}
        if provider.name == "salesforce" and grant.instance_url:
            connection_metadata["instance_url"] = grant.instance_url
        if provider.name == "atlassian":
            try:
                cloudid = await fetch_atlassian_cloudid(
                    access_token=grant.access_token,
                    http_client=http_client,
                )
            except OAuthError:
                # Re-raise — the caller needs to know the OAuth setup did not
                # complete. Without a cloudid the Atlassian MCP server has
                # nothing to address.
                raise
            if cloudid:
                connection_metadata["cloudid"] = cloudid

        connections: OAuthConnectionStore = _connection_store(request)
        connection = await connections.create(
            tenant_id=record.tenant_id,
            provider=provider.name,
            user_id=str(user_id),
            user_login=str(user_login) if user_login else None,
            scopes=effective_scopes,
            access_token=grant.access_token,
            refresh_token=grant.refresh_token,
            expires_at=grant.expires_at,
            metadata=connection_metadata or None,
        )

        log.info(
            "oauth.callback.success",
            provider=provider.name,
            tenant_id=record.tenant_id,
            connection_id=connection.id,
            user_login=user_login,
            metadata_keys=sorted(connection_metadata.keys()),
        )

        # Append the connection_id to the caller's redirect_uri.
        sep = "&" if "?" in record.redirect_uri else "?"
        target = f"{record.redirect_uri}{sep}{urlencode({'connection_id': connection.id})}"
        return RedirectResponse(url=target, status_code=302)

    # ------------------------------------------------------------------
    # Refresh
    # ------------------------------------------------------------------

    @router.post(
        "/{provider_name}/refresh",
        response_model=OAuthRefreshResponse,
        dependencies=[Depends(_inbound_auth)],
    )
    async def refresh(
        provider_name: str,
        request: Request,
        body: OAuthRefreshRequest,
    ) -> OAuthRefreshResponse:
        provider = get_provider(provider_name)
        settings = request.app.state.settings
        assert_provider_configured(provider, settings)
        client_id, client_secret = provider_credentials(provider, settings)

        connections: OAuthConnectionStore = _connection_store(request)
        decrypted = await connections.get_decrypted(body.connection_id)
        if decrypted.provider != provider.name:
            raise OAuthError(
                "connection does not belong to this provider",
                details={"connection_id": body.connection_id, "provider": provider.name},
            )
        if not decrypted.refresh_token:
            raise OAuthError(
                "connection has no refresh token; cannot refresh",
                details={"connection_id": body.connection_id},
            )

        proxy = request.app.state.proxy
        grant = await refresh_token_grant(
            provider=provider,
            client_id=client_id,
            client_secret=client_secret,
            refresh_token=decrypted.refresh_token,
            http_client=proxy.client,
        )

        public = await connections.update_tokens(
            body.connection_id,
            access_token=grant.access_token,
            refresh_token=grant.refresh_token,
            expires_at=grant.expires_at,
        )
        return OAuthRefreshResponse(
            connection_id=public.id,
            expires_at=public.expires_at,
            last_refreshed_at=public.last_refreshed_at,
            refreshed=True,
        )

    # ------------------------------------------------------------------
    # Connections — CRUD
    # ------------------------------------------------------------------

    @router.post(
        "/connections",
        response_model=OAuthConnectionPublic,
        status_code=201,
        dependencies=[Depends(_inbound_auth)],
    )
    async def create_connection(
        request: Request,
        body: OAuthConnectionCreate,
    ) -> OAuthConnectionPublic:
        # We accept arbitrary provider names here (no get_provider lookup) so
        # tests and ops tooling can seed connections without configuring the
        # full OAuth client. Real authorize/callback flow always uses a known
        # provider.
        connections: OAuthConnectionStore = _connection_store(request)
        return await connections.create(
            tenant_id=body.tenant_id,
            provider=body.provider,
            user_id=body.user_id,
            user_login=body.user_login,
            scopes=body.scopes,
            access_token=body.access_token,
            refresh_token=body.refresh_token,
            expires_at=body.expires_at,
            metadata=body.metadata or None,
        )

    @router.get(
        "/connections",
        response_model=OAuthConnectionListResponse,
        dependencies=[Depends(_inbound_auth)],
    )
    async def list_connections(
        request: Request,
        tenant_id: str | None = Query(default=None),
        provider: str | None = Query(default=None),
    ) -> OAuthConnectionListResponse:
        connections: OAuthConnectionStore = _connection_store(request)
        items = await connections.list_public(tenant_id=tenant_id, provider=provider)
        return OAuthConnectionListResponse(connections=items)

    @router.get(
        "/connections/{conn_id}",
        response_model=OAuthConnectionPublic,
        dependencies=[Depends(_inbound_auth)],
    )
    async def get_connection(
        conn_id: str,
        request: Request,
    ) -> OAuthConnectionPublic:
        connections: OAuthConnectionStore = _connection_store(request)
        return await connections.require_public(conn_id)

    @router.delete(
        "/connections/{conn_id}",
        status_code=204,
        dependencies=[Depends(_inbound_auth)],
    )
    async def delete_connection(
        conn_id: str,
        request: Request,
    ) -> Response:
        connections: OAuthConnectionStore = _connection_store(request)
        await connections.delete(conn_id)
        return Response(status_code=204)

    return router
