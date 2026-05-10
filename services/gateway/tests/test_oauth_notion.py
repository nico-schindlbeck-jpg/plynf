# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 The Plinth Authors
"""Provider-specific OAuth tests for Notion (v1.1).

These complement ``test_oauth.py`` and ``test_oauth_providers.py`` which
cover the GitHub/Slack/Linear providers. The protocol code is shared, so
tests here focus on:

* the provider registry (``get_provider("notion")``),
* the per-provider configuration assertions (503 ``OAUTH_NOT_CONFIGURED``),
* PKCE-OFF behavior (Notion does NOT support PKCE),
* the ``Notion-Version`` header on userinfo,
* end-to-end authorize → callback round-trip.
"""

from __future__ import annotations

from urllib.parse import parse_qs, urlparse

import pytest
import pytest_asyncio
import respx
from httpx import ASGITransport, AsyncClient, Response

from plinth_gateway.api import create_app
from plinth_gateway.encryption import generate_key
from plinth_gateway.exceptions import OAuthProviderNotConfigured
from plinth_gateway.oauth import (
    NOTION,
    assert_provider_configured,
    fetch_user_info,
    get_provider,
    provider_credentials,
    provider_redirect_uri,
)
from plinth_gateway.settings import Settings


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def notion_settings(tmp_path) -> Settings:
    """Settings with the Notion OAuth client configured for tests."""
    return Settings(
        data_dir=tmp_path,
        gateway_host="127.0.0.1",
        gateway_port=7422,
        log_level="WARNING",
        log_format="console",
        backend_timeout_seconds=5.0,
        oauth_encryption_key=generate_key(),
        oauth_notion_client_id="notion-cid",
        oauth_notion_client_secret="notion-cs",
        oauth_notion_redirect_uri="http://localhost:7422/v1/oauth/notion/callback",
        oauth_state_ttl_seconds=600,
    )


@pytest_asyncio.fixture
async def app_and_client(notion_settings: Settings):
    app = create_app(notion_settings)
    transport = ASGITransport(app=app)
    async with AsyncClient(
        transport=transport,
        base_url="http://testserver",
        headers={"Authorization": "Bearer test-token"},
    ) as client:
        async with app.router.lifespan_context(app):
            yield app, client


@pytest.fixture
def client(app_and_client):
    return app_and_client[1]


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------


def test_notion_provider_defaults() -> None:
    p = get_provider("notion")
    assert p is NOTION
    assert p.name == "notion"
    assert p.authorize_url == "https://api.notion.com/v1/oauth/authorize"
    assert p.token_url == "https://api.notion.com/v1/oauth/token"
    assert p.userinfo_url == "https://api.notion.com/v1/users/me"
    # Notion is workspace-scoped: no per-call scopes.
    assert p.default_scopes == []
    # Notion's OAuth flow does NOT support PKCE.
    assert p.pkce is False


def test_notion_provider_lookup_case_insensitive() -> None:
    assert get_provider("NOTION") is NOTION
    assert get_provider("Notion") is NOTION


# ---------------------------------------------------------------------------
# Configuration helpers
# ---------------------------------------------------------------------------


def test_assert_notion_configured(notion_settings: Settings) -> None:
    # Should NOT raise — credentials are wired.
    assert_provider_configured(NOTION, notion_settings)


def test_assert_notion_not_configured(tmp_path) -> None:
    settings = Settings(
        data_dir=tmp_path,
        log_level="WARNING",
        oauth_encryption_key=generate_key(),
        oauth_notion_client_id="",
        oauth_notion_client_secret="",
    )
    with pytest.raises(OAuthProviderNotConfigured) as exc:
        assert_provider_configured(NOTION, settings)
    assert "notion" in exc.value.message.lower()
    assert "PLINTH_OAUTH_NOTION_CLIENT_ID" in str(exc.value.details)


def test_notion_provider_credentials_and_redirect(notion_settings: Settings) -> None:
    cid, cs = provider_credentials(NOTION, notion_settings)
    assert cid == "notion-cid"
    assert cs == "notion-cs"
    assert (
        provider_redirect_uri(NOTION, notion_settings)
        == "http://localhost:7422/v1/oauth/notion/callback"
    )


# ---------------------------------------------------------------------------
# Authorize endpoint
# ---------------------------------------------------------------------------


async def test_notion_authorize_redirects_with_correct_params(client) -> None:
    resp = await client.get(
        "/v1/oauth/notion/authorize",
        params={"redirect_uri": "http://app.local/done"},
        follow_redirects=False,
    )
    assert resp.status_code == 302
    location = resp.headers["location"]
    assert location.startswith("https://api.notion.com/v1/oauth/authorize?")
    qs = parse_qs(urlparse(location).query)
    assert qs["client_id"] == ["notion-cid"]
    assert qs["redirect_uri"] == ["http://localhost:7422/v1/oauth/notion/callback"]
    assert qs["response_type"] == ["code"]
    # PKCE is OFF for Notion — challenge should not be set.
    assert "code_challenge" not in qs
    assert "code_challenge_method" not in qs
    # State is server-minted and present.
    assert len(qs["state"][0]) > 16


async def test_notion_authorize_503_when_not_configured(tmp_path) -> None:
    settings = Settings(
        data_dir=tmp_path,
        log_level="WARNING",
        oauth_encryption_key=generate_key(),
        oauth_notion_client_id="",
        oauth_notion_client_secret="",
    )
    app = create_app(settings)
    transport = ASGITransport(app=app)
    async with AsyncClient(
        transport=transport,
        base_url="http://testserver",
        headers={"Authorization": "Bearer test-token"},
    ) as client:
        async with app.router.lifespan_context(app):
            resp = await client.get(
                "/v1/oauth/notion/authorize",
                params={"redirect_uri": "http://x/cb"},
                follow_redirects=False,
            )
            assert resp.status_code == 503
            body = resp.json()
            assert body["error"]["code"] == "OAUTH_NOT_CONFIGURED"
            assert "PLINTH_OAUTH_NOTION_CLIENT_ID" in str(body["error"]["details"])


# ---------------------------------------------------------------------------
# fetch_user_info — Notion-Version header check
# ---------------------------------------------------------------------------


async def test_fetch_user_info_notion_sends_version_header() -> None:
    import httpx

    captured: dict = {}

    def _capture(request):
        captured["headers"] = dict(request.headers)
        return Response(
            200,
            json={
                "object": "user",
                "id": "user-123",
                "type": "person",
                "name": "Alice",
                "person": {"email": "alice@example.com"},
            },
        )

    async with httpx.AsyncClient() as http:
        with respx.mock(assert_all_called=True) as mock:
            mock.get("https://api.notion.com/v1/users/me").mock(side_effect=_capture)
            info = await fetch_user_info(
                provider=NOTION, access_token="secret-token", http_client=http
            )
        assert info["id"] == "user-123"
        assert info["name"] == "Alice"
        # Confirm the Notion-Version header was set.
        assert captured["headers"].get("notion-version") == "2022-06-28"
        assert captured["headers"].get("authorization") == "Bearer secret-token"


# ---------------------------------------------------------------------------
# Full callback round-trip
# ---------------------------------------------------------------------------


async def test_notion_callback_creates_connection(app_and_client) -> None:
    app, client = app_and_client

    resp = await client.get(
        "/v1/oauth/notion/authorize",
        params={"redirect_uri": "http://app.local/done"},
        follow_redirects=False,
    )
    state = parse_qs(urlparse(resp.headers["location"]).query)["state"][0]

    with respx.mock(assert_all_called=True) as mock:
        # Notion's token endpoint returns a flat JSON shape.
        mock.post("https://api.notion.com/v1/oauth/token").mock(
            return_value=Response(
                200,
                json={
                    "access_token": "secret_notion_token",
                    "token_type": "bearer",
                    "bot_id": "bot-1",
                    "workspace_id": "ws-1",
                    "workspace_name": "Demo Workspace",
                    "owner": {"type": "user", "user": {"id": "user-1"}},
                },
            )
        )
        mock.get("https://api.notion.com/v1/users/me").mock(
            return_value=Response(
                200,
                json={
                    "object": "user",
                    "id": "user-1",
                    "type": "person",
                    "name": "Alice",
                    "person": {"email": "alice@example.com"},
                },
            )
        )
        cb = await client.get(
            "/v1/oauth/notion/callback",
            params={"code": "auth-code", "state": state},
            follow_redirects=False,
        )
        assert cb.status_code == 302
        target = cb.headers["location"]
        qs = parse_qs(urlparse(target).query)
        conn_id = qs["connection_id"][0]

    public = await client.get(f"/v1/oauth/connections/{conn_id}")
    assert public.status_code == 200
    body = public.json()
    assert body["provider"] == "notion"
    assert body["user_id"] == "user-1"
    assert body["user_login"] == "Alice"

    decrypted = await app.state.oauth_connections.get_decrypted(conn_id)
    assert decrypted.access_token == "secret_notion_token"


# ---------------------------------------------------------------------------
# Connection list filtering
# ---------------------------------------------------------------------------


async def test_notion_connection_list_filters_by_provider(app_and_client) -> None:
    app, client = app_and_client

    # Seed a Notion + a non-Notion connection via the public POST endpoint.
    a = await client.post(
        "/v1/oauth/connections",
        json={
            "provider": "notion",
            "user_id": "u1",
            "user_login": "alice",
            "scopes": [],
            "access_token": "tok-notion",
        },
    )
    assert a.status_code == 201
    b = await client.post(
        "/v1/oauth/connections",
        json={
            "provider": "github",
            "user_id": "u2",
            "user_login": "bob",
            "scopes": ["repo"],
            "access_token": "tok-gh",
        },
    )
    assert b.status_code == 201

    listed = await client.get("/v1/oauth/connections", params={"provider": "notion"})
    assert listed.status_code == 200
    rows = listed.json()["connections"]
    providers = {row["provider"] for row in rows}
    assert providers == {"notion"}
